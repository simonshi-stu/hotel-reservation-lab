import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from hotel_sim.replay import MODES, canonical_bytes, replay
from hotel_sim.scenario import load_scenario, simulate

ROOT = Path(__file__).resolve().parents[1]

class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.data, self.digest = load_scenario(ROOT / 'data/upgrade_counterexample.json')

    def test_deterministic_and_no_input_mutation(self):
        original = copy.deepcopy(self.data)
        one = replay(self.data, source_sha256=self.digest)
        self.assertEqual(canonical_bytes(one), canonical_bytes(replay(self.data, source_sha256=self.digest)))
        self.assertEqual(self.data, original)
        self.assertEqual(one['replay']['effective_input_sha256'], hashlib.sha256(canonical_bytes(one['replay']['effective_input'])).hexdigest())

    def test_existing_week_and_counterexample_results_preserved(self):
        for name, expected in [('week.json', [26, 27, 27]), ('upgrade_counterexample.json', [2, 1, 2])]:
            data, digest = load_scenario(ROOT / 'data' / name)
            result = replay(data, source_sha256=digest)
            baseline = simulate(data, digest)
            self.assertEqual([result['policies'][m]['summary']['accepted'] for m in MODES], expected)
            for mode in MODES:
                self.assertEqual(result['policies'][mode]['summary'], baseline['policies'][mode]['summary'])
                self.assertEqual(result['policies'][mode]['decisions'], baseline['policies'][mode]['decisions'])

    def test_empty_override_allows_upgrade_displacing_native_demand(self):
        unprotected, protected = replay(self.data, []), replay(self.data)
        self.assertEqual(unprotected['policies']['protected_upgrade']['summary']['accepted'], 1)
        self.assertEqual(protected['policies']['protected_upgrade']['summary']['accepted'], 2)
        self.assertNotEqual(unprotected['input_sha256'], protected['input_sha256'])
        for mode in ('strict', 'upgrade'):
            self.assertEqual(unprotected['policies'][mode], protected['policies'][mode])

    def test_one_date_protection_blocks_entire_two_night_upgrade(self):
        result = replay(self.data, [self.data['protection'][1]])
        steps = result['policies']['protected_upgrade']['steps']
        self.assertFalse(steps[0]['decision']['accepted'])
        self.assertIn('PROTECTED_INVENTORY', steps[0]['decision']['rejection_reasons'])
        self.assertEqual(steps[0]['daily_changes'], [])
        self.assertTrue(steps[1]['decision']['accepted'])
        self.assertTrue(steps[2]['decision']['accepted'])
        self.assertEqual(steps[2]['daily_changes'][0]['remaining_by_type']['deluxe'], 0)

    def test_trace_reconciles_room_nights(self):
        data, _ = load_scenario(ROOT / 'data/week.json')
        for report in replay(data)['policies'].values():
            consumed = -sum(sum(day['room_delta'].values()) for step in report['steps'] for day in step['daily_changes'])
            self.assertEqual(consumed, report['summary']['accepted_room_nights'])
            self.assertTrue(all(not step['daily_changes'] for step in report['steps'] if not step['decision']['accepted']))

    def test_schedule_order_is_canonical(self):
        self.assertEqual(replay(self.data, self.data['protection']), replay(self.data, self.data['protection'][::-1]))

    def test_invalid_protection_rejected(self):
        row = self.data['protection'][0]
        invalid = [[dict(row, count=-1)], [dict(row, count=True)], [dict(row, count=2)],
                   [dict(row, date='2026-12-09')], [dict(row, date='20261207')],
                   [dict(row, room_type='missing')], [dict(row, room_type='standard', count=0)],
                   [row, row], [dict(row, unknown=True)], 'not-array']
        for schedule in invalid:
            with self.subTest(schedule=schedule), self.assertRaises(ValueError):
                replay(self.data, schedule)

    def test_cli_is_repeatable_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            outputs = [Path(folder) / name for name in ('one.json', 'two.json')]
            cmd = [sys.executable, '-B', '-m', 'hotel_sim.replay', '--input', 'data/upgrade_counterexample.json']
            for output in outputs:
                result = subprocess.run(cmd + ['--output', str(output)], cwd=ROOT, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
            result = subprocess.run(cmd + ['--output', str(outputs[0])], cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
