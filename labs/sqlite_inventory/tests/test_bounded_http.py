import concurrent.futures
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from hotel.api import Server
from scripts.http_client import Client
from scripts.support import scenario_store,request

ROOT=Path(__file__).resolve().parents[1]

def wait_for(predicate,timeout=3):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition not reached")

class BlockingStore:
    def __init__(self,base):
        self.base=base
        self.timeout=base.timeout
        self.entered=threading.Event()
        self.release=threading.Event()
    def reserve(self,**payload):
        if payload["idempotency_key"]=="blocking":
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("Test release missing")
        return self.base.reserve(**payload)
    def __getattr__(self,name):
        return getattr(self.base,name)

class BoundedHttpTests(unittest.TestCase):
    def setUp(self):
        root=ROOT/".runtime"/"tests"
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.store=scenario_store(Path(self.temp.name)/"hotel.db",capacity=10)
        self.server=None
        self.connections=[]
    def start(self,store=None,**options):
        self.server=Server(("127.0.0.1",0),store or self.store,**options)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.client=Client("http://127.0.0.1:"+str(self.server.server_port),timeout=3)
        return self.server
    def tearDown(self):
        for connection in self.connections:
            connection.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.assertTrue(all(not t.is_alive() for t in self.server.workers))
        self.temp.cleanup()

    def test_queue_full_expiry_no_commit_and_retry(self):
        blocked=BlockingStore(self.store)
        server=self.start(blocked,max_active=1,queue_size=1,queue_timeout=0.15,read_timeout=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.client.call,"/reservations",request(key="blocking"))
            try:
                self.assertTrue(blocked.entered.wait(3))
                second=pool.submit(self.client.call,"/reservations",request(customer=2,key="queued"))
                wait_for(lambda:server.stats()["pending"]==1)
                rejected=self.client.call("/reservations",request(customer=3,key="overflow"))
                self.assertEqual(rejected["status"],503)
                self.assertEqual(rejected["body"]["error"],"server_busy")
                self.assertEqual(rejected["body"]["outcome"],"not_committed")
                self.assertEqual(rejected["retry_after"],"1")
                expired=second.result(timeout=3)
                self.assertEqual(expired["status"],503)
                self.assertEqual(expired["body"]["error"],"queue_wait_timeout")
                with self.store.transaction() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],0)
            finally:
                blocked.release.set()
            self.assertEqual(first.result(timeout=3)["status"],201)
        accepted=self.client.call("/reservations",request(customer=3,key="overflow"))
        self.assertEqual(accepted["status"],201)
        stats=server.stats()
        self.assertLessEqual(stats["peak_active"],1)
        self.assertLessEqual(stats["peak_pending"],1)
        self.assertLessEqual(stats["peak_live"],3)
        self.assertTrue(self.store.audit()["ok"])

    def test_partial_headers_have_total_deadline_and_fixed_worker_count(self):
        server=self.start(max_active=2,queue_size=1,queue_timeout=0.12,read_timeout=0.3)
        for _ in range(2):
            connection=socket.create_connection(server.server_address,timeout=1)
            connection.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nX-Slow: ")
            self.connections.append(connection)
        wait_for(lambda:server.stats()["active"]==2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            queued=pool.submit(self.client.call,"/health")
            wait_for(lambda:server.stats()["pending"]==1)
            rejected=self.client.call("/health")
            self.assertEqual(rejected["status"],503)
            self.assertEqual(rejected["body"]["error"],"server_busy")
            self.assertEqual(queued.result(timeout=2)["body"]["error"],"queue_wait_timeout")
        # Drip bytes more often than a simple socket inactivity timeout.
        for _ in range(6):
            time.sleep(0.04)
            for connection in self.connections:
                try:
                    connection.sendall(b"x")
                except OSError:
                    pass
        wait_for(lambda:server.stats()["active"]==0,timeout=1)
        self.assertEqual(self.client.call("/health")["status"],200)
        stats=server.stats()
        self.assertGreaterEqual(stats["read_timeouts"],2)
        self.assertEqual(stats["worker_threads_alive"],2)
        self.assertLessEqual(stats["peak_active"],2)
        self.assertLessEqual(stats["peak_pending"],1)
        self.assertLessEqual(stats["peak_live"],4)

    def test_rejected_connection_can_finish_small_body_before_close(self):
        import json
        from http.client import HTTPResponse
        blocked=BlockingStore(self.store)
        server=self.start(blocked,max_active=1,queue_size=1,queue_timeout=2,read_timeout=1,response_timeout=.3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.client.call,"/reservations",request(key="blocking"))
            try:
                self.assertTrue(blocked.entered.wait(3))
                queued=pool.submit(self.client.call,"/health")
                wait_for(lambda:server.stats()["pending"]==1)
                connection=socket.create_connection(server.server_address,timeout=1)
                self.connections.append(connection)
                body=json.dumps(request(customer=3,key="late-body")).encode()
                headers=("POST /reservations HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                         "Content-Length: "+str(len(body))+"\r\n\r\n").encode()
                connection.sendall(headers)
                time.sleep(.03)
                connection.sendall(body)
                response=HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status,503)
                self.assertEqual(json.load(response)["error"],"server_busy")
                response.close()
                connection.close()
                with self.store.transaction() as database:
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],0)
            finally:
                blocked.release.set()
            self.assertEqual(first.result(timeout=3)["status"],201)
            self.assertEqual(queued.result(timeout=3)["status"],200)
        self.assertTrue(self.store.audit()["ok"])

    def test_client_retry_honors_retry_after_and_preserves_key(self):
        blocked=BlockingStore(self.store)
        server=self.start(blocked,max_active=1,queue_size=1,queue_timeout=2,read_timeout=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.client.call,"/reservations",request(key="blocking"))
            timer=None
            try:
                self.assertTrue(blocked.entered.wait(3))
                queued=pool.submit(self.client.call,"/health")
                wait_for(lambda:server.stats()["pending"]==1)
                timer=threading.Timer(.2,blocked.release.set)
                timer.start()
                retried=self.client.call("/reservations",request(customer=3,key="same-retry-key"),max_attempts=3)
                self.assertEqual([a["status"] for a in retried["attempts"]],[503,201])
                self.assertEqual(retried["attempts"][0]["retry_after"],"1")
                self.assertGreaterEqual(retried["logical_latency_ms"],1000)
            finally:
                blocked.release.set()
                if timer:
                    timer.join(timeout=2)
            self.assertEqual(first.result(timeout=3)["status"],201)
            self.assertEqual(queued.result(timeout=3)["status"],200)
        replay=self.client.call("/reservations",request(customer=3,key="same-retry-key"))
        self.assertEqual(replay["status"],200)
        self.assertEqual(replay["body"]["id"],retried["body"]["id"])
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reservations WHERE idempotency_key='same-retry-key'").fetchone()[0],1)
        self.assertTrue(self.store.audit()["ok"])

    def test_body_deadline_returns_408_without_mutation(self):
        server=self.start(max_active=1,queue_size=1,read_timeout=0.2)
        connection=socket.create_connection(server.server_address,timeout=1)
        self.connections.append(connection)
        connection.sendall(b"POST /reservations HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
        data=connection.recv(4096)
        self.assertIn(b"408",data)
        wait_for(lambda:server.stats()["active"]==0)
        with self.store.transaction() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],0)
        self.assertEqual(self.client.call("/health")["status"],200)

    def test_shutdown_does_not_start_queued_business_and_joins_workers(self):
        blocked=BlockingStore(self.store)
        server=self.start(blocked,max_active=1,queue_size=1,queue_timeout=3,read_timeout=0.3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            first=pool.submit(self.client.call,"/reservations",request(key="blocking"))
            try:
                self.assertTrue(blocked.entered.wait(3))
                queued=pool.submit(self.client.call,"/reservations",request(customer=2,key="shutdown-queued"))
                wait_for(lambda:server.stats()["pending"]==1)
                server.shutdown()
                closing=pool.submit(server.server_close)
                wait_for(lambda:server.closing.is_set())
            finally:
                blocked.release.set()
            self.assertEqual(first.result(timeout=3)["status"],201)
            rejected=queued.result(timeout=3)
            self.assertEqual(rejected["status"],503)
            self.assertEqual(rejected["body"]["error"],"server_shutdown")
            closing.result(timeout=3)
        self.assertTrue(all(not t.is_alive() for t in server.workers))
        with self.store.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],1)

    def test_unbounded_or_invalid_configuration_rejected(self):
        for options in ({"max_active":0},{"max_active":65},{"queue_size":0},
                        {"queue_size":257},{"queue_timeout":0},{"read_timeout":0}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    Server(("127.0.0.1",0),self.store,**options)

if __name__=="__main__":
    unittest.main()

