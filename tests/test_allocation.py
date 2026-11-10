from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
import random
import threading
import unittest
from hotel_sim import Allocator, BookingRequest, Policy, Room, RoomType
from hotel_sim.allocation import nights_between

DAY = date(2026,12,7)
DAYS = tuple(DAY+timedelta(days=i) for i in range(4))
TYPES = (RoomType('S',0,4,3,2,2,100,3), RoomType('D',1,5,4,2,2,200,3), RoomType('F',2,6,5,2,2,300,3))

def engine(rooms=None, types=TYPES, mode='upgrade', protection=(), cots=None, extras=None, **kwargs):
    return Allocator(types, rooms if rooms is not None else (Room('S1','S','king',2),),
        horizon_start=DAY,horizon_end=DAY+timedelta(days=4),
        cot_capacity=cots if cots is not None else dict.fromkeys(DAYS,2),
        extra_bed_capacity=extras if extras is not None else dict.fromkeys(DAYS,2),
        upgrades=kwargs.pop('upgrades',{'S':('D','F'),'D':('F',)}), policy=Policy(mode,protection), **kwargs)

def req(key='A',start=0,end=1,**kwargs):
    defaults={'requested_type':'S','accepted_bed_types':('king',),'adults':2}
    defaults.update(kwargs)
    return BookingRequest(key,DAY+timedelta(days=start),DAY+timedelta(days=end),**defaults)

class AllocationTests(unittest.TestCase):
    def test_half_open_adjacent_stays(self):
        e=engine();self.assertTrue(e.allocate(req()).accepted);self.assertTrue(e.allocate(req('B',1,2)).accepted)
        self.assertEqual(e.snapshot()['occupied_room_nights'],2)

    def test_cross_year_dates(self):
        self.assertEqual(nights_between(date(2026,12,31),date(2027,1,2)),(date(2026,12,31),date(2027,1,1)))

    def test_overlap_no_overbooking(self):
        e=engine();e.allocate(req(end=2));self.assertFalse(e.allocate(req('B',1,3)).accepted)
        self.assertEqual(e.snapshot()['occupied_room_nights'],2)

    def test_daily_fragments_do_not_fake_one_contiguous_room(self):
        e=engine((Room('S1','S','king',2,frozenset({DAYS[1]})),Room('S2','S','king',2,frozenset({DAYS[0]}))))
        before=e.snapshot();d=e.allocate(req(end=2))
        self.assertIn('NO_SAME_ROOM_FOR_ENTIRE_STAY',d.rejection_reasons);self.assertEqual(before,e.snapshot())

    def test_cot_shortage_on_later_night_is_atomic(self):
        e=engine(cots={**dict.fromkeys(DAYS,1),DAYS[1]:0});before=e.snapshot()
        d=e.allocate(req(end=2,child_ages=(1,)))
        self.assertIn('AUXILIARY_INVENTORY_EXHAUSTED',d.rejection_reasons);self.assertEqual(before,e.snapshot())

    def test_cots_shared_across_room_types(self):
        e=engine((Room('S1','S','king',2),Room('D1','D','king',2)),cots=dict.fromkeys(DAYS,1))
        self.assertTrue(e.allocate(req(child_ages=(0,))).accepted)
        self.assertFalse(e.allocate(req('B',requested_type='D',child_ages=(1,))).accepted)
        self.assertEqual(e.snapshot()['daily'][0]['cots_used'],1)

    def test_infant_counts_toward_total_occupancy(self):
        e=engine(types=(replace(TYPES[0],max_occupants=2,max_adults=2),*TYPES[1:]))
        self.assertFalse(e.allocate(req(child_ages=(0,))).accepted)

    def test_child_age_boundaries(self):
        for age,cots,extras in ((0,1,0),(1,1,0),(2,0,1),(17,0,1)):
            with self.subTest(age=age):
                d=engine().allocate(req(child_ages=(age,)));self.assertTrue(d.accepted)
                self.assertEqual((d.cots,d.extra_beds),(cots,extras))

    def test_configurable_cot_age_limit(self):
        self.assertEqual(engine(cot_age_limit=3).allocate(req(child_ages=(2,))).cots,1)

    def test_cot_rollaway_combination_limit(self):
        e=engine(types=(replace(TYPES[0],max_auxiliary_items=1),*TYPES[1:]))
        self.assertFalse(e.allocate(req(child_ages=(1,6))).accepted)

    def test_extra_bed_requires_guest_acceptance(self):
        self.assertFalse(engine().allocate(req(child_ages=(6,),allow_extra_bed=False)).accepted)

    def test_adult_limit_separate_from_total(self):
        self.assertFalse(engine(types=(replace(TYPES[0],max_adults=1),*TYPES[1:])).allocate(req()).accepted)

    def test_twin_cannot_silently_upgrade_to_king(self):
        d=engine((Room('D1','D','king',2),)).allocate(req(accepted_bed_types=('twin',)))
        self.assertFalse(d.accepted);self.assertEqual(d.rejection_reasons,('BED_TYPE_INCOMPATIBLE',))

    def test_explicit_upgrade_policy_and_guest_acceptance(self):
        for mode,permission,success in (('strict',True,False),('upgrade',False,False),('upgrade',True,True)):
            with self.subTest(mode=mode,permission=permission):
                d=engine((Room('D1','D','king',2),),mode=mode).allocate(req(allow_upgrade=permission))
                self.assertEqual(d.accepted,success)

    def test_upgrade_paths_not_transitive(self):
        e=engine((Room('F1','F','king',2),),upgrades={'S':('D',),'D':('F',)})
        self.assertFalse(e.allocate(req()).accepted)

    def test_budget_exact_whole_stay_sum(self):
        for budget,success in ((400,True),(399,False)):
            with self.subTest(budget=budget):
                e=engine((Room('D1','D','king',2),),rates={('D',DAYS[1]):400});before=e.snapshot()
                d=e.allocate(req(end=2,max_upgrade_cost_cents=budget));self.assertEqual(d.accepted,success)
                if success:self.assertEqual(d.upgrade_cost_cents,400)
                else:self.assertEqual(before,e.snapshot())

    def test_protection_checked_every_night(self):
        e=engine((Room('D1','D','king',2),Room('D2','D','king',2,frozenset({DAYS[1]}))),
                 mode='protected_upgrade',protection=(('D',DAYS[0],1),('D',DAYS[1],1)))
        d=e.allocate(req(end=2));self.assertIn('PROTECTED_INVENTORY',d.rejection_reasons)
        self.assertEqual(e.snapshot()['occupied_room_nights'],0)

    def test_exact_demand_can_use_protected_inventory(self):
        e=engine((Room('D1','D','king',2),),mode='protected_upgrade',protection=(('D',DAYS[0],1),))
        self.assertFalse(e.allocate(req()).accepted)
        self.assertTrue(e.allocate(req('B',requested_type='D')).accepted)

    def test_failed_candidate_does_not_block_later_candidate(self):
        e=engine((Room('D1','D','king',2,frozenset({DAYS[1]})),Room('F1','F','king',2)))
        d=e.allocate(req(end=2));self.assertEqual(d.assigned_type,'F')
        self.assertEqual(e.snapshot()['daily'][0]['available_by_type']['D'],1)

    def test_exact_type_first_then_stable_room_id(self):
        rooms=(Room('S2','S','king',2),Room('D1','D','king',2),Room('S1','S','king',2))
        self.assertEqual(engine(rooms).allocate(req()).room_id,'S1')
        self.assertEqual(engine(reversed(rooms)).allocate(req()).room_id,'S1')

    def test_idempotent_repeat_and_changed_content_conflict(self):
        e=engine();first=e.allocate(req());before=e.snapshot();self.assertEqual(e.allocate(req()),first)
        with self.assertRaises(ValueError):e.allocate(req(end=2))
        self.assertEqual(before,e.snapshot())

    def test_horizon_overrun_rejected_not_truncated(self):
        e=engine();d=e.allocate(req(start=3,end=5))
        self.assertEqual(d.rejection_reasons,('OUTSIDE_INVENTORY_HORIZON',))
        self.assertEqual(e.snapshot()['occupied_room_nights'],0)

    def test_twenty_threads_last_room(self):
        e,b=engine(),threading.Barrier(20)
        def buy(i):b.wait(timeout=10);return e.allocate(req(str(i))).accepted
        with ThreadPoolExecutor(max_workers=20) as p:self.assertEqual(sum(p.map(buy,range(20))),1)
        self.assertEqual(e.snapshot()['occupied_room_nights'],1)

    def test_concurrent_idempotent_retries(self):
        e,b=engine(),threading.Barrier(10)
        def buy(i):b.wait(timeout=10);return e.allocate(req())
        with ThreadPoolExecutor(max_workers=10) as p:results=list(p.map(buy,range(10)))
        self.assertTrue(all(d==results[0] for d in results));self.assertEqual(e.snapshot()['occupied_room_nights'],1)

    def test_counterexample_upgrades_can_reduce_accepted_count(self):
        rooms=(Room('D1','D','king',2),)
        requests=[req(end=2),req('B',requested_type='D'),req('C',1,2,requested_type='D')]
        groups=[engine(rooms,mode='strict').allocate_many(requests),engine(rooms).allocate_many(requests),
                engine(rooms,mode='protected_upgrade',protection=(('D',DAYS[0],1),('D',DAYS[1],1))).allocate_many(requests)]
        self.assertEqual([sum(d.accepted for d in group) for group in groups],[2,1,2])

    def test_invalid_requests(self):
        cases=({'adults':True},{'adults':0},{'child_ages':(-1,)},{'child_ages':(18,)},{'child_ages':(True,)},
               {'child_ages':[1]},{'accepted_bed_types':()},{'accepted_bed_types':('king','king')},
               {'allow_upgrade':1},{'max_upgrade_cost_cents':-1})
        for case in cases:
            with self.subTest(case=case),self.assertRaises(ValueError):req(**case)
        for start,end in ((DAY,DAY),(DAY,DAY-timedelta(days=1)),(DAY,DAY+timedelta(days=31)),(datetime(2026,12,7),DAY)):
            with self.subTest(start=start,end=end),self.assertRaises(ValueError):nights_between(start,end)

    def test_invalid_configuration(self):
        cases=[lambda:engine(cots={DAY:1}),lambda:engine(extras=dict.fromkeys(DAYS,-1)),
               lambda:engine((Room('X','unknown','king',2),)),
               lambda:engine((Room('S1','S','king',2),Room('S1','S','king',2))),
               lambda:engine(upgrades={'D':('S',)}),lambda:engine(upgrades={'S':'DF'}),lambda:engine(protection=(('missing',DAY,1),)),
               lambda:engine(rates={('S',DAY):-1}),lambda:engine(cot_age_limit=True)]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(ValueError):case()

    def test_inputs_and_snapshots_are_detached(self):
        capacities=dict.fromkeys(DAYS,1);e=engine(cots=capacities);capacities[DAY]=0
        self.assertTrue(e.allocate(req(child_ages=(0,))).accepted)
        state=e.snapshot();state['daily'][0]['cots_used']=999
        self.assertEqual(e.snapshot()['daily'][0]['cots_used'],1)

    def test_randomized_invariants_200_scenarios(self):
        for seed in range(200):
            rng=random.Random(seed)
            rooms=tuple(Room(f'{typ}{i}',typ,bed,places,frozenset(day for day in DAYS if rng.random()<.12))
                        for typ,bed,places in (('S','king',2),('D','twin',2),('F','family',4)) for i in range(2))
            e=engine(rooms,mode=('strict','upgrade','protected_upgrade')[seed%3],protection=(('F',DAYS[1],1),))
            requests=[]
            for i in range(30):
                start=rng.randrange(4)
                requests.append(req(str(i),start,rng.randrange(start+1,5),requested_type=rng.choice(('S','D','F')),
                    adults=rng.randrange(1,4),child_ages=tuple(rng.choice((0,1,2,8,17)) for _ in range(rng.randrange(3))),
                    accepted_bed_types=rng.choice((('king',),('twin',),('family',),('king','twin','family')))))
            decisions=e.allocate_many(requests);state=e.snapshot();accepted=[d for d in decisions if d.accepted]
            self.assertEqual(state['occupied_room_nights'],sum(len(d.nights) for d in accepted))
            self.assertEqual(len({(x['room_id'],x['date']) for x in state['occupancy']}),state['occupied_room_nights'])
            for day in state['daily']:
                self.assertTrue(all(n>=0 for n in day['available_by_type'].values()))
                for resource in ('cots','extra_beds'):
                    self.assertLessEqual(day[resource+'_used'],day[resource+'_capacity'])
                    self.assertEqual(day[resource+'_used'],sum(getattr(d,resource) for d in accepted if day['date'] in d.nights))
            for r,d in zip(requests,decisions):
                if not d.accepted:continue
                room=next(room for room in rooms if room.room_id==d.room_id)
                self.assertIn(room.bed_type,r.accepted_bed_types)
                self.assertLessEqual(r.adults+len(r.child_ages),e.types[room.room_type].max_occupants)
                self.assertTrue(all(date.fromisoformat(day) not in room.blocked_dates for day in d.nights))
                self.assertLessEqual(d.upgrade_cost_cents,r.max_upgrade_cost_cents)
                if d.upgraded:self.assertIn(d.assigned_type,e.upgrades[r.requested_type])

if __name__=='__main__':unittest.main()
