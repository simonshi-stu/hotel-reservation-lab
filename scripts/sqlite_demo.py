"""Real CLI subprocess persistence/replay/cancellation demo using only a temporary DB."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from hotel_sim.scenario import build_allocator,load_scenario,parse_requests


def main():
    data,_=load_scenario(ROOT/'data/week.json')
    chosen=next(r for r in parse_requests(data) if r.child_ages and build_allocator(data,'upgrade').allocate(r).accepted)
    body=next(row for row in data['requests'] if row['request_id']==chosen.request_id)
    report={'synthetic_only':True,'database':'temporary SQLite file; removed after demo','request_id':chosen.request_id}
    with TemporaryDirectory(prefix='hotel-sqlite-demo-') as directory:
        folder=Path(directory);path=folder/'hotel.db';request=folder/'request.json'
        request.write_text(json.dumps(body))
        def run(*arguments):
            done=subprocess.run([sys.executable,'-m','hotel_sim.sqlite_cli','--db',str(path),*arguments],cwd=ROOT,
                                capture_output=True,text=True,timeout=15)
            if done.returncode:raise AssertionError(done.stdout+done.stderr)
            return json.loads(done.stdout)
        report['initialize']=run('init','--scenario','data/week.json','--policy','upgrade')
        original=run('reserve','--request',str(request));assert original['status']=='BOOKED'
        assert run('reserve','--request',str(request))==original
        assert run('get',chosen.request_id)==original
        report['booked']=original;report['before_cancel']=run('reconcile');assert report['before_cancel']['healthy']
        cancelled=run('cancel',chosen.request_id);assert cancelled['status']=='CANCELLED'
        assert run('cancel',chosen.request_id)==cancelled
        assert run('reserve','--request',str(request))['status']=='CANCELLED'
        report['cancelled']=cancelled;report['after_cancel']=run('reconcile')
        assert report['after_cancel']['healthy'] and report['after_cancel']['active_room_nights']==0
        assert report['after_cancel']['events']==2
    print(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True))

if __name__=='__main__':main()
