"""Generate fully synthetic, versioned demand. Never reads guest data or networks."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import random


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)+'\n', encoding='utf-8')


def week(seed=20261106):
    rng=random.Random(seed)
    first=date(2026,12,7)
    days=[first+timedelta(days=i) for i in range(10)]
    types=[dict(code='standard',rank=0,max_occupants=3,max_adults=2,max_extra_beds=1,max_cots=1,
                max_auxiliary_items=1,base_rate_cents=18000),
           dict(code='deluxe',rank=1,max_occupants=4,max_adults=3,max_extra_beds=1,max_cots=1,
                max_auxiliary_items=2,base_rate_cents=24000),
           dict(code='family',rank=2,max_occupants=5,max_adults=4,max_extra_beds=1,max_cots=1,
                max_auxiliary_items=1,base_rate_cents=32000),
           dict(code='suite',rank=3,max_occupants=5,max_adults=4,max_extra_beds=2,max_cots=1,
                max_auxiliary_items=2,base_rate_cents=46000)]
    rooms=[dict(room_id=rid,room_type=typ,bed_type=bed,fixed_bed_places=places,blocked_dates=blocked)
           for rid,typ,bed,places,blocked in [
               ('S101','standard','king',2,[]),('S102','standard','king',2,[days[3].isoformat()]),
               ('S103','standard','twin',2,[]),('S104','standard','twin',2,[]),
               ('D201','deluxe','king',2,[days[1].isoformat()]),
               ('F301','family','family',4,[]),('F302','family','family',4,[days[5].isoformat()]),
               ('Q401','suite','king',2,[])]]
    demand_counts=[6,8,12,18,16,10,5]
    requests=[]
    for index,count in enumerate(demand_counts):
        for _ in range(count):
            typ=rng.choices(['standard','deluxe','family','suite'],[.58,.20,.17,.05])[0]
            adults=rng.choices([1,2,3],[.2,.7,.1])[0]
            # Synthetic age at arrival only, no birthdays, names or contacts.
            child_count=rng.choices([0,1,2],[.60,.30,.10])[0]
            ages=[rng.choice([0,1,2,5,9,15]) for _ in range(child_count)]
            if typ=='family':beds=['family']
            elif typ in ('deluxe','suite'):beds=['king']
            elif ages:beds=rng.choice([['king','twin','family'],['twin'],['king']])
            else:beds=rng.choice([['king'],['twin'],['king','twin']])
            length=rng.choice([1,1,2,2,3])
            requests.append(dict(request_id=f'SYN-{len(requests)+1:03d}',check_in=days[index].isoformat(),
                check_out=(days[index]+timedelta(days=length)).isoformat(),requested_type=typ,
                accepted_bed_types=beds,adults=adults,child_ages=ages,allow_upgrade=rng.random()<.85,
                allow_extra_bed=rng.random()<.85,max_upgrade_cost_cents=rng.choice([15000,30000,60000])))
    return dict(schema_version=1,data_origin='100% synthetic; generated with Python random.Random; no real guests',
        generator_seed=seed,description='Seven arrival-demand days, ten inventory nights, eight physical rooms; no trained forecast.',
        horizon_start=days[0].isoformat(),horizon_end=(days[-1]+timedelta(days=1)).isoformat(),cot_age_limit=2,
        room_types=types,rooms=rooms,allowed_upgrades={'standard':['deluxe','family','suite'],'deluxe':['suite'],'family':['suite']},
        daily_resources=[dict(date=day.isoformat(),cots=1 if i in (3,4) else 2,extra_beds=2 if i==4 else 3) for i,day in enumerate(days)],
        daily_rates=[dict(room_type=typ['code'],date=day.isoformat(),rate_cents=typ['base_rate_cents']+(8000 if i in (4,5) else 0))
                     for typ in types for i,day in enumerate(days)],
        protection=[dict(room_type=typ,date=days[i].isoformat(),count=1) for typ in ('family','suite') for i in (3,4,5)],
        requests=requests)


def counterexample():
    data=week()
    data['description']='Deliberate counterexample: accepting one two-night upgrade can displace two native one-night requests.'
    data['horizon_end']='2026-12-09'
    data['room_types']=data['room_types'][:2]
    data['rooms']=[dict(room_id='D201',room_type='deluxe',bed_type='king',fixed_bed_places=2,blocked_dates=[])]
    data['allowed_upgrades']={'standard':['deluxe']}
    data['daily_resources']=data['daily_resources'][:2]
    data['daily_rates']=[]
    data['protection']=[dict(room_type='deluxe',date=day,count=1) for day in ('2026-12-07','2026-12-08')]
    data['requests']=[dict(request_id=key,check_in=start,check_out=end,requested_type=typ,accepted_bed_types=['king'],
                           adults=2,child_ages=[],allow_upgrade=True,allow_extra_bed=True,max_upgrade_cost_cents=20000)
                      for key,start,end,typ in [('A','2026-12-07','2026-12-09','standard'),
                                               ('B','2026-12-07','2026-12-08','deluxe'),
                                               ('C','2026-12-08','2026-12-09','deluxe')]]
    return data


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path('data'))
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    write(args.output_dir/'week.json',week());write(args.output_dir/'upgrade_counterexample.json',counterexample())

if __name__=='__main__':main()
