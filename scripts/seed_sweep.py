"""Explore 30 synthetic demand draws; not a calibrated forecast or real A/B test."""
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from hotel_sim.scenario import simulate
from generate_demo import week

runs=[]
for seed in range(20261106,20261136):
    report=simulate(week(seed))
    runs.append({'seed':seed,**{mode:r['summary'] for mode,r in report['policies'].items()}})
summary={}
for mode in ('strict','upgrade','protected_upgrade'):
    summary[mode]={key:round(statistics.mean(run[mode][key] for run in runs),3)
                   for key in ('accepted','accepted_room_nights','upgraded','modeled_upgrade_cost_cents','accepted_with_children')}
comparisons={}
for mode in ('upgrade','protected_upgrade'):
    comparisons[mode]={
        'more_accepted_than_strict':sum(r[mode]['accepted']>r['strict']['accepted'] for r in runs),
        'equal_accepted_to_strict':sum(r[mode]['accepted']==r['strict']['accepted'] for r in runs),
        'fewer_accepted_than_strict':sum(r[mode]['accepted']<r['strict']['accepted'] for r in runs)}
output={'synthetic_only':True,'runs':len(runs),'design':'Fixed capacities, arrival counts, policy and protection; vary demand seed. No fitting or holdout.',
        'mean_metrics':summary,'comparisons':comparisons,'individual_runs':runs}
Path('results').mkdir(parents=True, exist_ok=True)
Path('results/seed-sweep.json').write_text(json.dumps(output,ensure_ascii=False,indent=2,sort_keys=True)+'\n')
print(json.dumps({'mean_metrics':summary,'comparisons':comparisons},ensure_ascii=False,indent=2))
