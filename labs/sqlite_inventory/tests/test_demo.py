import hashlib
import inspect
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from hotel.demo import DemoServer,Manager,source,validate
from hotel.service import Problem,Store

ROOT=Path(__file__).resolve().parents[1]
CONFIG={"rooms":5,"requests":24,"concurrency":8,"quantity":1,"cancel_orders":2,"refill_requests":8,
        "check_in":"2027-01-01","check_out":"2027-01-04"}

class DemoTests(unittest.TestCase):
    def setUp(self):
        root=ROOT/".runtime"/"tests"
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.manager=Manager(Path(self.temp.name)/"isolated",max_runs=2)
        self.server=DemoServer(0,self.manager)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url="http://127.0.0.1:"+str(self.server.server_port)
    def tearDown(self):
        for job in self.manager.jobs:
            job.join(timeout=15)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
    def call(self,path,payload=None,token=True,headers=None):
        data=None if payload is None else json.dumps(payload).encode()
        h={"Content-Type":"application/json"}
        if token:h["X-Demo-Token"]=self.server.demo_token
        h.update(headers or {})
        try:
            response=urlopen(Request(self.url+path,data=data,headers=h),timeout=10)
        except HTTPError as exc:
            response=exc
        with response:
            raw=response.read()
            return response.code,json.loads(raw) if "json" in response.headers.get("Content-Type","") else raw.decode()
    def wait(self,run_id):
        end=time.monotonic()+15
        while time.monotonic()<end:
            code,state=self.call("/api/runs/"+run_id)
            self.assertEqual(code,200)
            if state["status"]!="running":return state
            time.sleep(.03)
        self.fail("Run did not finish")
    def test_real_concurrent_run_isolated_and_stock_reconciled(self):
        existing=ROOT/".runtime"/"hotel.db"
        before=hashlib.sha256(existing.read_bytes()).hexdigest() if existing.exists() else None
        code,started=self.call("/api/runs",CONFIG)
        self.assertEqual(code,202)
        state=self.wait(started["id"])
        self.assertEqual(state["status"],"complete",state["error"])
        self.assertTrue(state["audit"]["ok"])
        self.assertEqual(state["summary"],{"initial_created":5,"initial_rejected":19,"distinct_cancelled":2,
                                          "refill_created":2,"refill_rejected":6})
        self.assertEqual(state["peak_overlapping_attempts"],8)
        self.assertGreaterEqual(len(state["attempt_threads"]),8)
        self.assertEqual([p["availability"]["available"] for p in state["phases"]],[5,0,2,0])
        kinds={e["kind"] for e in state["events"]}
        self.assertTrue({"write_lock_acquired","transaction_committed","transaction_failed","rejected"}<=kinds)
        self.assertLessEqual(len(state["events"]),1000)
        self.assertEqual(hashlib.sha256(existing.read_bytes()).hexdigest() if existing.exists() else None,before)
        run_path=self.manager.root/started["id"]/"hotel.db"
        self.assertTrue(run_path.is_file())
        self.assertTrue(Store(run_path).audit()["ok"])
    def test_parameter_limits_reject_before_files_created(self):
        for changes in ({"concurrency":13},{"requests":81},{"rooms":31},{"quantity":True},
                        {"cancel_orders":11},{"refill_requests":31},{"check_out":"2027-01-10"},
                        {"check_in":"2026-12-31"},{"db":"unsafe.db"}):
            with self.subTest(changes=changes):
                code,result=self.call("/api/runs",{**CONFIG,**changes})
                self.assertEqual(code,400,result)
        self.assertFalse(self.manager.root.exists())
    def test_origin_host_token_and_source_whitelist(self):
        self.assertEqual(self.call("/api/runs",CONFIG,token=False)[0],403)
        self.assertEqual(self.call("/api/runs",CONFIG,headers={"Origin":"http://evil.example"})[0],403)
        self.assertEqual(self.call("/",headers={"Host":"evil.example"})[0],403)
        self.assertEqual(self.call("/api/source/not_allowed")[0],404)
        code,result=self.call("/api/source/reserve")
        self.assertEqual(code,200)
        self.assertEqual(result["start_line"],inspect.getsourcelines(Store.reserve)[1])
        self.assertIn("def reserve",result["code"])
        code,page=self.call("/",token=False)
        self.assertEqual(code,200)
        self.assertNotIn("__TOKEN__",page)
        self.assertIn("nonce=",page)
        self.assertIn("不",page)
        self.assertFalse(self.manager.root.exists())
    def test_demo_port_cannot_be_shared_by_second_server(self):
        with self.assertRaises(OSError):
            DemoServer(self.server.server_port,Manager(Path(self.temp.name)/"other"))

    def test_busy_gate_and_session_limit(self):
        self.manager.active=True
        self.assertEqual(self.call("/api/runs",CONFIG)[0],503)
        self.manager.active=False
        self.manager.max_runs=1
        code,started=self.call("/api/runs",CONFIG)
        self.assertEqual(code,202)
        self.assertEqual(self.wait(started["id"])["status"],"complete")
        for job in self.manager.jobs:job.join()
        self.assertEqual(self.call("/api/runs",CONFIG)[0],429)
    def test_planning_mode_preserves_guards_and_compares_same_input(self):
        self.assertEqual(self.call("/api/planning",{"seed":True,"scale":1})[0],400)
        self.assertEqual(self.call("/api/planning",{"seed":61104,"scale":1},token=False)[0],403)
        self.assertEqual(self.call("/planning",token=False)[0],200)
        code,result=self.call("/api/planning",{"seed":61104,"scale":1})
        self.assertEqual(code,202)
        state=self.wait(result["id"])
        self.assertEqual(state["status"],"complete",state.get("error"))
        self.assertEqual(state["result"]["request_count"],len(state["result"]["demand"]))
        self.assertTrue(all(not r["violations"] for r in state["result"]["strategies"].values()))
        code,source=self.call("/api/source/allocation")
        self.assertEqual(code,200)
        self.assertEqual(source["path"],"hotel/planning.py")
        self.assertIn("SAVEPOINT rearrange",source["code"])

    def test_separate_runs_do_not_share_inventory(self):
        ids=[]
        for _ in range(2):
            code,result=self.call("/api/runs",{**CONFIG,"requests":8,"cancel_orders":0,"refill_requests":0})
            self.assertEqual(code,202)
            state=self.wait(result["id"])
            for job in self.manager.jobs:job.join()
            self.assertEqual(state["summary"]["initial_created"],5)
            ids.append(result["id"])
        self.assertNotEqual(*ids)
        self.assertEqual(len(list(self.manager.root.glob("*/hotel.db"))),2)

if __name__=="__main__":
    unittest.main()

