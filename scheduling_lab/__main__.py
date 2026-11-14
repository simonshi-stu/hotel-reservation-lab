from .scheduler import Task, schedule
import json

sample = [Task(1, 3, 0, 1), Task(2, 1, 1, 3), Task(3, 2, 2, 1)]
print(json.dumps({mode: schedule(sample, preemptive=(mode == 'preemptive'))
                  for mode in ('preemptive', 'nonpreemptive')}, indent=2))
