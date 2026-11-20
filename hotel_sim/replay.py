"""Auditable, isolated policy replay; never reads or changes reservation databases."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from .scenario import build_allocator, load_scenario, parse_requests, records, simulate

MODES = ('strict', 'upgrade', 'protected_upgrade')


def canonical_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def replay(data, protection=None, source_sha256=None):
    """Replace the entire protection schedule when supplied; [] disables reserves.

    Omitted dates/types have reserve zero. Native requested-type bookings may
    consume protected inventory: this is an upgrade floor, not permanently idle stock.
    """
    effective = copy.deepcopy(data)
    if protection is not None:
        rows = records(protection, 'protection')
        if any(set(row) != {'room_type', 'date', 'count'} for row in rows):
            raise ValueError('protection rows require exactly room_type, date, count')
        effective['protection'] = copy.deepcopy(rows)
    # Existing constructor validates types, dates, duplicate keys and integer counts.
    reference = build_allocator(effective, 'protected_upgrade')
    lowest_rank = min(typ.rank for typ in reference.types.values())
    for (typ, day), count in reference.protection.items():
        if reference.types[typ].rank <= lowest_rank:
            raise ValueError('protection is restricted to high-tier room types')
        capacity = sum(room.room_type == typ and day not in room.blocked_dates for room in reference.rooms)
        if count > capacity:
            raise ValueError('protection exceeds usable room capacity on date')
    effective['protection'] = sorted(effective.get('protection', []), key=lambda row: (row['date'], row['room_type']))
    digest = hashlib.sha256(canonical_bytes(effective)).hexdigest()
    result = simulate(effective, digest)
    result['replay'] = {
        'schema_version': 1, 'evidence_status': 'executed_simulation',
        'source_file_sha256': source_sha256, 'effective_input_sha256': digest,
        'hash_encoding': 'UTF-8 sorted compact JSON plus LF',
        'effective_input': effective, 'generator_seed': effective.get('generator_seed'),
        'writes_reservations': False,
        'protection_semantics': 'Minimum remaining high-tier rooms after upgrades only; native demand may consume reserve; omitted pairs default to zero.',
        'reason_semantics': 'Rejection codes are nonexclusive candidate blockers, not a unique causal diagnosis.',
    }
    requests = parse_requests(effective)
    for mode in MODES:
        engine = build_allocator(effective, mode)
        steps = []
        for index, request in enumerate(requests):
            before = engine.snapshot()
            decision = engine.allocate(request)
            after = engine.snapshot()
            changes = []
            for old, new in zip(before['daily'], after['daily']):
                if old != new:
                    changes.append({'date': new['date'],
                        'room_delta': {typ: new['available_by_type'][typ] - old['available_by_type'][typ]
                                       for typ in new['available_by_type']},
                        'extra_beds_delta': new['extra_beds_used'] - old['extra_beds_used'],
                        'cots_delta': new['cots_used'] - old['cots_used'],
                        'remaining_by_type': new['available_by_type']})
            steps.append({'index': index, 'request_id': request.request_id,
                          'decision': decision.to_dict(), 'daily_changes': changes})
        assert [step['decision'] for step in steps] == result['policies'][mode]['decisions']
        assert engine.snapshot() == result['policies'][mode]['inventory']
        result['policies'][mode]['steps'] = steps
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--protection', type=Path, help='JSON array replacing the whole protection schedule; [] disables it')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data, digest = load_scenario(args.input)
    schedule = json.loads(args.protection.read_text(encoding='utf-8')) if args.protection else None
    result = replay(data, schedule, digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental overwrite of evidence or input files.
    with args.output.open('xb') as stream:
        stream.write(canonical_bytes(result))
    print(json.dumps({mode: result['policies'][mode]['summary'] for mode in MODES}, indent=2))


if __name__ == '__main__':
    main()
