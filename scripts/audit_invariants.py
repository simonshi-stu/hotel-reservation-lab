"""Independent per-operation invariant audit of the candidate (no source edits)."""
from datetime import date, timedelta
from random import Random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hotel_sim import Allocator, BookingRequest, Policy, Room, RoomType

BASE = date(2026, 12, 28)
DAYS = tuple(BASE + timedelta(days=i) for i in range(7))
TYPES = (RoomType('S', 0, 4, 3, 2, 2, 100, 3),
         RoomType('D', 1, 5, 4, 2, 2, 200, 3),
         RoomType('F', 2, 6, 5, 2, 2, 300, 3))
operations = 0
for seed in range(500):
    rng = Random(seed)
    rooms = tuple(Room(f'{typ}{i}', typ, rng.choice(('king', 'twin')), rng.choice((1, 2, 3)),
                       frozenset(d for d in DAYS if rng.random() < .15))
                  for typ in ('S', 'D', 'F') for i in range(3))
    cots = {d: rng.randrange(4) for d in DAYS}
    extras = {d: rng.randrange(4) for d in DAYS}
    reserve = tuple((typ, d, rng.randrange(2)) for typ in ('S', 'D', 'F') for d in DAYS)
    mode = ('strict', 'upgrade', 'protected_upgrade')[seed % 3]
    cutoff = rng.choice((1, 2, 3))
    rates = {(typ, d): rng.randrange(50, 500) for typ in ('S', 'D', 'F') for d in DAYS}
    args = dict(horizon_start=DAYS[0], horizon_end=DAYS[-1]+timedelta(days=1),
                cot_capacity=cots, extra_bed_capacity=extras, upgrades={'S': ('D', 'F'), 'D': ('F',)},
                policy=Policy(mode, reserve), cot_age_limit=cutoff, rates=rates)
    e = Allocator(TYPES, rooms, **args)
    requests = []
    accepted = []
    for i in range(40):
        start = rng.randrange(-1, 8)
        end = start + rng.randrange(1, 5)
        r = BookingRequest(str(i), BASE+timedelta(days=start), BASE+timedelta(days=end),
                           rng.choice(('S','D','F')), rng.choice((('king',),('twin',),('king','twin'))),
                           rng.randrange(1,5), tuple(rng.randrange(18) for _ in range(rng.randrange(4))),
                           bool(rng.randrange(2)), bool(rng.randrange(2)), rng.randrange(600))
        requests.append(r)
        before = e.snapshot()
        d = e.allocate(r)
        after = e.snapshot()
        operations += 1
        assert e.allocate(r) == d
        assert e.snapshot() == after, 'idempotent repeat changed inventory'
        assert d.nights == tuple((r.check_in+timedelta(days=n)).isoformat() for n in range((r.check_out-r.check_in).days))
        if not d.accepted:
            assert before == after, 'rejection changed inventory'
            continue
        accepted.append((r,d))
        room = next(x for x in rooms if x.room_id == d.room_id)
        typ = next(x for x in TYPES if x.code == room.room_type)
        assert r.adults <= typ.max_adults
        assert r.adults + len(r.child_ages) <= typ.max_occupants
        assert d.cots == sum(x < cutoff for x in r.child_ages)
        sleepers = r.adults + len(r.child_ages) - d.cots
        assert d.extra_beds == max(0, sleepers - room.fixed_bed_places)
        assert d.cots <= typ.max_cots and d.extra_beds <= typ.max_extra_beds
        assert d.cots + d.extra_beds <= typ.max_auxiliary_items
        assert d.extra_beds == 0 or r.allow_extra_bed
        assert room.bed_type in r.accepted_bed_types
        assert all(date.fromisoformat(n) not in room.blocked_dates for n in d.nights)
        assert d.upgraded == (room.room_type != r.requested_type)
        assert d.upgrade_cost_cents <= r.max_upgrade_cost_cents
        if d.upgraded:
            assert r.allow_upgrade and mode != 'strict'
            assert room.room_type in args['upgrades'].get(r.requested_type, ())
            assert d.upgrade_cost_cents == sum(max(0, rates[room.room_type, date.fromisoformat(n)] - rates[r.requested_type, date.fromisoformat(n)]) for n in d.nights)
        assert after['occupied_room_nights'] - before['occupied_room_nights'] == len(d.nights)
        for old, new in zip(before['daily'], after['daily']):
            affected = new['date'] in d.nights
            assert new['cots_used'] - old['cots_used'] == (d.cots if affected else 0)
            assert new['extra_beds_used'] - old['extra_beds_used'] == (d.extra_beds if affected else 0)
            assert new['cots_used'] <= new['cots_capacity']
            assert new['extra_beds_used'] <= new['extra_beds_capacity']
            if mode == 'protected_upgrade' and d.upgraded and affected:
                assert new['available_by_type'][room.room_type] >= e.protection[room.room_type,date.fromisoformat(new['date'])]
        assert len({(x['room_id'],x['date']) for x in after['occupancy']}) == after['occupied_room_nights']
        assert after['occupied_room_nights'] == sum(len(dec.nights) for _,dec in accepted)
    replay = Allocator(TYPES, reversed(rooms), **args)
    original = [e.allocate(r) for r in requests]
    assert replay.allocate_many(requests) == original
    assert replay.snapshot() == e.snapshot()
print(f'PASS: {operations} primary allocations across 500 deterministic scenarios; idempotent retry of every decision; reversed-room-order replay; per-operation inventory/resource/capacity/budget/protection audits.')
