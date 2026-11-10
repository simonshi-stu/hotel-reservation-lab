from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from hotel_sim.scenario import load_scenario, parse_requests, build_allocator
from hotel_sim.sqlite_store import (SQLiteHotel, BookingNotFound, InvalidBookingState,
    IdempotencyConflict, DatabaseBusy, DataIntegrityError)
from test_allocation import DAY, DAYS, TYPES, req

ROOT=Path(__file__).resolve().parent.parent

def configuration(rooms=1):
    return {'horizon_start':DAY.isoformat(),'horizon_end':(DAY+timedelta(days=4)).isoformat(),
        'room_types':[asdict(t) for t in TYPES],
        'rooms':[{'room_id':f'S{i}','room_type':'S','bed_type':'king','fixed_bed_places':2,'blocked_dates':[]} for i in range(rooms)],
        'daily_resources':[{'date':d.isoformat(),'cots':2,'extra_beds':2} for d in DAYS],
        'allowed_upgrades':{'S':['D','F'],'D':['F']},'daily_rates':[],'protection':[],'cot_age_limit':2}

class SQLiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'hotel.db';self.db=SQLiteHotel(self.path)
        self.config=configuration();self.db.initialize(self.config,'upgrade')

    def count(self,table):
        with self.db.read_snapshot() as c:return c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def test_persistence_reopen_and_cancel_release(self):
        first=self.db.reserve(req(end=3,child_ages=(1,5)))
        self.assertEqual(first['status'],'BOOKED')
        opened=SQLiteHotel(self.path)
        self.assertEqual(opened.get_booking('A'),first)
        self.assertEqual(opened.snapshot()['occupied_room_nights'],3)
        self.assertTrue(opened.reconcile()['healthy'])
        cancelled=opened.cancel('A');self.assertEqual(cancelled['status'],'CANCELLED')
        self.assertEqual(opened.snapshot()['occupied_room_nights'],0)
        self.assertEqual(self.count('booking_nights'),3)
        self.assertEqual(self.count('reservation_events'),2)
        self.assertTrue(opened.reconcile()['healthy'])
        for day in opened.snapshot()['daily']:
            self.assertEqual((day['cots_used'],day['extra_beds_used']),(0,0))

    def test_cancelled_original_request_does_not_rebook(self):
        self.db.reserve(req());self.db.cancel('A');before=self.db.snapshot()
        replay=self.db.reserve(req())
        self.assertEqual(replay['status'],'CANCELLED');self.assertTrue(replay['original_decision']['accepted'])
        self.assertEqual(before,self.db.snapshot());self.assertEqual(self.count('reservation_events'),2)
        self.assertEqual(self.db.reserve(req('B'))['status'],'BOOKED')

    def test_idempotency_semantic_array_order_and_conflict(self):
        a=req(child_ages=(1,5),accepted_bed_types=('king','twin'))
        original=self.db.reserve(a)
        self.assertEqual(self.db.reserve(replace(a,child_ages=(5,1),accepted_bed_types=('twin','king'))),original)
        with self.assertRaises(IdempotencyConflict):self.db.reserve(replace(a,adults=1))
        self.assertEqual(self.count('bookings'),1);self.assertEqual(self.count('reservation_events'),1)

    def test_rejected_request_is_persistent_without_inventory_changes(self):
        self.db.reserve(req());before=self.db.snapshot();failed=self.db.reserve(req('B'))
        self.assertEqual(failed['status'],'REJECTED');self.assertEqual(before,self.db.snapshot())
        self.db.cancel('A')
        self.assertEqual(self.db.reserve(req('B'))['status'],'REJECTED')
        with self.assertRaises(InvalidBookingState):self.db.cancel('B')
        self.assertEqual(self.db.reserve(req('C'))['status'],'BOOKED')
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_adjacent_dates_use_half_open_interval(self):
        self.assertEqual(self.db.reserve(req())['status'],'BOOKED')
        self.assertEqual(self.db.reserve(req('B',1,2))['status'],'BOOKED')
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_rejection_on_later_night_has_no_partial_commit(self):
        self.db.reserve(req('existing',1,2));before=self.db.snapshot()
        self.assertEqual(self.db.reserve(req('A',0,3))['status'],'REJECTED')
        self.assertEqual(before,self.db.snapshot());self.assertEqual(self.count('booking_nights'),1)
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_mid_reserve_fault_rolls_back_every_table_then_retries(self):
        before=self.db.snapshot()
        def fault(stage,c):
            if stage=='reserve_after_night':raise RuntimeError('synthetic fault')
        self.db._checkpoint=fault
        with self.assertRaisesRegex(RuntimeError,'synthetic'):self.db.reserve(req(end=3,child_ages=(1,)))
        self.db._checkpoint=lambda *args:None
        self.assertEqual(before,self.db.snapshot())
        for table in ('bookings','booking_nights','active_room_nights','reservation_events'):self.assertEqual(self.count(table),0)
        self.assertEqual(self.db.reserve(req(end=3,child_ages=(1,)))['status'],'BOOKED')
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_before_commit_fault_rolls_back_event_too(self):
        def fault(stage,c):
            if stage=='reserve_before_commit':raise RuntimeError('synthetic fault')
        self.db._checkpoint=fault
        with self.assertRaises(RuntimeError):self.db.reserve(req())
        self.assertEqual(self.count('bookings'),0);self.assertEqual(self.count('reservation_events'),0)

    def test_after_reserve_commit_response_loss_retry_once(self):
        def fault(stage,c):
            if stage=='reserve_after_commit':raise RuntimeError('lost response')
        self.db._checkpoint=fault
        with self.assertRaises(RuntimeError):self.db.reserve(req(end=2))
        self.db._checkpoint=lambda *args:None
        self.assertEqual(self.db.reserve(req(end=2))['status'],'BOOKED')
        self.assertEqual(self.count('bookings'),1);self.assertEqual(self.count('reservation_events'),1)
        self.assertEqual(self.count('active_room_nights'),2)

    def test_mid_cancel_fault_restores_all_resources_and_state(self):
        self.db.reserve(req(end=3,child_ages=(1,5)));before=self.db.snapshot()
        def fault(stage,c):
            if stage=='cancel_after_release':raise RuntimeError('synthetic release fault')
        self.db._checkpoint=fault
        with self.assertRaises(RuntimeError):self.db.cancel('A')
        self.db._checkpoint=lambda *args:None
        self.assertEqual(before,self.db.snapshot());self.assertEqual(self.db.get_booking('A')['status'],'BOOKED')
        self.assertEqual(self.count('reservation_events'),1);self.assertTrue(self.db.reconcile()['healthy'])
        self.db.cancel('A');self.assertTrue(self.db.reconcile()['healthy'])

    def test_after_cancel_commit_response_loss_is_idempotent(self):
        self.db.reserve(req(end=2,child_ages=(1,)))
        def fault(stage,c):
            if stage=='cancel_after_commit':raise RuntimeError('lost response')
        self.db._checkpoint=fault
        with self.assertRaises(RuntimeError):self.db.cancel('A')
        self.db._checkpoint=lambda *args:None
        self.assertEqual(self.db.cancel('A')['status'],'CANCELLED')
        self.assertEqual(self.count('reservation_events'),2);self.assertEqual(self.count('active_room_nights'),0)
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_twenty_independent_stores_compete_for_last_room(self):
        barrier=threading.Barrier(20)
        def buy(i):barrier.wait(timeout=10);return SQLiteHotel(self.path).reserve(req(str(i)))['status']
        with ThreadPoolExecutor(max_workers=20) as pool:statuses=list(pool.map(buy,range(20)))
        self.assertEqual(statuses.count('BOOKED'),1);self.assertEqual(statuses.count('REJECTED'),19)
        self.assertEqual(self.count('active_room_nights'),1);self.assertTrue(self.db.reconcile()['healthy'])

    def test_concurrent_same_request_key_commits_once(self):
        barrier=threading.Barrier(10)
        def buy(i):barrier.wait(timeout=10);return SQLiteHotel(self.path).reserve(req(end=3))
        with ThreadPoolExecutor(max_workers=10) as pool:results=list(pool.map(buy,range(10)))
        self.assertTrue(all(result==results[0] for result in results))
        self.assertEqual(self.count('bookings'),1);self.assertEqual(self.count('reservation_events'),1)
        self.assertEqual(self.count('active_room_nights'),3)

    def test_concurrent_cancels_release_once(self):
        self.db.reserve(req(end=3,child_ages=(1,5)));barrier=threading.Barrier(10)
        def cancel(i):barrier.wait(timeout=10);return SQLiteHotel(self.path).cancel('A')['status']
        with ThreadPoolExecutor(max_workers=10) as pool:self.assertEqual(list(pool.map(cancel,range(10))),['CANCELLED']*10)
        self.assertEqual(self.count('reservation_events'),2);self.assertEqual(self.count('active_room_nights'),0)
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_cancel_reserve_race_has_serializable_outcome(self):
        self.db.reserve(req());barrier=threading.Barrier(2)
        def cancel():barrier.wait(timeout=10);return SQLiteHotel(self.path).cancel('A')
        def book():barrier.wait(timeout=10);return SQLiteHotel(self.path).reserve(req('B'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=pool.submit(cancel),pool.submit(book);a.result();result=b.result()
        self.assertIn(result['status'],('BOOKED','REJECTED'))
        self.assertEqual(self.count('active_room_nights'),int(result['status']=='BOOKED'))
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_real_separate_processes_compete_for_last_room(self):
        script='''import json,sys,time
from pathlib import Path
from datetime import date
from hotel_sim import BookingRequest
from hotel_sim.sqlite_store import SQLiteHotel
path,folder,key=sys.argv[1:]
folder=Path(folder);(folder/('ready-'+key)).write_text('ready')
end=time.monotonic()+15
while not (folder/'start').exists():
 if time.monotonic()>end:raise RuntimeError('barrier timeout')
 time.sleep(.005)
request=BookingRequest(key,date(2026,12,7),date(2026,12,8),'S',('king',),2)
print(json.dumps(SQLiteHotel(path).reserve(request)))
'''
        folder=Path(self.tmp.name)
        processes=[subprocess.Popen([sys.executable,'-c',script,str(self.path),str(folder),f'process-{i}'],cwd=ROOT,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(8)]
        try:
            end=time.monotonic()+15
            while len(list(folder.glob('ready-*')))<8:
                if time.monotonic()>end:self.fail('subprocess barrier timeout')
                time.sleep(.01)
            (folder/'start').write_text('go')
            statuses=[]
            for process in processes:
                out,err=process.communicate(timeout=20);self.assertEqual(process.returncode,0,err)
                statuses.append(json.loads(out)['status'])
            self.assertEqual(statuses.count('BOOKED'),1);self.assertEqual(statuses.count('REJECTED'),7)
            self.assertTrue(self.db.reconcile()['healthy'])
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.communicate()

    def test_lock_timeout_does_not_cache_false_rejection(self):
        holder=self.db.connect();holder.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(DatabaseBusy):SQLiteHotel(self.path,timeout_ms=30).reserve(req())
        finally:holder.rollback();holder.close()
        self.assertEqual(self.count('bookings'),0)
        self.assertEqual(self.db.reserve(req())['status'],'BOOKED')

    def test_read_snapshot_and_reconciliation_are_read_only(self):
        with self.db.read_snapshot() as c:
            with self.assertRaises(sqlite3.OperationalError):c.execute("UPDATE auxiliary_inventory SET cots_used=1")
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_reconciliation_sees_one_snapshot_during_concurrent_commit(self):
        snapshot_established,writer_finished=threading.Event(),threading.Event()
        def checkpoint(stage,c):
            if stage=='reconcile_snapshot':
                snapshot_established.set();self.assertTrue(writer_finished.wait(10))
        self.db._checkpoint=checkpoint
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.db.reconcile);self.assertTrue(snapshot_established.wait(10))
            SQLiteHotel(self.path).reserve(req());writer_finished.set();report=future.result(10)
        self.assertTrue(report['healthy']);self.assertEqual(report['booking_status_counts'],{})
        self.db._checkpoint=lambda *args:None
        self.assertEqual(self.db.reconcile()['booking_status_counts'],{'BOOKED':1})

    def test_reconcile_detects_counter_corruption_and_write_fails_closed(self):
        self.db.reserve(req(child_ages=(1,)))
        c=self.db.connect();c.execute('UPDATE auxiliary_inventory SET cots_used=0');c.close()
        report=self.db.reconcile();self.assertFalse(report['healthy'])
        self.assertTrue(any('resource_counter_mismatch' in item for item in report['issues']))
        with self.assertRaises(DataIntegrityError):self.db.reserve(req('B',1,2))
        self.assertEqual(self.count('bookings'),1)

    def test_reconcile_detects_missing_active_night_cancel_refuses_over_release(self):
        self.db.reserve(req(end=2,child_ages=(1,)))
        c=self.db.connect();c.execute('DELETE FROM active_room_nights WHERE night=?',(DAY.isoformat(),));c.close()
        self.assertFalse(self.db.reconcile()['healthy'])
        with self.assertRaises(DataIntegrityError):self.db.cancel('A')
        self.assertEqual(self.db.get_booking('A')['status'],'BOOKED')

    def test_history_config_and_terminal_guards(self):
        self.db.reserve(req());self.db.cancel('A')
        c=self.db.connect()
        try:
            for statement in ("DELETE FROM bookings","UPDATE booking_nights SET cots=5","DELETE FROM reservation_events",
                              "UPDATE configuration SET mode='strict'","UPDATE room_calendar SET blocked=1",
                              "UPDATE bookings SET status='BOOKED',cancelled_at=NULL"):
                with self.subTest(statement=statement),self.assertRaises(sqlite3.IntegrityError):c.execute(statement)
        finally:c.close()
        self.assertTrue(self.db.reconcile()['healthy'])

    def test_constraints_reject_double_allocation_and_negative_resources(self):
        self.db.reserve(req());c=self.db.connect()
        try:
            for sql in ("INSERT INTO active_room_nights SELECT * FROM active_room_nights",
                        "UPDATE auxiliary_inventory SET cots_used=-1","UPDATE auxiliary_inventory SET cots_used=3",
                        "INSERT INTO active_room_nights VALUES('S0','2026-12-08','missing')"):
                with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):c.execute(sql)
        finally:c.close()

    def test_reconcile_detects_missing_event_and_bad_original_decision(self):
        self.db.reserve(req());self.db.cancel('A');c=self.db.connect()
        c.execute('DROP TRIGGER reservation_events_delete_guard');c.execute("DELETE FROM reservation_events WHERE event_type='CANCELLED'")
        c.close();report=self.db.reconcile()
        self.assertFalse(report['healthy']);self.assertIn('event_sequence_mismatch:A',report['issues'])
        c=self.db.connect();c.execute('DROP TRIGGER bookings_identity_guard')
        row=c.execute("SELECT decision_json FROM bookings WHERE request_id='A'").fetchone()
        dec=json.loads(row[0]);dec['upgrade_cost_cents']=999
        c.execute('UPDATE bookings SET decision_json=? WHERE request_id=?',(json.dumps(dec),'A'));c.close()
        self.assertIn('upgrade_cost_mismatch:A',self.db.reconcile()['issues'])

    def test_configuration_checksum_drift_detected(self):
        c=self.db.connect();c.execute('DROP TRIGGER configuration_update_guard')
        c.execute("UPDATE configuration SET mode='strict'");c.close()
        self.assertFalse(self.db.reconcile()['healthy'])
        with self.assertRaises(DataIntegrityError):self.db.reserve(req())

    def test_initialization_idempotent_and_different_inventory_refused(self):
        self.db.reserve(req());self.db.initialize(self.config,'upgrade')
        self.assertEqual(self.count('bookings'),1)
        with self.assertRaises(ValueError):self.db.initialize(self.config,'strict')
        changed=copy.deepcopy(self.config);changed['daily_resources'][0]['cots']=3
        with self.assertRaises(ValueError):self.db.initialize(changed,'upgrade')
        self.assertEqual(self.db.get_booking('A')['status'],'BOOKED')

    def test_initialization_fault_rolls_back_schema_and_seed(self):
        fresh=SQLiteHotel(Path(self.tmp.name)/'failed.db')
        def fail(stage,c):
            if stage=='initialize_before_commit':raise RuntimeError('seed fault')
        fresh._checkpoint=fail
        with self.assertRaises(RuntimeError):fresh.initialize(self.config)
        c=fresh.connect();self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],0);c.close()
        fresh._checkpoint=lambda *args:None;fresh.initialize(self.config);self.assertTrue(fresh.reconcile()['healthy'])

    def test_concurrent_initializers_do_not_duplicate_inventory(self):
        path=Path(self.tmp.name)/'concurrent-init.db';barrier=threading.Barrier(4)
        def init(i):barrier.wait(timeout=10);return SQLiteHotel(path).initialize(self.config,'upgrade')
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(init,range(4)))
        self.assertTrue(all(r['initialized'] and r['config_sha256']==results[0]['config_sha256'] for r in results));self.assertTrue(SQLiteHotel(path).reconcile()['healthy'])

    def test_unknown_schema_and_unmanaged_database_refused(self):
        c=self.db.connect();c.execute('PRAGMA user_version=999');c.close()
        with self.assertRaises(DataIntegrityError):self.db.initialize(self.config)
        self.assertFalse(self.db.reconcile()['healthy'])
        other=SQLiteHotel(Path(self.tmp.name)/'other.db');c=other.connect();c.execute('CREATE TABLE foreign_data(x)');c.close()
        with self.assertRaises(DataIntegrityError):other.initialize(self.config)

    def test_sqlite_matches_original_simulation_for_all_policies(self):
        data,_=load_scenario(ROOT/'data/week.json');requests=parse_requests(data)
        for mode in ('strict','upgrade','protected_upgrade'):
            with self.subTest(mode=mode):
                db=SQLiteHotel(Path(self.tmp.name)/(mode+'.db'));db.initialize(data,mode)
                baseline=build_allocator(data,mode)
                for request in requests:
                    expected=json.loads(json.dumps(baseline.allocate(request).to_dict()))
                    self.assertEqual(db.reserve(request)['original_decision'],expected)
                self.assertEqual(db.snapshot(),baseline.snapshot());self.assertTrue(db.reconcile()['healthy'])
                for request in requests:
                    if db.get_booking(request.request_id)['status']=='BOOKED':db.cancel(request.request_id)
                self.assertEqual(db.snapshot()['occupied_room_nights'],0);self.assertTrue(db.reconcile()['healthy'])

    def test_missing_booking_and_invalid_reconcile_limit(self):
        with self.assertRaises(BookingNotFound):self.db.get_booking('missing')
        with self.assertRaises(BookingNotFound):self.db.cancel('missing')
        for limit in (0,1001,True):
            with self.subTest(limit=limit),self.assertRaises(ValueError):self.db.reconcile(limit)

    def test_wal_busy_after_initialization_is_success_with_explicit_warning(self):
        db=SQLiteHotel(Path(self.tmp.name)/'wal-reader.db',timeout_ms=30)
        holders=[]
        def hold_reader(stage,connection):
            if stage=='initialize_after_commit':
                reader=db.connect();reader.execute('BEGIN');reader.execute('SELECT * FROM configuration').fetchone()
                holders.append(reader)
        db._checkpoint=hold_reader
        try:
            result=db.initialize(self.config)
            self.assertTrue(result['initialized']);self.assertFalse(result['wal_enabled'])
            self.assertTrue(result['warnings']);self.assertTrue(db.reconcile()['healthy'])
        finally:
            for holder in holders:holder.rollback();holder.close()
        db._checkpoint=lambda *args:None
        self.assertTrue(db.initialize(self.config)['wal_enabled'])

    def test_reconcile_missing_table_returns_incomplete_not_exception(self):
        c=self.db.connect();c.execute('DROP TABLE reservation_events');c.close()
        report=self.db.reconcile();self.assertFalse(report['healthy']);self.assertFalse(report['verification_complete'])
        with self.assertRaises(DataIntegrityError):self.db.initialize(self.config,'upgrade')

    def test_reconcile_invalid_decision_shape_returns_unhealthy(self):
        self.db.reserve(req());c=self.db.connect();c.execute('DROP TRIGGER bookings_identity_guard')
        c.execute("UPDATE bookings SET decision_json='null'");c.close()
        report=self.db.reconcile();self.assertFalse(report['healthy'])

    def test_real_cli_subprocess_demo(self):
        result=subprocess.run([sys.executable,'scripts/sqlite_demo.py'],cwd=ROOT,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['booked']['status'],'BOOKED');self.assertEqual(report['cancelled']['status'],'CANCELLED')
        self.assertEqual(report['after_cancel']['active_room_nights'],0);self.assertTrue(report['after_cancel']['healthy'])

    def test_randomized_booking_cancellation_trace_reconciles(self):
        import random
        rng=random.Random(20261106)
        db=SQLiteHotel(Path(self.tmp.name)/'trace.db');db.initialize(configuration(3),'upgrade')
        live=[]
        for index in range(200):
            if live and rng.random()<.45:
                key=live.pop(rng.randrange(len(live)));db.cancel(key);db.cancel(key)
            else:
                start=rng.randrange(4);end=rng.randrange(start+1,5)
                request=req(f'trace-{index}',start,end,child_ages=rng.choice(((),(1,),(5,))))
                result=db.reserve(request)
                if result['status']=='BOOKED':live.append(request.request_id)
            self.assertTrue(db.reconcile()['healthy'])
        for key in live:db.cancel(key)
        self.assertEqual(db.snapshot()['occupied_room_nights'],0);self.assertTrue(db.reconcile()['healthy'])

    def test_reconcile_missing_configuration_column_is_incomplete(self):
        c=self.db.connect();c.execute('ALTER TABLE configuration DROP COLUMN mode');c.close()
        report=self.db.reconcile();self.assertFalse(report['healthy']);self.assertFalse(report['verification_complete'])

if __name__=='__main__':unittest.main()
