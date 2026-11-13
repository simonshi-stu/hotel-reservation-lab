import unittest
from scripts.benchmark_http import percentile, run_level


class HTTPBenchmarkTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        self.assertEqual(percentile([1, 2, 3, 4], .5), 2)
        self.assertEqual(percentile([1, 2, 3, 4], .95), 4)

    def test_small_concurrent_run_uses_real_http_and_checks_database(self):
        result = run_level(2, 4)
        self.assertTrue(result["all_checks_passed"], result)
        self.assertEqual(result["final_booked"], 4)
        self.assertEqual(result["final_room_nights"], 12)
        for phase in result["phases"].values():
            self.assertEqual(phase["http_counts"], {"200": 4})
            self.assertEqual(phase["transport_errors"], 0)
