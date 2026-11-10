"""Loopback-only interactive synthetic demo over the preserved SQLite candidate.

This web layer serializes demonstration actions. It is not a load generator,
production hotel service, or evidence of real-world concurrency performance.
"""
import argparse
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from hotel_demo.recovery import lookup_operation, RecoveryUnavailable
from hotel_sim.scenario import load_scenario, parse_requests
from hotel_sim.sqlite_audit import audit
from hotel_sim.sqlite_store import (SQLiteHotel, BookingNotFound, InvalidBookingState,
    IdempotencyConflict, DatabaseBusy, DataIntegrityError)

ROOT=Path(__file__).resolve().parent.parent
MODES=('strict','upgrade','protected_upgrade')


class DemoError(Exception):
    def __init__(self,status,code,message):
        self.status,self.code,self.message=status,code,message


class InjectedFailure(RuntimeError):
    pass


class TracedHotel(SQLiteHotel):
    def __init__(self,path,inject_failure=False):
        super().__init__(path,timeout_ms=2000)
        self.trace=[];self.inject_failure=inject_failure

    @contextmanager
    def transaction(self):
        try:
            with super().transaction() as connection:
                self.trace.append({'code':'BEGIN','label':'开始写事务 · BEGIN IMMEDIATE'})
                yield connection
            self.trace.append({'code':'COMMIT','label':'数据库提交成功'})
        except BaseException:
            self.trace.append({'code':'ROLLBACK','label':'事务未提交，已执行回滚'})
            raise

    def _checkpoint(self,stage,connection):
        labels={'reserve_after_record':'保存请求与原始决定','reserve_after_night':'写入一晚房间与辅助用品占用',
                'reserve_before_commit':'预订记录与事件齐备，准备提交','cancel_after_release':'释放一晚房间与辅助用品',
                'cancel_before_commit':'取消状态与事件齐备，准备提交'}
        if stage in labels:self.trace.append({'code':stage,'label':labels[stage]})
        if self.inject_failure and stage=='reserve_after_night':
            self.trace.append({'code':'INJECTED_FAILURE','label':'教学故障注入：首晚写入后主动中断'})
            raise InjectedFailure('synthetic first-night failure')


class DemoWorkspace:
    def __init__(self,path,scenario=None):
        self.path=Path(path).resolve();self._lock=threading.RLock()
        self.scenario=load_scenario(scenario or ROOT/'data/week.json')[0]
        marker=self.path/'DEMO-WORKSPACE.json'
        if self.path.exists() and any(self.path.iterdir()) and not marker.is_file():
            raise ValueError('refusing an unmarked non-empty state directory')
        self.path.mkdir(parents=True,exist_ok=True)
        if marker.exists():
            if json.loads(marker.read_text()).get('hotel_local_demo')!=1:raise ValueError('unrecognized demo workspace')
        else:marker.write_text(json.dumps({'hotel_local_demo':1,'synthetic_only':True})+'\n')
        current=self.path/'CURRENT'
        if current.exists():
            self.generation=current.read_text().strip()
            if not re.fullmatch('[0-9a-f]{32}',self.generation):raise ValueError('invalid demo round identifier')
            if not (self.path/self.generation).is_dir() or any(not (self.path/self.generation/(mode+'.db')).is_file() for mode in MODES):
                raise ValueError('current demo round is incomplete; refusing a silent reset')
            for mode in MODES:self._store(mode).initialize(self.scenario,mode)
        else:self._new_round()

    def _new_round(self):
        generation=uuid4().hex
        folder=self.path/generation;folder.mkdir()
        for mode in MODES:SQLiteHotel(folder/(mode+'.db')).initialize(self.scenario,mode)
        temporary=self.path/('CURRENT-'+uuid4().hex+'.tmp');temporary.write_text(generation+'\n')
        temporary.replace(self.path/'CURRENT');self.generation=generation

    def _store(self,mode,traced=False,inject_failure=False):
        if mode not in MODES:raise DemoError(400,'INVALID_MODE','请选择有效的演示策略。')
        path=self.path/self.generation/(mode+'.db')
        return TracedHotel(path,inject_failure) if traced else SQLiteHotel(path)

    def _check_generation(self,generation):
        if generation!=self.generation:raise DemoError(409,'STALE_ROUND','演示轮次已改变，请刷新后再操作。此次请求没有写入。')

    def state(self,mode):
        with self._lock:
            store=self._store(mode)
            with store.read_snapshot() as c:
                config,stored_mode=store._configuration(c)
                inventory=store._planner(c,config,stored_mode).snapshot()
                bookings=[store._public(row) for row in c.execute('SELECT * FROM bookings ORDER BY created_at DESC,request_id DESC')]
                report=audit(c,config,stored_mode,100)
            return {'generation':self.generation,'mode':mode,
                'catalog':{key:config[key] for key in ('horizon_start','horizon_end','cot_age_limit','room_types','rooms')},
                'inventory':inventory,'bookings':bookings,'audit':report,
                'capabilities':{'local_demo':True,'synthetic':True,'serialized_interaction':True,'production_service':False}}

    def reserve(self,body):
        with self._lock:
            self._check_generation(body.get('generation'))
            mode=body.get('mode');store=self._store(mode,traced=True,inject_failure=body.get('simulate_failure',False))
            if type(body.get('simulate_failure',False)) is not bool:raise ValueError('simulate_failure must be boolean')
            raw=body.get('request')
            if not isinstance(raw,dict):raise ValueError('request must be an object')
            if not re.fullmatch('[A-Za-z0-9_-]{1,80}',str(raw.get('request_id',''))):raise ValueError('invalid request ID')
            if not isinstance(raw.get('child_ages',[]),list) or len(raw.get('child_ages',[]))>10:raise ValueError('too many child ages')
            request=parse_requests({'requests':[raw]})[0]
            before=store.snapshot()['occupied_room_nights']
            try:store.get_booking(request.request_id);replayed=True
            except BookingNotFound:replayed=False
            try:record=store.reserve(request);status=record['status']
            except InjectedFailure:record=None;status='ROLLED_BACK'
            report=store.reconcile()
            after=store.snapshot()['occupied_room_nights']
            return {'generation':self.generation,'mode':mode,'request_id':request.request_id,'status':status,
                'record':record,'trace':store.trace,'audit':report,'inventory_delta_nights':after-before,'replayed':replayed}

    def cancel(self,body):
        with self._lock:
            self._check_generation(body.get('generation'));mode=body.get('mode');store=self._store(mode,traced=True)
            key=body.get('request_id')
            if not isinstance(key,str):raise ValueError('request_id must be a string')
            before=store.snapshot()['occupied_room_nights'];old=store.get_booking(key)
            record=store.cancel(key);report=store.reconcile();after=store.snapshot()['occupied_room_nights']
            return {'generation':self.generation,'mode':mode,'request_id':key,'status':record['status'],'record':record,
                'trace':store.trace,'audit':report,'inventory_delta_nights':after-before,'replayed':old['status']=='CANCELLED'}

    def lookup(self,body):
        with self._lock:
            return lookup_operation(self,body,MODES)

    def reconcile(self,mode):
        with self._lock:
            return self._store(mode).reconcile()

    def reset(self,body):
        with self._lock:
            self._check_generation(body.get('generation'));mode=body.get('mode')
            if mode not in MODES:raise ValueError('invalid mode')
            self._new_round()
            return self.state(mode)


class DemoServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,workspace,allowed_hosts=()):
        self.workspace=workspace;self.csrf=secrets.token_urlsafe(32)
        self.allowed_hosts={'127.0.0.1','localhost','::1',*allowed_hosts}
        super().__init__(address,DemoHandler)


class DemoHandler(BaseHTTPRequestHandler):
    server_version='HotelLocalDemo/0.1'
    def log_message(self,format,*args):
        pass  # Do not log request bodies, tokens or query strings.

    def _headers(self,status,content_type,length):
        self.send_response(status)
        for key,value in {'Content-Type':content_type,'Content-Length':str(length),'Cache-Control':'no-store',
            'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"}.items():self.send_header(key,value)
        self.end_headers()

    def _json(self,status,data):
        payload=json.dumps(data,ensure_ascii=False).encode();self._headers(status,'application/json; charset=utf-8',len(payload));self.wfile.write(payload)

    def _validate_host(self):
        host=self.headers.get('Host','')
        try:hostname=urlsplit('//'+host).hostname
        except ValueError:hostname=None
        if hostname not in self.server.allowed_hosts:raise DemoError(403,'UNTRUSTED_HOST','仅允许本机或已配置的预览地址访问。')
        return host

    def _error(self,exc):
        if isinstance(exc,DemoError):status,code,message=exc.status,exc.code,exc.message
        elif isinstance(exc,RecoveryUnavailable):status,code,message=409,'RECOVERY_UNAVAILABLE','原轮次数据库缺失、不匹配或未通过完整核对，原操作结果仍未确认。不会创建或修复数据库。'
        elif isinstance(exc,DatabaseBusy):status,code,message=503,'DATABASE_BUSY','数据库暂时繁忙，请使用原请求键重放。'
        elif isinstance(exc,IdempotencyConflict):status,code,message=409,'IDEMPOTENCY_CONFLICT','同一请求键不能用于不同内容。'
        elif isinstance(exc,BookingNotFound):status,code,message=404,'BOOKING_NOT_FOUND','此策略和轮次下没有该订单。'
        elif isinstance(exc,InvalidBookingState):status,code,message=409,'INVALID_BOOKING_STATE','只有已预订的订单可以取消。'
        elif isinstance(exc,(ValueError,TypeError,KeyError)):status,code,message=400,'VALIDATION_ERROR','请检查日期、人数、儿童年龄、床型和输入格式。'
        else:status,code,message=500,'RESULT_UNCONFIRMED','操作结果未确认。请保留原请求键重放核对，不要另建重复订单。'
        self._json(status,{'error':{'code':code,'message':message}})

    def do_GET(self):
        try:
            self._validate_host();url=urlsplit(self.path)
            if url.path=='/api/state':
                mode=parse_qs(url.query).get('mode',['strict'])[0]
                return self._json(200,self.server.workspace.state(mode))
            if url.path=='/api/reconcile':
                mode=parse_qs(url.query).get('mode',['strict'])[0]
                return self._json(200,self.server.workspace.reconcile(mode))
            names={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/styles.css':('styles.css','text/css; charset=utf-8')}
            if url.path=='/favicon.ico':self._headers(204,'image/x-icon',0);return
            if url.path not in names:raise DemoError(404,'NOT_FOUND','未找到页面。')
            name,kind=names[url.path];data=(Path(__file__).with_name('static')/name).read_text(encoding='utf-8')
            if name=='index.html':data=data.replace('__DEMO_CSRF__',self.server.csrf)
            payload=data.encode();self._headers(200,kind,len(payload));self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:self._error(exc)

    def do_POST(self):
        try:
            host=self._validate_host()
            if self.headers.get('Origin') not in ('http://'+host,'https://'+host):raise DemoError(403,'INVALID_ORIGIN','演示写入只接受同源页面请求。')
            if not hmac.compare_digest(self.headers.get('X-Demo-CSRF',''),self.server.csrf):raise DemoError(403,'CSRF_REQUIRED','请刷新本机演示页面后重试。')
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':raise DemoError(415,'JSON_REQUIRED','仅接受 JSON 请求。')
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:raise DemoError(400,'INVALID_LENGTH','请求长度无效。')
            length=int(self.headers['Content-Length'])
            if not 0<length<=32768:raise DemoError(413,'BODY_TOO_LARGE','演示请求过大。')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('body must be object')
            path=urlsplit(self.path).path
            handlers={'/api/reserve':self.server.workspace.reserve,'/api/cancel':self.server.workspace.cancel,'/api/reset':self.server.workspace.reset,'/api/lookup':self.server.workspace.lookup}
            if path not in handlers:raise DemoError(404,'NOT_FOUND','未找到操作。')
            return self._json(200,handlers[path](body))
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:self._error(exc)


def main():
    parser=argparse.ArgumentParser(description='Local synthetic hotel demo, not a production service')
    parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8019')))
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--state-dir',type=Path,default=ROOT/'.hotel-demo-state')
    parser.add_argument('--allow-host',action='append',default=[])
    parser.add_argument('--preview',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--strictPort',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.host!='127.0.0.1' and not args.preview:parser.error('normal local demo binds only 127.0.0.1')
    hosts=[*args.allow_host,*filter(None,os.environ.get('HOTEL_DEMO_ALLOWED_HOSTS','').split(','))]
    server=DemoServer((args.host,args.port),DemoWorkspace(args.state_dir),hosts)
    print(f'Hotel synthetic local demo: http://127.0.0.1:{server.server_port}',flush=True)
    print('Local synthetic SQLite transactions only. No real hotel booking or performance test.',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
