import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from hotel_sim.scenario import build_allocator, load_scenario, parse_requests, simulate

ROOT=Path(__file__).resolve().parent.parent

class ScenarioTests(unittest.TestCase):
    def setUp(self):self.data,self.digest=load_scenario(ROOT/'data/week.json')

    def test_repeat_is_deterministic_without_input_mutation(self):
        original=copy.deepcopy(self.data)
        one=simulate(self.data,self.digest);two=simulate(self.data,self.digest)
        self.assertEqual(one,two);self.assertEqual(self.data,original)
        self.assertEqual(one['input_sha256'],hashlib.sha256((ROOT/'data/week.json').read_bytes()).hexdigest())

    def test_explicit_variable_arrival_demand(self):
        result=simulate(self.data)
        self.assertEqual(list(result['daily_arrival_demand'].values()),[6,8,12,18,16,10,5])
        self.assertEqual(result['input_request_count'],75)
        for mode in result['policies'].values():
            summary=mode['summary']
            self.assertEqual(summary['accepted']+summary['rejected'],75)
            self.assertEqual(summary['available_room_nights'],77)
            self.assertEqual(summary['accepted_room_nights'],sum(len(d['nights']) for d in mode['decisions'] if d['accepted']))

    def test_generator_reproduces_saved_inputs_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable,'scripts/generate_demo.py','--output-dir',tmp],cwd=ROOT,check=True)
            for name in ('week.json','upgrade_counterexample.json'):
                self.assertEqual((ROOT/'data'/name).read_bytes(),(Path(tmp)/name).read_bytes())

    def test_cli_actual_subprocess_repeatable_and_report_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            files=[Path(tmp)/'one.json',Path(tmp)/'two.json']
            for output in files:
                result=subprocess.run([sys.executable,'-m','hotel_sim','--input','data/week.json','--output',str(output)],
                    cwd=ROOT,text=True,capture_output=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(files[0].read_bytes(),files[1].read_bytes())
            report=json.loads(files[0].read_text())
            self.assertEqual(report['input_sha256'],self.digest)
            self.assertEqual([report['policies'][mode]['summary']['accepted'] for mode in ('strict','upgrade','protected_upgrade')],[26,27,27])

    def test_duplicate_request_ids_rejected(self):
        self.data['requests'].append(copy.deepcopy(self.data['requests'][0]))
        with self.assertRaisesRegex(ValueError,'unique'):parse_requests(self.data)

    def test_missing_or_duplicate_daily_resource_is_not_assumed_unlimited(self):
        shortened=copy.deepcopy(self.data);shortened['daily_resources'].pop()
        with self.assertRaisesRegex(ValueError,'every inventory date'):build_allocator(shortened,'strict')
        self.data['daily_resources'].append(copy.deepcopy(self.data['daily_resources'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate resource date'):build_allocator(self.data,'strict')

    def test_duplicate_rate_is_rejected(self):
        self.data['daily_rates'].append(copy.deepcopy(self.data['daily_rates'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate nightly rate'):build_allocator(self.data,'strict')

    def test_saved_upgrade_counterexample(self):
        data,digest=load_scenario(ROOT/'data/upgrade_counterexample.json');report=simulate(data,digest)
        self.assertEqual([report['policies'][m]['summary']['accepted'] for m in ('strict','upgrade','protected_upgrade')],[2,1,2])
        self.assertEqual([report['policies'][m]['summary']['accepted_room_nights'] for m in ('strict','upgrade','protected_upgrade')],[2,2,2])

    def test_noncanonical_dates_cannot_silently_override_inventory(self):
        for field in ("daily_resources", "daily_rates"):
            with self.subTest(field=field):
                data=copy.deepcopy(self.data)
                duplicate=copy.deepcopy(data[field][0]);duplicate["date"]=duplicate["date"].replace("-","")
                data[field].append(duplicate)
                with self.assertRaisesRegex(ValueError,"YYYY-MM-DD"):build_allocator(data,"upgrade")
                data[field].reverse()
                with self.assertRaisesRegex(ValueError,"YYYY-MM-DD"):build_allocator(data,"upgrade")

    def test_other_date_fields_require_canonical_format(self):
        data=copy.deepcopy(self.data);data["requests"][0]["check_in"]="20261207"
        with self.assertRaisesRegex(ValueError,"YYYY-MM-DD"):parse_requests(data)
        data=copy.deepcopy(self.data);data["rooms"][0]["blocked_dates"]=["20261207"]
        with self.assertRaisesRegex(ValueError,"YYYY-MM-DD"):build_allocator(data,"strict")

    def test_array_fields_reject_strings_instead_of_splitting_constraints(self):
        for field,value in (("accepted_bed_types","king"),("child_ages","12")):
            data=copy.deepcopy(self.data);data["requests"][0][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,"JSON array"):parse_requests(data)
        data=copy.deepcopy(self.data);data["allowed_upgrades"]["standard"]="deluxe"
        with self.assertRaisesRegex(ValueError,"JSON array"):build_allocator(data,"upgrade")
        data=copy.deepcopy(self.data);data["rooms"][0]["blocked_dates"]="2026-12-07"
        with self.assertRaisesRegex(ValueError,"JSON array"):build_allocator(data,"upgrade")

if __name__=='__main__':unittest.main()
