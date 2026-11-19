import json
import sqlite3
import unittest
import test_demo_http as support

class AvailabilityTests(unittest.TestCase):
    setUp = support.DemoHTTPTests.setUp
    tearDown = support.DemoHTTPTests.tearDown
    call = support.DemoHTTPTests.call

    def get(self, query='mode=protected_upgrade'):
        status, _, data = self.call('/api/availability?'+query)
        return status, json.loads(data)

    def test_fourteen_days_outside_is_unknown_not_zero(self):
        status, data = self.get()
        self.assertEqual(status,200)
        self.assertEqual(len(data['days']),14)
        self.assertEqual(sum(d['available'] for d in data['days']),10)
        for day in data['days'][10:]:
            self.assertEqual(day['types'],[])
            self.assertNotIn('extra_beds_remaining',day)
            self.assertEqual(day['reason'],'OUTSIDE_INVENTORY_HORIZON')
        self.assertNotIn('bookings',data)
        self.assertNotIn('request_id',json.dumps(data))

    def test_configured_and_active_protection_are_distinct(self):
        _, strict=self.get('mode=strict')
        _, protected=self.get()
        self.assertTrue(all(t['active_protection']==0 for d in strict['days'] for t in d['types']))
        self.assertTrue(any(t['active_protection']>0 for d in protected['days'] for t in d['types']))

    def test_preview_does_not_write_and_no_occupant_identifiers(self):
        store=self.workspace._store('upgrade')
        req=self.body.copy();req['request_id']='SECRET-OCCUPANT-KEY'
        from hotel_sim.scenario import parse_requests
        store.reserve(parse_requests({'requests':[req]})[0])
        def dump():
            with sqlite3.connect(store.path) as conn:return '\n'.join(conn.iterdump())
        before=dump()
        status,data=self.get('mode=upgrade&check_in=2026-12-07&check_out=2026-12-09&room_type=standard&bed=king&adults=2')
        self.assertEqual(status,200)
        self.assertIsNotNone(data['preview'])
        self.assertEqual(dump(),before)
        self.assertNotIn('SECRET-OCCUPANT-KEY',json.dumps(data))
        self.assertNotIn('request_id',json.dumps(data))

    def test_outside_stay_and_capacity_reasons(self):
        base='mode=upgrade&room_type=standard&bed=king&adults=2'
        status,data=self.get(base+'&check_in=2026-12-16&check_out=2026-12-18')
        self.assertEqual(status,200)
        self.assertFalse(data['preview']['accepted'])
        self.assertEqual(data['preview']['rejection_reasons'],['OUTSIDE_INVENTORY_HORIZON'])
        _,data=self.get(base.replace('adults=2','adults=99')+'&check_in=2026-12-07&check_out=2026-12-09')
        self.assertFalse(data['preview']['accepted'])
        self.assertIn('PARTY_OR_CHILD_REQUIREMENTS_UNSUPPORTED',data['preview']['rejection_reasons'])

    def test_invalid_queries_are_errors(self):
        proposal='mode=strict&check_in=2026-12-07&check_out=2026-12-09&room_type=standard&bed=king&adults=2'
        for query in ['start=bad','start=20261207','start=9999-12-31','mode=bad','mode=','mode=strict&mode=upgrade','unknown=x','start',
                      'check_in=2026-12-07',proposal+'&child_ages=1,,8',proposal+'&allow_upgrade=yes',proposal.replace('bed=king','bed=unknown'),
                      proposal.replace('adults=2','adults=2.5'),proposal.replace('check_out=2026-12-09','check_out=2026-12-06')]:
            with self.subTest(query=query):
                status,_=self.get(query);self.assertEqual(status,400)

    def test_page_and_script_served_without_ledger(self):
        for path in ['/availability','/availability.js']:
            status,_,body=self.call(path);self.assertEqual(status,200)
            self.assertNotIn(b'/api/state',body)
            self.assertNotIn(b'/api/reserve',body)
