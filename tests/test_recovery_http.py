"""Real HTTP / SQLite recovery tests; all business inputs are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from hotel_demo.server import DemoServer, DemoWorkspace
from hotel_sim.sqlite_store import SQLiteHotel

ROOT = Path(__file__).resolve().parent.parent


class RecoveryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state'
        self.workspace = DemoWorkspace(self.path)
        self.server = DemoServer(('127.0.0.1', 0), self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.payload = {'mode': 'upgrade', 'generation': self.workspace.generation,
                        'request': json.loads((ROOT/'data/sqlite_demo_request.json').read_text()),
                        'simulate_failure': False}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, route, body, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        merged = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Demo-CSRF': self.server.csrf}
        merged.update(headers or {})
        connection.request('POST', '/api/' + route, json.dumps(body), merged)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def reserve(self, payload=None):
        status, result = self.post('reserve', payload or self.payload)
        self.assertEqual(status, 200, result)
        return result

    def lookup(self, kind='reserve', payload=None):
        return self.post('lookup', {'kind': kind, 'payload': payload or self.payload})

    def reset(self):
        status, result = self.post('reset', {'generation': self.workspace.generation, 'mode': 'upgrade'})
        self.assertEqual(status, 200, result)
        return result

    def cancel_payload(self):
        return {'generation': self.payload['generation'], 'mode': 'upgrade',
                'request_id': self.payload['request']['request_id']}

    def db(self):
        return self.path / self.payload['generation'] / 'upgrade.db'

    def ledger(self, path=None):
        with SQLiteHotel(path or self.db()).read_snapshot() as connection:
            return '\n'.join(connection.iterdump())

    def test_archived_committed_reservation_recovered_without_retry(self):
        original = self.reserve()
        before = self.ledger()
        current = self.reset()['generation']
        with patch.object(SQLiteHotel, 'reserve', side_effect=AssertionError('no retry')), \
             patch.object(SQLiteHotel, 'cancel', side_effect=AssertionError('no cancel')), \
             patch.object(SQLiteHotel, 'initialize', side_effect=AssertionError('no init')), \
             patch.object(SQLiteHotel, 'transaction', side_effect=AssertionError('no write')):
            status, found = self.lookup()
        self.assertEqual(status, 200, found)
        self.assertEqual(found['record'], original['record'])
        self.assertEqual(found['outcome'], 'RECORDED')
        self.assertTrue(found['read_only'] and found['snapshot_only'] and found['archived'])
        self.assertEqual(found['current_generation'], current)
        self.assertEqual(before, self.ledger())
        self.assertEqual(self.workspace.state('upgrade')['bookings'], [])

    def test_current_round_can_be_checked_without_creating_unknown_request(self):
        before = self.ledger()
        status, found = self.lookup()
        self.assertEqual(status, 200, found)
        self.assertEqual(found['status'], 'NOT_FOUND')
        self.assertEqual(found['outcome'], 'NOT_RECORDED')
        self.assertIsNone(found['record'])
        self.assertFalse(found['archived'])
        self.assertEqual(before, self.ledger())

    def test_archived_rolled_back_reservation_is_absent_not_rebooked(self):
        self.reserve({**self.payload, 'simulate_failure': True})
        self.reset()
        before = self.ledger()
        status, found = self.lookup()
        self.assertEqual(status, 200)
        self.assertEqual(found['outcome'], 'NOT_RECORDED')
        self.assertEqual(before, self.ledger())

    def test_cancelled_result_and_original_reservation_are_both_recoverable(self):
        self.reserve()
        status, _ = self.post('cancel', self.cancel_payload())
        self.assertEqual(status, 200)
        self.reset()
        before = self.ledger()
        for kind, payload, outcome in (
            ('reserve', self.payload, 'RECORDED'),
            ('cancel', self.cancel_payload(), 'CANCELLED')
        ):
            status, found = self.lookup(kind, payload)
            self.assertEqual(status, 200, found)
            self.assertEqual(found['status'], 'CANCELLED')
            self.assertEqual(found['outcome'], outcome)
        self.assertEqual(before, self.ledger())

    def test_uncommitted_cancellation_never_executes_during_lookup(self):
        self.reserve()
        self.reset()
        before = self.ledger()
        status, found = self.lookup('cancel', self.cancel_payload())
        self.assertEqual(status, 200)
        self.assertEqual(found['status'], 'BOOKED')
        self.assertEqual(found['outcome'], 'NOT_CANCELLED')
        self.assertEqual(before, self.ledger())

    def test_absent_cancel_is_not_claimed_successful(self):
        self.reset()
        status, found = self.lookup('cancel', self.cancel_payload())
        self.assertEqual(status, 200)
        self.assertEqual(found['outcome'], 'NOT_RECORDED')

    def test_rejected_reservation_is_a_recorded_decision(self):
        bad = deepcopy(self.payload)
        bad['request']['accepted_bed_types'] = ['family']
        bad['request']['allow_upgrade'] = False
        self.assertEqual(self.reserve(bad)['status'], 'REJECTED')
        self.reset()
        status, found = self.lookup(payload=bad)
        self.assertEqual(status, 200, found)
        self.assertEqual(found['status'], 'REJECTED')
        self.assertEqual(found['outcome'], 'RECORDED')
        status, found = self.lookup('cancel', self.cancel_payload())
        self.assertEqual(found['outcome'], 'NOT_CANCELLED')

    def test_original_content_must_match_even_when_key_exists(self):
        self.reserve()
        self.reset()
        wrong = deepcopy(self.payload)
        wrong['request']['adults'] += 1
        status, found = self.lookup(payload=wrong)
        self.assertEqual(status, 409, found)
        self.assertEqual(found['error']['code'], 'IDEMPOTENCY_CONFLICT')

    def test_unordered_constraints_use_same_canonical_identity(self):
        payload = deepcopy(self.payload)
        payload['request']['accepted_bed_types'] = ['twin', 'king']
        payload['request']['child_ages'] = [1, 5]
        self.reserve(payload)
        self.reset()
        payload['request']['accepted_bed_types'].reverse()
        payload['request']['child_ages'].reverse()
        payload['simulate_failure'] = True  # A lookup never injects a write failure.
        status, found = self.lookup(payload=payload)
        self.assertEqual(status, 200, found)
        self.assertEqual(found['outcome'], 'RECORDED')

    def test_lookup_scopes_same_key_by_mode_and_round(self):
        self.reserve()
        strict = {**self.payload, 'mode': 'strict'}
        status, absent = self.lookup(payload=strict)
        self.assertEqual(status, 200)
        self.assertEqual(absent['status'], 'NOT_FOUND')
        self.reset()
        new = {**self.payload, 'generation': self.workspace.generation}
        status, absent = self.lookup(payload=new)
        self.assertEqual(status, 200)
        self.assertEqual(absent['status'], 'NOT_FOUND')
        self.assertEqual(self.lookup()[1]['status'], 'BOOKED')

    def test_missing_round_never_creates_directory_or_database(self):
        payload = {**self.payload, 'generation': '0' * 32}
        status, result = self.lookup(payload=payload)
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'RECOVERY_UNAVAILABLE')
        self.assertFalse((self.path/payload['generation']).exists())

    def test_missing_database_is_not_not_found(self):
        self.reset()
        self.db().unlink()
        status, result = self.lookup()
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'RECOVERY_UNAVAILABLE')
        self.assertFalse(self.db().exists())

    def test_malformed_database_is_unconfirmed_not_repaired(self):
        self.reset()
        for sidecar in ('-wal', '-shm'):
            self.db().with_name(self.db().name+sidecar).unlink(missing_ok=True)
        self.db().write_bytes(b'not a sqlite database')
        status, result = self.lookup()
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'RECOVERY_UNAVAILABLE')
        self.assertEqual(self.db().read_bytes(), b'not a sqlite database')

    def test_inconsistent_ledger_does_not_confirm_existing_record(self):
        self.reserve()
        self.reset()
        with sqlite3.connect(self.db()) as connection:
            connection.execute('UPDATE auxiliary_inventory SET cots_used=0')
        before = self.ledger()
        status, result = self.lookup()
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'RECOVERY_UNAVAILABLE')
        self.assertEqual(before, self.ledger())

    def test_wrong_policy_database_is_not_accepted(self):
        self.reset()
        source = self.path/self.payload['generation']/'strict.db'
        # SQLite backup avoids copying potentially uncheckpointed WAL bytes.
        with sqlite3.connect(source) as source_db, sqlite3.connect(self.db()) as dest_db:
            source_db.backup(dest_db)
        status, result = self.lookup()
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'RECOVERY_UNAVAILABLE')

    def test_traversal_and_invalid_input_never_lookup_arbitrary_path(self):
        cases = [
            {'kind': 'reserve', 'payload': {**self.payload, 'generation': '../other'}},
            {'kind': 'reserve', 'payload': {**self.payload, 'mode': '../other'}},
            {'kind': 'reset', 'payload': self.payload},
            {'kind': 'cancel', 'payload': {**self.cancel_payload(), 'request_id': '../other'}},
            {'kind': 'reserve', 'payload': {**self.payload, 'request': {}}},
            {'kind': 'reserve', 'payload': None},
        ]
        for body in cases:
            with self.subTest(body=body):
                status, result = self.post('lookup', body)
                self.assertEqual(status, 400, result)

    def test_symlink_archive_is_rejected(self):
        self.reset()
        alternate = self.path/('a'*32)
        alternate.symlink_to(self.db().parent, target_is_directory=True)
        status, result = self.lookup(payload={**self.payload, 'generation': 'a'*32})
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'RECOVERY_UNAVAILABLE')

    def test_lookup_obeys_origin_csrf_and_body_size_checks(self):
        for headers in ({'Origin': 'https://attacker.invalid'}, {'X-Demo-CSRF': ''}):
            status, result = self.post('lookup', {'kind': 'reserve', 'payload': self.payload}, headers)
            self.assertEqual(status, 403, result)
        status, _ = self.post('lookup', {'x': 'x'*40000})
        self.assertEqual(status, 413)

    def test_restart_and_concurrent_duplicate_lookups_leave_inventory_unchanged(self):
        self.reserve()
        self.reset()
        self.workspace = DemoWorkspace(self.path)
        self.server.workspace = self.workspace
        before = self.ledger()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.lookup(), range(24)))
        self.assertTrue(all(s == 200 and r['outcome'] == 'RECORDED' for s, r in results))
        self.assertEqual(before, self.ledger())
        self.assertEqual(self.workspace.state('upgrade')['bookings'], [])

    def test_reset_race_cannot_retarget_original_lookup(self):
        self.reserve()
        original = self.payload['generation']
        with ThreadPoolExecutor(max_workers=2) as pool:
            lookup = pool.submit(self.lookup)
            reset = pool.submit(self.reset)
            status, found = lookup.result()
            new = reset.result()
        self.assertEqual(status, 200, found)
        self.assertEqual(found['generation'], original)
        self.assertEqual(found['status'], 'BOOKED')
        self.assertNotEqual(new['generation'], original)
        self.assertEqual(self.workspace.state('upgrade')['bookings'], [])


if __name__ == '__main__':
    unittest.main()
