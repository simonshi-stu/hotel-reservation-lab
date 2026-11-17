import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from hotel.planning import PlanningStore,generate,compare,TYPES
from hotel.service import Problem
ROOT=Path(__file__).resolve().parents[1]
def demand(i,**changes):
    return {"id":"R"+str(i),"tier":0,"bed":"king","adults":2,"children":0,"child_friendly":False,
            "consent":True,"check_in":"2027-01-01","check_out":"2027-01-03",**changes}
class PlanningTests(unittest.TestCase):
    def setUp(self):
        root=ROOT/".runtime"/"tests";root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def store(self,caps=None):
        caps=caps or {"standard-king":1,"deluxe-king":1}
        calendar=[{"room":t["id"],"day":"2027-01-0"+str(d),"capacity":caps.get(t["id"],0)} for t in TYPES for d in range(1,5)]
        store=PlanningStore(self.root/"test.db");store.initialize_calendar(calendar);return store
    def test_existing_upgrade_frees_standard_preserves_price_and_no_consent(self):
        store=self.store();first=store.book(demand(1))
        admitted=store.book(demand(2,consent=False),True)
        report=store.report();self.assertEqual(admitted["assigned"],"standard-king")
        self.assertEqual(report["existing_moves"],1);self.assertEqual(report["violations"],[])
        b=next(b for b in report["bookings"] if b["id"]=="R1")
        self.assertEqual(b["assigned"],"deluxe-king");self.assertEqual(b["total_cents"],first["total_cents"])
    def test_no_consent_cannot_move_or_receive_upgrade(self):
        store=self.store();store.book(demand(1,consent=False))
        with self.assertRaises(Problem):store.book(demand(2,consent=False),True)
        self.assertEqual(store.report()["existing_moves"],0)
    def test_children_bed_and_adults_are_hard_constraints(self):
        store=self.store({"standard-king":3,"standard-twin":1,"suite-twin":1})
        with self.assertRaises(Problem):store.book(demand(1,children=1,child_friendly=True,consent=False),True)
        result=store.book(demand(2,bed="twin",adults=4,children=2,child_friendly=True),True)
        self.assertEqual(result["assigned"],"suite-twin");self.assertEqual(store.report()["violations"],[])
    def test_failed_multiday_admission_rolls_back_partial_relocation(self):
        store=self.store()
        store.book(demand(1,check_out="2027-01-02"))
        store.book(demand(2,consent=False,check_in="2027-01-02"))
        before=store.report()
        with self.assertRaises(Problem):store.book(demand(3,consent=False),True)
        after=store.report();self.assertEqual(before,after)
    def test_relocation_requires_destination_for_entire_existing_stay(self):
        store=self.store();store.book(demand(1,check_out="2027-01-04"))
        with store.transaction(write=True) as c:c.execute("UPDATE inventory SET capacity=0,remaining=0 WHERE room='deluxe-king' AND day='2027-01-03'")
        with self.assertRaises(Problem):store.book(demand(2,consent=False,check_out="2027-01-02"),True)
        self.assertEqual(store.report()["existing_moves"],0)
    def test_concurrent_oversell_and_idempotency(self):
        store=self.store({"standard-king":5,"deluxe-king":2,"suite-king":1})
        def place(i):
            try:return store.book(demand(i),True)
            except Problem:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(place,range(40)))
        self.assertEqual(sum(r is not None for r in results),8)
        self.assertEqual(store.report()["violations"],[])
        existing=next(r for r in results if r)
        original=json.loads(next(b["payload"] for b in store.report()["bookings"] if b["id"]==existing["id"]))
        self.assertTrue(store.book(original,True)["replayed"])
        with self.assertRaises(Problem):store.book({**original,"adults":1},True)
    def test_same_seed_comparison_reproducible(self):
        a=compare(self.root/"a",71);b=compare(self.root/"b",71)
        self.assertEqual(a,b)
        self.assertGreater(len({d["visits"] for d in a["daily"]}),1)
        self.assertTrue(all(not r["violations"] for r in a["strategies"].values()))
if __name__=="__main__":unittest.main()

