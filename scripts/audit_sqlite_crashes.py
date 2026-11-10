import sys,os,json,sqlite3,tempfile,multiprocessing as mp,time,traceback,threading,hashlib
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).parent))
from audit_sqlite import ROOT,OUT,config,req,SQLiteHotel,rawstate

class ExitAt(SQLiteHotel):
 stage=''
 def _checkpoint(self,stage,conn):
  if stage==self.stage:os._exit(73)

def crash_worker(path,stage):
 s=ExitAt(path);s.stage=stage
 s.cancel('A') if stage.startswith('cancel') else s.reserve(req())

def init_trace(path,barrier,q):
 try:
  barrier.wait();meta=SQLiteHotel(path).initialize(config());q.put({'ok':True,'initialize':meta})
 except Exception as e:q.put({'ok':False,'type':type(e).__name__,'error':str(e),'traceback':traceback.format_exc()})

def main():
 ctx=mp.get_context('spawn');results={'source_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hotel_sim').glob('sqlite_*.py')}}
 with tempfile.TemporaryDirectory(prefix='extended_',dir=OUT) as td:
  root=Path(td)
  stages=['reserve_after_night','reserve_before_commit','reserve_after_commit','cancel_after_release','cancel_before_commit','cancel_after_commit']
  for i,stage in enumerate(stages):
   s=SQLiteHotel(root/f'crash{i}.db');s.initialize(config())
   if stage.startswith('cancel'):s.reserve(req())
   before=rawstate(s);p=ctx.Process(target=crash_worker,args=(s.path,stage));p.start();p.join(20);assert p.exitcode==73
   after=rawstate(s);report=s.reconcile();assert report['healthy'],report
   committed=stage.endswith('after_commit')
   assert (before!=after)==committed,(stage,before,after)
   retry=s.cancel('A') if stage.startswith('cancel') else s.reserve(req())
   assert s.reconcile()['healthy'];results[stage]={'child_exit':p.exitcode,'committed':committed,'replay_status':retry['status']}
  cold_summaries=[]
  for trial in range(10):
   path=root/f'cold{trial}.db';barrier=ctx.Barrier(12);q=ctx.Queue();ps=[ctx.Process(target=init_trace,args=(path,barrier,q)) for _ in range(12)]
   for p in ps:p.start()
   r=[q.get(timeout=30) for p in ps]
   for p in ps:p.join(30)
   failed=[x for x in r if not x['ok']]
   cold_summaries.append({'trial':trial,'success_count':len(r)-len(failed),
    'wal_enabled_count':sum(bool(x.get('initialize',{}).get('wal_enabled')) for x in r),
    'committed_warning_count':sum(bool(x.get('initialize',{}).get('warnings')) for x in r)})
   if failed:
    results['cold_initialization_error']={'trial':trial,'failures':failed,'audit':SQLiteHotel(path).reconcile()};break
  else:results['cold_initialization_error']={'reproduced':False,'trials':10}
  results['cold_initialization_summaries']=cold_summaries
  class BlockWAL(SQLiteHotel):
   holder=None
   def _checkpoint(self,stage,conn):
    if stage=='initialize_after_commit':
     self.holder=sqlite3.connect(self.path,isolation_level=None)
     self.holder.execute('BEGIN');self.holder.execute('SELECT * FROM configuration').fetchall()
  s=BlockWAL(root/'walwarning.db',timeout_ms=50)
  try:
   meta=s.initialize(config())
   assert meta['initialized'] is True and meta['wal_enabled'] is False and meta['journal_mode']=='delete',meta
   assert any('initialization_committed' in x for x in meta['warnings']),meta
   assert s.reconcile()['healthy']
  finally:
   if s.holder:s.holder.rollback();s.holder.close()
  after=SQLiteHotel(s.path).initialize(config());assert after['wal_enabled'] is True,after
  results['committed_wal_warning']={'blocked_result':meta,'retry_result':after}
 results['source_hashes_after']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hotel_sim').glob('sqlite_*.py')}
 (OUT/'extended_results.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
 assert results['source_hashes']==results['source_hashes_after'],'source changed during run'
 assert results['cold_initialization_error'].get('reproduced') is False,results['cold_initialization_error']
if __name__=='__main__':main()
