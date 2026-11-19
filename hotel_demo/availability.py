"""Read-only projection of one SQLite snapshot; preview mutates only a disposable planner."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from hotel_sim.allocation import BookingRequest
from hotel_sim.scenario import parse_date


def query_parameters(query):
    if len(query) > 4096:
        raise ValueError('query too long')
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    allowed = {'mode', 'start', 'check_in', 'check_out', 'room_type', 'bed', 'adults',
               'child_ages', 'allow_upgrade', 'allow_extra_bed', 'budget'}
    if set(values) - allowed or any(len(v) != 1 or not v[0] for v in values.values()):
        raise ValueError('unknown, duplicate or empty parameter')
    return {key: value[0] for key, value in values.items()}


def view(workspace, params):
    mode = params.get('mode', 'strict')
    with workspace._lock:
        store = workspace._store(mode)
        with store.read_snapshot() as connection:
            config, stored_mode = store._configuration(connection)
            engine = store._planner(connection, config, stored_mode)
            start = parse_date(params.get('start', config['horizon_start']))
            if start.year > 9998:
                raise ValueError('start date too late')
            daily = {row['date']: row for row in engine.snapshot()['daily']}
            minimum_rank = min(t.rank for t in engine.types.values())
            rows = []
            for offset in range(14):
                day = start + timedelta(days=offset)
                source = daily.get(day.isoformat())
                row = {'date': day.isoformat(), 'available': source is not None, 'types': []}
                if source is not None:
                    for typ in engine.types.values():
                        high = typ.rank > minimum_rank
                        configured = engine.protection.get((typ.code, day), 0) if high else 0
                        row['types'].append({'room_type': typ.code, 'remaining': source['available_by_type'][typ.code],
                            'configured_protection': configured,
                            'active_protection': configured if mode == 'protected_upgrade' else 0})
                    row['extra_beds_remaining'] = source['extra_beds_capacity'] - source['extra_beds_used']
                    row['cots_remaining'] = source['cots_capacity'] - source['cots_used']
                else:
                    row['reason'] = 'OUTSIDE_INVENTORY_HORIZON'
                rows.append(row)
            result = {'mode': mode, 'generation': workspace.generation,
                'snapshot_at': datetime.now(timezone.utc).isoformat(), 'days': rows,
                'horizon_start': config['horizon_start'], 'horizon_end': config['horizon_end'],
                'room_types': list(engine.types), 'bed_types': sorted({r.bed_type for r in engine.rooms}),
                'read_only': True, 'synthetic': True, 'preview': None}
            proposal_keys = set(params) - {'mode', 'start'}
            if proposal_keys:
                if not {'check_in', 'check_out', 'room_type', 'bed', 'adults'} <= set(params):
                    raise ValueError('complete stay dates, room type, bed and adults required')
                if params['bed'] not in result['bed_types']:
                    raise ValueError('unknown bed type')
                def flag(name):
                    value = params.get(name, 'true')
                    if value not in ('true', 'false'):
                        raise ValueError('allow flags must be true or false')
                    return value == 'true'
                raw_children = params.get('child_ages')
                if raw_children is not None and any(not age.strip() for age in raw_children.split(',')):
                    raise ValueError('child ages must be comma-separated integers')
                children = tuple(int(age) for age in raw_children.split(',')) if raw_children else ()
                if len(children) > 10:
                    raise ValueError('too many children')
                request = BookingRequest('__readonly_preview__', parse_date(params['check_in']), parse_date(params['check_out']),
                    params['room_type'], (params['bed'],), int(params['adults']), children,
                    flag('allow_upgrade'), flag('allow_extra_bed'), int(params.get('budget', '100000')))
                decision = engine.allocate(request)
                # Never return occupant identities, booking ledger or request identifiers.
                result['preview'] = {key: value for key, value in decision.to_dict().items() if key != 'request_id'}
            return result
