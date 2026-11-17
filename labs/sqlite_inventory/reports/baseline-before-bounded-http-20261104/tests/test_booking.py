import concurrent.futures
import json
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from hotel.api import Server
from hotel.service import Store, Problem
from scripts.support import scenario_store, request

ROOT = Path(__file__).resolve().parents[1]

def process_book(args):
    path, group, count = args
    store, outcomes = Store(path), {}
    for index in range(count):
        try:
            result = store.reserve(**request(customer=group*count+index+1, key="process-" + str(index)))
            code = "replayed" if result["replayed"] else "created"
        except Problem as exc:
            code = exc.code
        outcomes[code] = outcomes.get(code, 0) + 1
    return outcomes

def process_shared_key(args):
    path, operation, reservation_id = args
    store=Store(path)
    if operation == "book":
        return store.reserve(**request(key="shared-process-key"))
    return store.cancel(reservation_id,"C00001")
def crash_writer(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE nightly_inventory SET available=0 WHERE room_type_id='H1-R1'")
    os._exit(0)  # Kill process before COMMIT; another process must not see these writes.

class BookingTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / ".runtime" / "tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=directory)
        self.path = Path(self.tmp.name) / "hotel.db"
        self.store = scenario_store(self.path, capacity=10)

    def tearDown(self):
        self.tmp.cleanup()

    def assertProblem(self, code, operation):
        with self.assertRaises(Problem) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)

    def assertAudit(self):
        audit = self.store.audit()
        self.assertTrue(audit["ok"], audit)

    def test_prices_and_checkout_exclusive(self):
        created = self.store.reserve(**request(quantity=2))
        self.assertEqual(created["total_cents"], 2*(35100+35200+35300))
        self.assertEqual(self.store.availability("H1-R1","2027-01-01","2027-01-04")["available"], 8)
        self.assertEqual(self.store.availability("H1-R1","2027-01-04","2027-01-05")["available"], 10)
        self.assertAudit()

    def test_adjacent_full_stays_can_both_book(self):
        self.store.reserve(**request(quantity=10,check_out="2027-01-03"))
        self.store.reserve(**request(customer=2,key="adjacent",check_in="2027-01-03",quantity=10))
        self.assertAudit()

    def test_bottleneck_night_no_partial_write(self):
        self.store.reserve(**request(quantity=10,check_in="2027-01-02",check_out="2027-01-03"))
        self.assertProblem("sold_out", lambda: self.store.reserve(**request(customer=2,key="rejected",quantity=2)))
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inventory_events").fetchone()[0], 1)
        self.assertEqual(self.store.availability("H1-R1","2027-01-01","2027-01-02")["available"], 10)
        self.assertAudit()

    def test_calendar_gap_entire_stay_rejected(self):
        with self.store.transaction(write=True) as connection:
            connection.execute("DELETE FROM nightly_inventory WHERE room_type_id='H1-R1' AND stay_date='2027-01-02'")
        self.assertProblem("inventory_not_found", lambda: self.store.reserve(**request()))
        self.assertAudit()

    def test_invalid_dates_and_quantities(self):
        for changes, code in [({"check_in":"2027-02-30"}, "invalid_dates"),
                              ({"check_in":"2027-01-04"}, "invalid_dates"),
                              ({"check_out":"2028-01-01"}, "invalid_dates"),
                              ({"check_in":"20270101"}, "invalid_dates"),
                              ({"check_out":None}, "invalid_dates"),
                              ({"quantity":True}, "invalid_quantity"),
                              ({"quantity":1.0}, "invalid_quantity"),
                              ({"quantity":0}, "invalid_quantity"),
                              ({"quantity":11}, "invalid_quantity"),
                              ({"quantity":-2}, "invalid_quantity"),
                              ({"idempotency_key":"x' OR 1=1"}, "invalid_input")]:
            with self.subTest(changes=changes):
                payload=request()
                payload.update(changes)
                self.assertProblem(code, lambda: self.store.reserve(**payload))
        self.assertAudit()

    def test_missing_customer_room_and_out_of_calendar(self):
        for changes, code in [({"customer_id":"unknown"}, "customer_not_found"),
                              ({"room_type_id":"unknown"}, "inventory_not_found"),
                              ({"check_in":"2028-01-01","check_out":"2028-01-02"}, "inventory_not_found")]:
            payload=request()
            payload.update(changes)
            self.assertProblem(code, lambda: self.store.reserve(**payload))
        self.assertAudit()

    def test_same_key_threads_one_order_and_conflict(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda _: self.store.reserve(**request()), range(100)))
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(sum(not r["replayed"] for r in results), 1)
        self.assertProblem("idempotency_conflict", lambda: self.store.reserve(**request(quantity=2)))
        self.assertAudit()

    def test_idempotency_customer_scope(self):
        first=self.store.reserve(**request())
        second=self.store.reserve(**request(customer=2))
        self.assertNotEqual(first["id"],second["id"])
        self.assertAudit()

    def test_cancel_repeated_threads_and_replay_after_cancel(self):
        first=self.store.reserve(**request(quantity=3))
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            results=list(pool.map(lambda _:self.store.cancel(first["id"],"C00001"),range(60)))
        self.assertEqual(sum(not r["replayed"] for r in results),1)
        self.assertEqual(self.store.availability("H1-R1","2027-01-01","2027-01-04")["available"],10)
        replay=self.store.reserve(**request(quantity=3))
        self.assertEqual(replay["status"],"CANCELLED")
        self.assertEqual(replay["id"], first["id"])
        new=self.store.reserve(**request(key="new-key",quantity=3))
        self.assertNotEqual(new["id"],first["id"])
        self.assertAudit()

    def test_cancel_owner_and_missing_order(self):
        first=self.store.reserve(**request())
        self.assertProblem("reservation_not_found",lambda:self.store.cancel(first["id"],"C00002"))
        self.assertProblem("reservation_not_found",lambda:self.store.get(first["id"],"C00002"))
        self.assertProblem("reservation_not_found",lambda:self.store.cancel("unknown","C00001"))
        self.assertEqual(self.store.get(first["id"],"C00001")["status"],"CONFIRMED")
        self.assertAudit()

    def test_overlapping_mixed_batches_reclaim_inventory(self):
        for batch in range(4):
            results=[]
            for i in range(6):
                try:
                    results.append(self.store.reserve(**request(customer=i+1,key="batch-"+str(batch)+"-"+str(i),quantity=2)))
                except Problem as exc:
                    self.assertEqual(exc.code,"sold_out")
            self.assertEqual(len(results),5)
            self.assertEqual(self.store.availability("H1-R1","2027-01-01","2027-01-04")["available"],0)
            for result in results:
                self.store.cancel(result["id"],result["customer_id"])
            self.assertAudit()

    def test_multiple_processes_cannot_oversell(self):
        ctx=multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=8,mp_context=ctx) as pool:
            groups=list(pool.map(process_book,[(str(self.path),g,20) for g in range(8)]))
        self.assertEqual(sum(g.get("created",0) for g in groups),10)
        self.assertEqual(sum(g.get("sold_out",0) for g in groups),150)
        self.assertEqual(sum(g.get("database_busy",0) for g in groups),0)
        self.assertAudit()

    def test_multiple_processes_share_idempotency_and_cancel_once(self):
        ctx=multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=8,mp_context=ctx) as pool:
            results=list(pool.map(process_shared_key,[(str(self.path),"book",None)]*48))
            self.assertEqual(len({r["id"] for r in results}),1)
            self.assertEqual(sum(not r["replayed"] for r in results),1)
            reservation_id=results[0]["id"]
            cancelled=list(pool.map(process_shared_key,[(str(self.path),"cancel",reservation_id)]*48))
            self.assertEqual(sum(not r["replayed"] for r in cancelled),1)
        self.assertEqual(self.store.availability("H1-R1","2027-01-01","2027-01-04")["available"],10)
        self.assertAudit()
    def test_process_crash_rolls_back_uncommitted_writes(self):
        ctx=multiprocessing.get_context("spawn")
        process=ctx.Process(target=crash_writer,args=(str(self.path),))
        process.start()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join()
            self.fail("Crash writer did not finish")
        self.assertEqual(process.exitcode,0)
        self.assertEqual(self.store.availability("H1-R1","2027-01-01","2027-01-04")["available"],10)
        self.assertAudit()

    def test_busy_writer_returns_retryable_error(self):
        with self.store.transaction(write=True):
            store=Store(self.path,timeout=0.02)
            self.assertProblem("database_busy",lambda:store.reserve(**request()))
        self.store.reserve(**request())
        self.assertAudit()

    def test_database_constraints_prevent_negative_inventory(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction(write=True) as connection:
                connection.execute("UPDATE nightly_inventory SET available=-1")
        self.assertAudit()

    def test_restart_reads_committed_order(self):
        first=self.store.reserve(**request())
        reopened=Store(self.path)
        self.assertEqual(reopened.get(first["id"],"C00001")["id"],first["id"])
        self.assertTrue(reopened.audit()["ok"])

    def test_fault_during_second_night_rolls_back_booking_and_events(self):
        with self.store.transaction(write=True) as connection:
            connection.execute("CREATE TRIGGER simulated_failure BEFORE UPDATE ON nightly_inventory "
                               "WHEN NEW.room_type_id='H1-R1' AND NEW.stay_date='2027-01-02' "
                               "BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reserve(**request())
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inventory_events").fetchone()[0],0)
        self.assertAudit()

    def test_cancel_fault_rolls_back_all_nights(self):
        first=self.store.reserve(**request(quantity=3))
        with self.store.transaction(write=True) as connection:
            connection.execute("CREATE TRIGGER simulated_failure BEFORE UPDATE ON nightly_inventory "
                               "WHEN NEW.room_type_id='H1-R1' AND NEW.stay_date='2027-01-02' "
                               "BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.cancel(first["id"],"C00001")
        self.assertEqual(self.store.get(first["id"],"C00001")["status"],"CONFIRMED")
        self.assertAudit()

    def test_audit_detects_deliberately_corrupt_ledger(self):
        first=self.store.reserve(**request())
        with self.store.transaction(write=True) as connection:
            connection.execute("DELETE FROM inventory_events WHERE reservation_id=?",(first["id"],))
        self.assertFalse(self.store.audit()["ok"])

class HttpTests(BookingTests):
    # Only the HTTP tests below are run here; inherited BookingTests are suppressed by load_tests.
    def setUp(self):
        super().setUp()
        self.server=Server(("127.0.0.1",0),self.store,4)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url="http://127.0.0.1:"+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def call(self,path,body=None):
        data=None if body is None else json.dumps(body).encode()
        req=Request(self.url+path,data=data,headers={"Content-Type":"application/json"})
        try:
            with urlopen(req,timeout=10) as response:
                return response.status,json.load(response)
        except HTTPError as error:
            return error.code,json.load(error)

    def test_http_booking_lookup_cancel(self):
        code,result=self.call("/reservations",request())
        self.assertEqual(code,201)
        self.assertEqual(self.call("/reservations",request())[0],200)
        self.assertEqual(self.call("/reservations/"+result["id"]+"?customer_id=C00001")[0],200)
        self.assertEqual(self.call("/reservations/"+result["id"]+"/cancel",{"customer_id":"C00001"})[1]["status"],"CANCELLED")
        self.assertEqual(self.call("/health")[0],200)
        self.assertAudit()

    def test_http_invalid_and_sold_out(self):
        self.assertEqual(self.call("/reservations",{})[0],400)
        self.assertEqual(self.call("/unknown")[0],404)
        self.assertEqual(self.call("/availability?room_type_id=H1-R1&check_in=2027-01-01&check_out=2027-01-02")[0],200)
        self.assertEqual(self.call("/availability?room_type_id=H1-R1&room_type_id=H1-R1&check_in=2027-01-01&check_out=2027-01-02")[0],400)
        self.call("/reservations",request(quantity=10))
        self.assertEqual(self.call("/reservations",request(customer=2,key="overflow"))[1]["error"],"sold_out")

    def test_http_admission_returns_503_and_recovers(self):
        for _ in range(4):
            self.server.slots.acquire()
        try:
            code,payload=self.call("/health")
            self.assertEqual(code,503)
            self.assertEqual(payload["error"],"server_busy")
        finally:
            for _ in range(4):
                self.server.slots.release()
        self.assertEqual(self.call("/health")[0],200)

    def test_http_rejects_malformed_json(self):
        req=Request(self.url+"/reservations",data=b"{bad",headers={"Content-Type":"application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(req,timeout=10)
        self.assertEqual(caught.exception.code,400)

def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(BookingTests))
    for name in ("test_http_booking_lookup_cancel","test_http_invalid_and_sold_out",
                 "test_http_admission_returns_503_and_recovers","test_http_rejects_malformed_json"):
        suite.addTest(HttpTests(name))
    return suite

if __name__=="__main__":
    unittest.main()

