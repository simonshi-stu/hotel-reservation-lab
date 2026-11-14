import random
import unittest
from scheduling_lab.scheduler import Task, schedule


def oracle(tasks):
    """Independent tick-by-tick integer-time implementation (no heap)."""
    remaining = [t.duration for t in tasks]
    now, done = 0, []
    while len(done) < len(tasks):
        eligible = [i for i, t in enumerate(tasks) if remaining[i] and t.arrival <= now]
        if not eligible:
            now += 1
            continue
        i = min(eligible, key=lambda i: (-tasks[i].priority, tasks[i].arrival, i))
        remaining[i] -= 1
        now += 1
        if not remaining[i]:
            done.append({'id': tasks[i].id, 'time': now})
    return done


class SchedulerTests(unittest.TestCase):
    def test_original_example(self):
        jobs = [Task(1, 3, 0, 1), Task(2, 1, 1, 3), Task(3, 2, 2, 1)]
        self.assertEqual(schedule(jobs)['order'], [1, 3, 2])
        self.assertEqual(schedule(jobs, preemptive=False)['order'], [1, 2, 3])
        self.assertEqual(schedule(jobs)['waiting'], {'1': 0, '2': 1, '3': 0})

    def test_empty(self):
        self.assertEqual(schedule([])['order'], [])

    def test_ties_input_order_not_id(self):
        self.assertEqual(schedule([Task(9, 2, 0, 2), Task(1, 2, 0, 1)])['order'], [9, 1])

    def test_equal_priority_does_not_displace_earlier(self):
        self.assertEqual(schedule([Task(1, 2, 0, 4), Task(2, 2, 1, 1)])['order'], [1, 2])

    def test_idle_and_unsorted(self):
        self.assertEqual(schedule([Task(2, 1, 10, 1), Task(1, 1, 3, 2)])['completions'],
                         [{'id': 1, 'time': 5}, {'id': 2, 'time': 11}])

    def test_exact_boundary(self):
        self.assertEqual(schedule([Task(1, 1, 0, 2), Task(2, 5, 2, 1)])['order'], [1, 2])

    def test_simultaneous_arrivals_grouped(self):
        self.assertEqual(schedule([Task(1, 1, 0, 1), Task(2, 4, 0, 1)])['order'], [2, 1])

    def test_validation(self):
        for tasks in ([Task(1, 1, -1, 2)], [Task(1, 1, 0, 0)],
                      [Task(1, 1, 0, 1), Task(1, 2, 1, 2)], [Task(1, 1, 0, True)]):
            with self.assertRaises(ValueError):
                schedule(tasks)

    def test_seeded_oracle_1000_cases(self):
        rng = random.Random(20261114)
        for _ in range(1000):
            jobs = [Task(i, rng.randint(-2, 5), rng.randint(0, 12), rng.randint(1, 7))
                    for i in range(rng.randint(1, 12))]
            rng.shuffle(jobs)
            result = schedule(jobs)
            self.assertEqual(result['completions'], oracle(jobs))
            for t in jobs:
                elapsed = sum(s['end'] - s['start'] for s in result['segments'] if s['id'] == t.id)
                self.assertEqual(elapsed, t.duration)
                self.assertGreaterEqual(result['waiting'][str(t.id)], 0)
