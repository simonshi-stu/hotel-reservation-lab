"""Independent sequential booking/cancellation state model (no allocator call)."""
import sys,json,tempfile,random,hashlib
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).parent))
from audit_sqlite import SQLiteHotel,config,req,OUT,ROOT,rawstate,DAY
from hotel_sim.sqlite_store import InvalidBookingState

def main():
 report={'seeds':[],'source_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hotel_sim').glob('sqlite_*.py')}}
 with tempfile.TemporaryDirectory(prefix='state_',dir=OUT) as td:
  for seed in range(5):
   rng=random.Random(78211+seed);s=SQLiteHotel(Path(td)/f'{seed}.db');s.initialize(config(3,2));model={};active={};used={i:0 for i in range(3)};events=0;counts={'new':0,'replay':0,'cancel':0}
   for step in range(200):
    chance=rng.random()
    if not model or chance<0.5:
     key=f'K{step}';start=rng.randrange(3);end=rng.randrange(start+1,4);infant=bool(rng.randrange(2));request=req(key,start,end,infant)
     room=next((f'S{r}' for r in range(3) if all((f'S{r}',d) not in active and used[d]+infant<=2 for d in range(start,end))),None)
     expected='BOOKED' if room is not None else 'REJECTED';got=s.reserve(request)
     assert got['status']==expected,(seed,step,got,expected)
     assert got['original_decision']['room_id']==room,(seed,step,got,room)
     model[key]={'request':request,'status':expected,'original':got['original_decision'],'room':room,'days':list(range(start,end)),'cots':int(infant)}
     if room is not None:
      for d in range(start,end):active[room,d]=key;used[d]+=infant
     events+=1;counts['new']+=1
    elif chance<0.75:
     key=rng.choice(list(model));m=model[key];got=SQLiteHotel(s.path).reserve(m['request'])
     assert got['status']==m['status'] and got['original_decision']==m['original'];counts['replay']+=1
    else:
     key=rng.choice(list(model));m=model[key];counts['cancel']+=1
     if m['status']=='REJECTED':
      try:s.cancel(key)
      except InvalidBookingState:pass
      else:raise AssertionError('rejected cancellation succeeded')
     else:
      got=s.cancel(key);assert got['status']=='CANCELLED' and got['original_decision']==m['original']
      if m['status']=='BOOKED':
       for d in m['days']:del active[m['room'],d];used[d]-=m['cots']
       m['status']='CANCELLED';events+=1
    state=rawstate(s)
    assert len(state['reservation_events'])==events
    actual={(r[0],( __import__('datetime').date.fromisoformat(r[1])-DAY).days):r[2] for r in state['active_room_nights']}
    assert actual==active,(seed,step,actual,active)
    assert {i:r[3] for i,r in enumerate(state['auxiliary_inventory'])}==used
    assert {r[0]:r[4] for r in state['bookings']}=={k:m['status'] for k,m in model.items()}
    assert s.reconcile()['healthy'],(seed,step)
   report['seeds'].append({'seed':78211+seed,'operations':200,'counts':counts,'unique_requests':len(model),'events':events})
 report['source_hashes_after']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hotel_sim').glob('sqlite_*.py')}
 report['all_passed']=True
 (OUT/'state_machine_results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
