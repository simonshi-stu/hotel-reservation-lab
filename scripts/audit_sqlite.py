"""Independent black-box checks; candidate source is read-only."""
import os, sys, json, sqlite3, tempfile, hashlib, multiprocessing as mp, time, traceback, random
from pathlib import Path
from dataclasses import replace
from datetime import date, timedelta
ROOT=Path(__file__).resolve().parent.parent
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from hotel_sim.sqlite_store import SQLiteHotel, DatabaseBusy, IdempotencyConflict, BookingNotFound
from hotel_sim.allocation import BookingRequest
from hotel_sim.scenario import build_allocator, parse_requests
OUT=ROOT/'results/sqlite/independent'
OUT.mkdir(parents=True,exist_ok=True)
DAY=date(2026,12,7)

def config(room_count=1,cots=1):
 return {'horizon_start':DAY.isoformat(),'horizon_end':(DAY+timedelta(days=3)).isoformat(),
 'room_types':[{'code':'S','rank':0,'max_occupants':4,'max_adults':3,'max_extra_beds':2,'max_cots':2,'base_rate_cents':100,'max_auxiliary_items':3}],
 'rooms':[{'room_id':f'S{i}','room_type':'S','bed_type':'king','fixed_bed_places':2} for i in range(room_count)],
 'daily_resources':[{'date':(DAY+timedelta(days=i)).isoformat(),'cots':cots,'extra_beds':2} for i in range(3)],'allowed_upgrades':{}}

def req(key='A',start=0,end=2,infant=True):
 return BookingRequest(key,DAY+timedelta(days=start),DAY+timedelta(days=end),'S',('king',),2,(1,) if infant else ())

def init_worker(path,barrier,q,timeout):
 try:
  s=SQLiteHotel(path,timeout);barrier.wait();s.initialize(config());q.put(('ok',None))
 except Exception as e:q.put((type(e).__name__,str(e)))

def reserve_worker(path,barrier,q,index,identical=False):
 try:
  s=SQLiteHotel(path);barrier.wait();result=s.reserve(req('same' if identical else f'P{index}'));q.put(('ok',result['status']))
 except Exception as e:q.put((type(e).__name__,str(e)))

def cancel_worker(path,barrier,q):
 try:
  s=SQLiteHotel(path);barrier.wait();q.put(('ok',s.cancel('A')['status']))
 except Exception as e:q.put((type(e).__name__,str(e)))

def process_batch(target,path,n,*extra):
 ctx=mp.get_context('spawn');barrier=ctx.Barrier(n);q=ctx.Queue()
 args=lambda i:(path,barrier,q,i,*extra) if target==reserve_worker else (path,barrier,q,*extra)
 ps=[ctx.Process(target=target,args=args(i)) for i in range(n)]
 for p in ps:p.start()
 results=[q.get(timeout=30) for p in ps]
 for p in ps:
  p.join(30)
  assert p.exitcode==0,p.exitcode
 return results

def rawstate(s):
 with s.read_snapshot() as c:
  return {t:[tuple(row) for row in c.execute('SELECT * FROM '+t)] for t in ('bookings','booking_nights','active_room_nights','auxiliary_inventory','reservation_events')}

class FailAt(SQLiteHotel):
 stage=''
 def _checkpoint(self,stage,connection):
  if stage==self.stage:raise RuntimeError('independent fault injection: '+stage)

REPORT={'checks':[],'findings':[]}
def run(name,fn):
 try:
  result=fn();REPORT['checks'].append({'name':name,'passed':True,'detail':result})
 except Exception as e:
  REPORT['checks'].append({'name':name,'passed':False,'error':str(e),'traceback':traceback.format_exc()})

def main():
 REPORT['source_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hotel_sim').glob('sqlite_*.py')}
 with tempfile.TemporaryDirectory(prefix='db_',dir=OUT) as td:
  base=Path(td)
  def store(name,conf=None):
   s=SQLiteHotel(base/(name+'.db'));s.initialize(conf or config());return s
  def init():
   trials=[]
   for i in range(8):
    results=process_batch(init_worker,str(base/f'cold{i}.db'),8,5000)
    trials.append(results)
    assert all(r[0]=='ok' for r in results),results
    assert SQLiteHotel(base/f'cold{i}.db').reconcile()['healthy']
   return {'trials':len(trials),'processes_per_trial':8,'all_succeeded':True}
  run('simultaneous_cold_initialization',init)
  def race():
   s=store('race');r=process_batch(reserve_worker,str(s.path),12,False)
   assert r.count(('ok','BOOKED'))==1,r
   assert r.count(('ok','REJECTED'))==11,r
   assert s.reconcile()['healthy'];return r
  run('multiprocess_last_room',race)
  def same():
   s=store('same');r=process_batch(reserve_worker,str(s.path),12,True)
   assert r.count(('ok','BOOKED'))==12,r
   st=rawstate(s);assert len(st['bookings'])==1 and len(st['reservation_events'])==1
   assert s.reconcile()['healthy'];return {'statuses':r,'events':len(st['reservation_events'])}
  run('multiprocess_idempotency',same)
  def shared():
   s=store('shared',config(12,1));r=process_batch(reserve_worker,str(s.path),12,False)
   assert r.count(('ok','BOOKED'))==1,r
   assert s.reconcile()['healthy'];return r
  run('multiprocess_shared_resource',shared)
  def cancellation():
   s=store('cancel');s.reserve(req());r=process_batch(cancel_worker,str(s.path),12)
   assert r.count(('ok','CANCELLED'))==12,r
   assert s.reconcile()['healthy'];st=rawstate(s)
   assert len(st['reservation_events'])==2 and not st['active_room_nights']
   assert all(row[3:]==(0,0) for row in st['auxiliary_inventory'])
   return {'statuses':r,'events':len(st['reservation_events'])}
  run('multiprocess_cancel_once',cancellation)
  def faults():
   stages=['reserve_after_record','reserve_after_night','reserve_before_commit','cancel_after_release','cancel_before_commit']
   for i,stage in enumerate(stages):
    s=store('fault'+str(i));f=FailAt(s.path);f.stage=stage
    if stage.startswith('cancel'):s.reserve(req())
    before=rawstate(s)
    try:
     f.cancel('A') if stage.startswith('cancel') else f.reserve(req())
    except RuntimeError:pass
    else:raise AssertionError('fault not raised')
    assert rawstate(s)==before,stage
    assert s.reconcile()['healthy'],stage
    s.cancel('A') if stage.startswith('cancel') else s.reserve(req())
    assert s.reconcile()['healthy'],stage
   return stages
  run('booking_and_cancel_failure_atomicity',faults)
  def busy():
   s=store('busy');holder=s.connect();holder.execute('BEGIN IMMEDIATE')
   try:
    try:SQLiteHotel(s.path,20).reserve(req())
    except DatabaseBusy:pass
    else:raise AssertionError('missing DatabaseBusy')
   finally:holder.rollback();holder.close()
   assert not rawstate(s)['bookings'];assert s.reserve(req())['status']=='BOOKED'
   return {'no_rejection_persisted':True,'same_key_retry':'BOOKED'}
  run('busy_is_transient_not_rejected',busy)
  def persistence():
   s=store('persist');a=s.reserve(req());b=s.reserve(req('B'));assert b['status']=='REJECTED';s.cancel('A')
   s=SQLiteHotel(s.path);assert s.reserve(req())['status']=='CANCELLED';assert s.reserve(req())['original_decision']==a['original_decision']
   assert s.reserve(req('B'))==b;assert s.reserve(req('C'))['status']=='BOOKED'
   for key in ['A','B','C']:
    try:s.reserve(req(key,end=3))
    except IdempotencyConflict:pass
    else:raise AssertionError(key)
   assert s.reconcile()['healthy'];return {'cancelled_stays_cancelled':True,'rejected_stays_rejected':True}
  run('restart_replay_and_conflicts',persistence)
  def parity():
   data=json.loads((ROOT/'data/week.json').read_text());counts={}
   for mode in ('strict','upgrade','protected_upgrade'):
    s=SQLiteHotel(base/(mode+'.db'));s.initialize(data,mode);memory=build_allocator(data,mode)
    for request in parse_requests(data):
     expected=json.loads(json.dumps(memory.allocate(request).to_dict()))
     actual=s.reserve(request)['original_decision'];assert expected==actual,(mode,request.request_id)
    assert s.reconcile()['healthy'];counts[mode]=len(parse_requests(data))
   return counts
  run('baseline_sequential_decision_parity',parity)
  def corruption():
   results=[]
   mutations=[
    ('missing_table',"DROP TABLE active_room_nights"),
    ('missing_configuration_column',"ALTER TABLE configuration DROP COLUMN mode"),
    ('invalid_status',"PRAGMA ignore_check_constraints=ON; DROP TRIGGER booking_status_guard; UPDATE bookings SET status='BROKEN' WHERE request_id='A'"),
    ('string_counter',"PRAGMA ignore_check_constraints=ON; UPDATE auxiliary_inventory SET cots_used='garbage' WHERE night='2026-12-07'"),
    ('missing_active',"DELETE FROM active_room_nights WHERE night='2026-12-07'"),
    ('extra_calendar',"INSERT INTO room_calendar VALUES('ghost','2026-12-07','S',0)"),
   ]
   for i,(name,sql) in enumerate(mutations):
    s=store('corrupt'+str(i));s.reserve(req())
    with sqlite3.connect(s.path) as c:c.executescript(sql)
    try:
     result=s.reconcile();results.append({'mutation':name,'returned_unhealthy':not result['healthy'],'report':result})
    except Exception as e:
     results.append({'mutation':name,'returned_unhealthy':False,'raised':type(e).__name__,'message':str(e)})
   REPORT['findings'].extend(r for r in results if 'raised' in r)
   assert all(r['returned_unhealthy'] for r in results),results
   return results
  run('audit_corruption_robustness',corruption)
 REPORT['source_hashes_after']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hotel_sim').glob('sqlite_*.py')}
 REPORT['implementation_changed_during_review']=REPORT['source_hashes']!=REPORT['source_hashes_after']
 (OUT/'independent_results.json').write_text(json.dumps(REPORT,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(REPORT,ensure_ascii=False,indent=2))
 if any(not row['passed'] for row in REPORT['checks']) or REPORT['findings'] or REPORT['implementation_changed_during_review']:
  raise SystemExit(1)
if __name__=='__main__':main()
