"""Read-only reconciliation using independent recomputation and event replay.

Does not call the greedy allocator, rewrite data, or claim tamper-proof history.
"""
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import json


def audit(connection, config, mode, limit):
    issues = []
    def problem(code, detail=''):
        issues.append(code + (':' + str(detail) if detail else ''))
    checks = [row[0] for row in connection.execute('PRAGMA integrity_check')]
    if checks != ['ok']:
        problem('sqlite_integrity_check', checks)
    for row in connection.execute('PRAGMA foreign_key_check'):
        problem('foreign_key_violation', row[0])
    start, end = date.fromisoformat(config['horizon_start']), date.fromisoformat(config['horizon_end'])
    days = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days)]
    rooms = {r['room_id']: r for r in config['rooms']}
    types = {t['code']: t for t in config['room_types']}
    resources = {r['date']: r for r in config['daily_resources']}
    rates = {(r['room_type'], r['date']): r['rate_cents'] for r in config['daily_rates']}
    protection = {(r['room_type'], r['date']): r['count'] for r in config['protection']}
    expected_calendar = {(room['room_id'], day, room['room_type'], int(day in room.get('blocked_dates', [])))
                         for room in rooms.values() for day in days}
    actual_calendar = {tuple(row) for row in connection.execute('SELECT room_id,night,room_type,blocked FROM room_calendar')}
    if actual_calendar != expected_calendar:
        problem('calendar_configuration_mismatch')
    inventory = {row['night']: dict(row) for row in connection.execute('SELECT * FROM auxiliary_inventory')}
    if set(inventory) != set(days):
        problem('inventory_date_mismatch')
    for day, row in inventory.items():
        if day not in resources or (row['cot_capacity'], row['extra_capacity']) != (resources[day]['cots'], resources[day]['extra_beds']):
            problem('capacity_configuration_mismatch', day)
    bookings = {row['request_id']: dict(row) for row in connection.execute('SELECT * FROM bookings')}
    expected_history = set()
    decoded = {}
    for key, row in bookings.items():
        try:
            req, dec = json.loads(row['request_json']), json.loads(row['decision_json'])
            if hashlib.sha256(row['request_json'].encode()).hexdigest() != row['request_sha256']:
                problem('request_checksum_mismatch', key)
            if req['request_id'] != key or dec['request_id'] != key:
                problem('request_identity_mismatch', key)
            check_in, check_out = date.fromisoformat(req['check_in']), date.fromisoformat(req['check_out'])
            length = (check_out-check_in).days
            expected_days = [(check_in+timedelta(days=i)).isoformat() for i in range(length)]
            if not 1 <= length <= 30 or dec['nights'] != expected_days:
                problem('request_nights_mismatch', key)
            accepted = dec['accepted'] is True
            if accepted != (row['status'] in ('BOOKED', 'CANCELLED')):
                problem('decision_status_mismatch', key)
            decoded[key] = (req, dec)
            if not accepted:
                continue
            room = rooms[dec['room_id']]
            typ = types[room['room_type']]
            ages, adults = req['child_ages'], req['adults']
            if type(adults) is not int or adults < 1 or any(type(a) is not int or not 0 <= a <= 17 for a in ages):
                problem('invalid_party', key)
            cots = sum(age < config['cot_age_limit'] for age in ages)
            extras = max(0, adults+len(ages)-cots-room['fixed_bed_places'])
            if (adults > typ['max_adults'] or adults+len(ages) > typ['max_occupants']
                    or cots > typ['max_cots'] or extras > typ['max_extra_beds']
                    or cots+extras > typ.get('max_auxiliary_items', 2) or (extras and not req['allow_extra_bed'])):
                problem('party_constraints_violated', key)
            if dec['cots'] != cots or dec['extra_beds'] != extras:
                problem('decision_resource_mismatch', key)
            if room['bed_type'] not in req['accepted_bed_types'] or dec['bed_type'] != room['bed_type']:
                problem('bed_type_mismatch', key)
            if dec['assigned_type'] != room['room_type']:
                problem('assigned_type_mismatch', key)
            upgraded = room['room_type'] != req['requested_type']
            if dec['upgraded'] != upgraded:
                problem('upgrade_flag_mismatch', key)
            if upgraded and (mode == 'strict' or not req['allow_upgrade']
                    or room['room_type'] not in config['allowed_upgrades'].get(req['requested_type'], [])):
                problem('unauthorized_upgrade', key)
            cost = sum(max(0, rates.get((room['room_type'], day),typ['base_rate_cents'])
                       - rates.get((req['requested_type'], day),types[req['requested_type']]['base_rate_cents']))
                       for day in expected_days) if upgraded else 0
            if cost != dec['upgrade_cost_cents'] or cost > req['max_upgrade_cost_cents']:
                problem('upgrade_cost_mismatch', key)
            if any(day not in days or day in room.get('blocked_dates', []) for day in expected_days):
                problem('unavailable_calendar_night', key)
            for day in expected_days:
                expected_history.add((key, room['room_id'], day, cots, extras))
        except (ValueError, KeyError, TypeError) as exc:
            problem('malformed_booking', key)
    actual_history = {tuple(row) for row in connection.execute('SELECT request_id,room_id,night,cots,extra_beds FROM booking_nights')}
    if actual_history != expected_history:
        problem('historical_nights_mismatch')
    expected_active = {(rid, night, key) for key, rid, night, _, _ in expected_history if bookings[key]['status'] == 'BOOKED'}
    actual_active = {tuple(row) for row in connection.execute('SELECT room_id,night,request_id FROM active_room_nights')}
    if actual_active != expected_active:
        problem('active_nights_mismatch')
    expected_totals = defaultdict(lambda: [0, 0])
    for key, _, night, cots, extras in expected_history:
        if bookings[key]['status'] == 'BOOKED':
            expected_totals[night][0] += cots
            expected_totals[night][1] += extras
    for night, row in inventory.items():
        if [row['cots_used'], row['extras_used']] != expected_totals[night]:
            problem('resource_counter_mismatch', night)
        if not (0 <= row['cots_used'] <= row['cot_capacity'] and 0 <= row['extras_used'] <= row['extra_capacity']):
            problem('resource_bounds_violation', night)
    events = list(connection.execute('SELECT * FROM reservation_events ORDER BY sequence'))
    by_booking = defaultdict(list)
    replay_rooms = {}
    replay_resources = defaultdict(lambda: [0, 0])
    historical_by_id = defaultdict(list)
    for key, rid, night, cots, extras in expected_history:
        historical_by_id[key].append((rid, night, cots, extras))
    for event in events:
        key, kind = event['request_id'], event['event_type']
        by_booking[key].append(kind)
        if key not in decoded:
            problem('event_missing_decision', key)
            continue
        req, dec = decoded[key]
        nights = historical_by_id[key]
        if kind == 'BOOKED':
            for rid, night, cots, extras in nights:
                if (rid, night) in replay_rooms:
                    problem('event_replay_double_booking', key)
                if dec['upgraded'] and mode == 'protected_upgrade':
                    typ = rooms[rid]['room_type']
                    remaining = sum(r['room_type'] == typ and night not in r.get('blocked_dates', [])
                                    and (r['room_id'], night) not in replay_rooms for r in rooms.values())
                    if remaining - 1 < protection.get((typ, night), 0):
                        problem('event_replay_protection_violation', key)
                replay_rooms[rid, night] = key
                replay_resources[night][0] += cots
                replay_resources[night][1] += extras
        elif kind == 'CANCELLED':
            for rid, night, cots, extras in nights:
                if replay_rooms.get((rid, night)) != key:
                    problem('event_replay_invalid_release', key)
                else:
                    del replay_rooms[rid, night]
                replay_resources[night][0] -= cots
                replay_resources[night][1] -= extras
        for _, night, _, _ in nights:
            if night not in resources or not (0 <= replay_resources[night][0] <= resources[night]['cots']
                                             and 0 <= replay_resources[night][1] <= resources[night]['extra_beds']):
                problem('event_replay_resource_violation', key)
    for key, row in bookings.items():
        wanted = {'BOOKED':['BOOKED'], 'CANCELLED':['BOOKED','CANCELLED'], 'REJECTED':['REJECTED']}[row['status']]
        if by_booking[key] != wanted:
            problem('event_sequence_mismatch', key)
    replay_active = {(rid, night, key) for (rid, night), key in replay_rooms.items()}
    if replay_active != actual_active:
        problem('event_replay_active_mismatch')
    return {'healthy': not issues, 'verification_complete': True, 'issue_count': len(issues), 'issues': issues[:limit], 'truncated': len(issues)>limit,
            'booking_status_counts': dict(sorted(Counter(row['status'] for row in bookings.values()).items())),
            'active_room_nights': len(actual_active), 'historical_room_nights': len(actual_history),
            'events': len(events), 'sqlite_version': connection.execute('SELECT sqlite_version()').fetchone()[0]}
