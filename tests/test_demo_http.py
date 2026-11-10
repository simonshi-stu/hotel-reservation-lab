import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from hotel_demo.server import DemoServer,DemoWorkspace,MODES
from hotel_sim.sqlite_store import SQLiteHotel

ROOT=Path(__file__).resolve().parent.parent

class DemoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state'
        self.workspace=DemoWorkspace(self.path)
        self.server=DemoServer(('127.0.0.1',0),self.workspace)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()
        self.origin=f'http://127.0.0.1:{self.server.server_port}'
        self.body=json.loads((ROOT/'data/sqlite_demo_request.json').read_text())
        self.body['request_id']='HTTP-DEMO-001'

    def tearDown(self):
        self.server.shutdown();self.thread.join(3);self.server.server_close();self.tmp.cleanup()

    def call(self,path='/api/state?mode=upgrade',method='GET',body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        merged={}
        if method=='POST':merged={'Origin':self.origin,'Content-Type':'application/json','X-Demo-CSRF':self.server.csrf}
        if headers:merged.update(headers)
        connection.request(method,path,body=json.dumps(body) if body is not None else None,headers=merged)
        response=connection.getresponse();data=response.read();result=(response.status,dict(response.getheaders()),data)
        connection.close();return result

    def state(self,mode='upgrade'):
        status,headers,data=self.call('/api/state?mode='+mode);self.assertEqual(status,200,data);return json.loads(data)

    def reserve(self,**changes):
        body={'mode':'upgrade','generation':self.workspace.generation,'request':self.body,'simulate_failure':False}
        body.update(changes)
        status,headers,data=self.call('/api/reserve','POST',body)
        return status,json.loads(data)

    def test_state_is_coherent_and_explicitly_synthetic(self):
        state=self.state();self.assertTrue(state['capabilities']['synthetic'])
        self.assertTrue(state['capabilities']['serialized_interaction']);self.assertFalse(state['capabilities']['production_service'])
        self.assertEqual(len(state['catalog']['rooms']),8);self.assertEqual(len(state['inventory']['daily']),10)
        self.assertEqual(state['bookings'],[]);self.assertTrue(state['audit']['healthy'])

    def test_actual_sqlite_booking_receipt_and_idempotent_replay(self):
        status,receipt=self.reserve();self.assertEqual(status,200);self.assertEqual(receipt['status'],'BOOKED')
        self.assertEqual(receipt['inventory_delta_nights'],3);self.assertEqual(receipt['trace'][-1]['code'],'COMMIT')
        self.assertTrue(receipt['audit']['healthy']);self.assertFalse(receipt['replayed'])
        status,replayed=self.reserve();self.assertEqual(status,200);self.assertTrue(replayed['replayed'])
        self.assertEqual(replayed['inventory_delta_nights'],0);self.assertEqual(self.state()['audit']['events'],1)

    def test_actual_first_night_failure_rolls_back_then_same_key_retries(self):
        before=self.state()['inventory'];status,receipt=self.reserve(simulate_failure=True)
        self.assertEqual(status,200);self.assertEqual(receipt['status'],'ROLLED_BACK')
        self.assertEqual(receipt['trace'][-1]['code'],'ROLLBACK');self.assertIsNone(receipt['record'])
        self.assertEqual(receipt['inventory_delta_nights'],0);self.assertEqual(self.state()['inventory'],before)
        self.assertEqual(self.state()['bookings'],[])
        status,receipt=self.reserve();self.assertEqual(receipt['status'],'BOOKED')

    def test_cancel_release_and_cancelled_replay(self):
        self.reserve();body={'mode':'upgrade','generation':self.workspace.generation,'request_id':self.body['request_id']}
        status,headers,data=self.call('/api/cancel','POST',body);receipt=json.loads(data)
        self.assertEqual(status,200);self.assertEqual(receipt['status'],'CANCELLED');self.assertEqual(receipt['inventory_delta_nights'],-3)
        self.assertEqual(self.state()['inventory']['occupied_room_nights'],0)
        status,replay=self.reserve();self.assertEqual(replay['status'],'CANCELLED');self.assertTrue(replay['replayed'])
        self.assertEqual(self.state()['audit']['events'],2)

    def test_strategies_have_independent_databases(self):
        self.reserve();self.assertEqual(len(self.state('upgrade')['bookings']),1)
        self.assertEqual(self.state('strict')['bookings'],[]);self.assertEqual(self.state('protected_upgrade')['bookings'],[])

    def test_new_round_retains_old_database_and_rejects_stale_write(self):
        self.reserve();old=self.workspace.generation
        status,headers,data=self.call('/api/reset','POST',{'mode':'upgrade','generation':old});new=json.loads(data)
        self.assertEqual(status,200);self.assertNotEqual(new['generation'],old);self.assertEqual(new['bookings'],[])
        old_store=SQLiteHotel(self.path/old/'upgrade.db');self.assertEqual(old_store.get_booking(self.body['request_id'])['status'],'BOOKED')
        status,error=self.reserve(generation=old);self.assertEqual(status,409);self.assertEqual(error['error']['code'],'STALE_ROUND')
        self.assertEqual(self.state()['bookings'],[])

    def test_restart_keeps_current_round_and_bookings(self):
        self.reserve();reopened=DemoWorkspace(self.path)
        self.assertEqual(reopened.generation,self.workspace.generation)
        self.assertEqual(reopened.state('upgrade')['bookings'][0]['status'],'BOOKED')

    def test_unmarked_nonempty_directory_is_not_modified(self):
        folder=Path(self.tmp.name)/'foreign';folder.mkdir();(folder/'notes.txt').write_text('keep')
        with self.assertRaises(ValueError):DemoWorkspace(folder)
        self.assertEqual([p.name for p in folder.iterdir()],['notes.txt'])

    def test_missing_current_database_does_not_silently_reset(self):
        target=self.path/self.workspace.generation/'strict.db';target.unlink()
        with self.assertRaisesRegex(ValueError,'incomplete'):DemoWorkspace(self.path)
        self.assertFalse(target.exists())

    def test_cross_site_and_missing_csrf_blocked_before_mutation(self):
        body={'mode':'upgrade','generation':self.workspace.generation,'request':self.body}
        for headers in ({'Origin':'https://attacker.invalid'},{'X-Demo-CSRF':''},{'Origin':'null'}):
            with self.subTest(headers=headers):
                status,_,data=self.call('/api/reserve','POST',body,headers);self.assertEqual(status,403)
        self.assertEqual(self.state()['bookings'],[])

    def test_unknown_host_blocked_and_no_cors_leak(self):
        status,headers,data=self.call(headers={'Host':'attacker.invalid'})
        self.assertEqual(status,403);self.assertNotIn('Access-Control-Allow-Origin',headers)

    def test_malformed_input_never_creates_booking(self):
        for changes in ({'child_ages':[-1]},{'adults':True},{'accepted_bed_types':'king'},
                        {'check_in':'20261207'},{'request_id':'../../bad'}):
            with self.subTest(changes=changes):
                status,error=self.reserve(request={**self.body,**changes});self.assertEqual(status,400)
        self.assertEqual(self.state()['bookings'],[])

    def test_body_size_and_content_type_limits(self):
        status,_,_=self.call('/api/reserve','POST',{'data':'x'*40000});self.assertEqual(status,413)
        status,_,_=self.call('/api/reserve','POST',{},headers={'Content-Type':'text/plain'});self.assertEqual(status,415)

    def test_no_get_mutations_or_static_path_traversal(self):
        for path in ('/api/reset','/api/reserve','/../hotel_sim/sqlite_store.py','/%2e%2e/README.md'):
            with self.subTest(path=path):status,_,_=self.call(path);self.assertEqual(status,404)
        self.assertEqual(self.state()['bookings'],[])

    def test_immutable_request_conflict_is_explicit(self):
        self.reserve();status,error=self.reserve(request={**self.body,'adults':self.body['adults']+1})
        self.assertEqual(status,409);self.assertEqual(error['error']['code'],'IDEMPOTENCY_CONFLICT')
        self.assertEqual(len(self.state()['bookings']),1)

    def test_html_has_csrf_and_security_headers(self):
        status,headers,data=self.call('/')
        self.assertEqual(status,200);self.assertIn(self.server.csrf.encode(),data)
        self.assertNotIn(b'__DEMO_CSRF__',data);self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])

if __name__=='__main__':unittest.main()
