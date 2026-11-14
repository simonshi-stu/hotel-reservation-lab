"""Event-based simulator derived from the owner's heapq exercise.

Larger priority wins; ties use original arrival then input position. Jobs use
integer time units, positive durations and distinct integer IDs. Preemption
has zero overhead. No aging, parallel workers, IO, deadlines or booking writes.
"""
from dataclasses import dataclass
import heapq


@dataclass(frozen=True)
class Task:
    id: int
    priority: int
    arrival: int
    duration: int


def schedule(tasks, *, preemptive=True):
    tasks = list(tasks)
    ids = set()
    for t in tasks:
        if any(type(v) is not int for v in (t.id, t.priority, t.arrival, t.duration)):
            raise ValueError('task fields must be integers')
        if t.id in ids or t.arrival < 0 or t.duration <= 0:
            raise ValueError('distinct IDs, nonnegative arrivals, positive durations required')
        ids.add(t.id)
    arrivals = sorted(enumerate(tasks), key=lambda x: (x[1].arrival, x[0]))
    heap, segments, completions = [], [], []
    now = i = 0
    while i < len(arrivals) or heap:
        if not heap:
            now = max(now, arrivals[i][1].arrival)
        while i < len(arrivals) and arrivals[i][1].arrival <= now:
            idx, t = arrivals[i]
            heapq.heappush(heap, (-t.priority, t.arrival, idx, t.id, t.duration))
            i += 1
        p, a, idx, tid, remaining = heapq.heappop(heap)
        run = remaining
        if preemptive and i < len(arrivals):
            run = min(run, arrivals[i][1].arrival - now)
        end = now + run
        if segments and segments[-1]['id'] == tid and segments[-1]['end'] == now:
            segments[-1]['end'] = end
        else:
            segments.append({'id': tid, 'start': now, 'end': end})
        now = end
        remaining -= run
        if remaining:
            heapq.heappush(heap, (p, a, idx, tid, remaining))
        else:
            completions.append({'id': tid, 'time': now})
    completion_times = {x['id']: x['time'] for x in completions}
    return {'order': [x['id'] for x in completions], 'segments': segments,
            'completions': completions,
            'waiting': {str(t.id): completion_times[t.id] - t.arrival - t.duration for t in tasks}}
