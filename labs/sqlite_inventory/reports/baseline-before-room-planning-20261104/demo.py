"""Isolated synthetic hotel demonstration, loopback only."""
import argparse
import inspect
import json
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from .api import Handler, Server
from .service import Store, Problem, nights

ROOT=Path(__file__).resolve().parents[1]

def validate(payload):
    expected={"rooms","requests","concurrency","quantity","cancel_orders","refill_requests","check_in","check_out"}
    if not isinstance(payload,dict) or set(payload)!=expected:
        raise Problem("invalid_input","Supply exactly the eight demo controls")
    limits={"rooms":(1,30),"requests":(1,80),"concurrency":(1,12),"quantity":(1,3),
            "cancel_orders":(0,10),"refill_requests":(0,30)}
    for name,(low,high) in limits.items():
        if type(payload[name]) is not int or not low<=payload[name]<=high:
            raise Problem("invalid_input",name+" outside demo limit")
    days=nights(payload["check_in"],payload["check_out"])
    if len(days)>7 or days[0]<"2027-01-01" or payload["check_out"]>"2028-01-01":
        raise Problem("invalid_dates","Use 1-7 synthetic nights within year 2027")
    return dict(payload)

class TracedStore(Store):
    def __init__(self,path,emit,context):
        super().__init__(path)
        self.emit,self.context=emit,context
    @contextmanager
    def transaction(self,write=False):
        try:
            with super().transaction(write) as connection:
                if write:
                    self.emit("write_lock_acquired","BEGIN IMMEDIATE 已返回，当前事务获得 SQLite 写锁",operation=getattr(self.context,"operation",""))
                yield connection
            if write:
                self.emit("transaction_committed","事务已提交（幂等重放也可能是无库存变化的事务）",operation=getattr(self.context,"operation",""))
        except Exception:
            if write:
                self.emit("transaction_failed","事务未提交；已回滚，或尚未取得写锁",operation=getattr(self.context,"operation",""))
            raise

class Manager:
    def __init__(self,root=None,max_runs=20):
        self.root=Path(root) if root else ROOT/".runtime"/"interactive-hotel"
        self.max_runs=max_runs
        self.lock=threading.Lock()
        self.runs={}
        self.active=False
        self.jobs=[]
    def start(self,payload):
        payload=validate(payload)
        with self.lock:
            if self.active:
                raise Problem("demo_busy","One demonstration is already running",503)
            if len(self.runs)>=self.max_runs:
                raise Problem("demo_limit","Maximum 20 isolated runs per server session",429)
            run_id=uuid.uuid4().hex
            state={"id":run_id,"status":"running","synthetic":True,"execution":"actual SQLite transactions in a bounded in-process thread pool",
                   "config":payload,"events":[],"phases":[],"peak_overlapping_attempts":0,"attempt_threads":[],
                   "error":None,"audit":None}
            self.runs[run_id]=state
            self.active=True
            thread=threading.Thread(target=self.execute,args=(run_id,),name="hotel-demo-run",daemon=True)
            self.jobs.append(thread)
            thread.start()
        return run_id
    def get(self,run_id):
        with self.lock:
            if run_id not in self.runs:
                raise Problem("not_found","Unknown demo run",404)
            return json.loads(json.dumps(self.runs[run_id]))
    def execute(self,run_id):
        began=time.perf_counter()
        context=threading.local()
        with self.lock:
            state=self.runs[run_id]
            config=state["config"]
        def emit(kind,message,**extra):
            event={"kind":kind,"message":message,"elapsed_ms":round((time.perf_counter()-began)*1000,3),
                   "thread":threading.get_ident(),**extra}
            with self.lock:
                event["seq"]=len(state["events"])+1
                if len(state["events"])<1000:
                    state["events"].append(event)
        try:
            folder=self.root/run_id
            folder.mkdir(parents=True,exist_ok=False)
            path=folder/"hotel.db"
            seed=Store(path)
            seed.initialize()
            with seed.transaction(write=True) as connection:
                connection.execute("INSERT INTO hotels VALUES ('DEMO','SIMULATED Demo Hotel','SIMULATED')")
                connection.execute("INSERT INTO room_types VALUES ('DEMO-ROOM','DEMO','SIMULATED Standard',35000,2)")
                for day in nights(config["check_in"],config["check_out"]):
                    connection.execute("INSERT INTO nightly_inventory VALUES ('DEMO-ROOM',?,?,?,35000)",(day,config["rooms"],config["rooms"]))
                for i in range(1,81):
                    connection.execute("INSERT INTO customers VALUES (?,?,?,'leisure',?)",
                                       ("C"+str(i),"SIMULATED Guest "+str(i),"SIMULATED","sim-"+str(i)+"@example.invalid"))
            store=TracedStore(path,emit,context)
            emit("seeded","独立合成库已创建；不读取既有演示库或电商数据")
            def snapshot(phase):
                available=store.availability("DEMO-ROOM",config["check_in"],config["check_out"])
                audit=store.audit()
                with self.lock:
                    state["phases"].append({"name":phase,"availability":available,"audit":audit})
                    state["audit"]=audit
                emit("inventory_snapshot",phase+"：稳定阶段房态，独立对账 "+str(audit["ok"]),available=available["available"])
                if not audit["ok"]:
                    raise AssertionError("Inventory audit failed")
            snapshot("initial")
            active=0
            counter_lock=threading.Lock()
            def parallel(tasks,action,label):
                nonlocal active
                if not tasks:
                    return []
                count=min(config["concurrency"],len(tasks))
                barrier=threading.Barrier(count)
                def worker(pair):
                    nonlocal active
                    index,item=pair
                    operation=label+"-"+str(index+1)
                    context.operation=operation
                    with counter_lock:
                        active+=1
                        with self.lock:
                            state["peak_overlapping_attempts"]=max(state["peak_overlapping_attempts"],active)
                            if threading.get_ident() not in state["attempt_threads"]:
                                state["attempt_threads"].append(threading.get_ident())
                    emit("attempt_started","线程开始业务调用，尚不能据此推断已获得写锁",operation=operation)
                    try:
                        if index<count:
                            barrier.wait(timeout=5)
                        result=action(item)
                        emit("operation_result","业务调用已返回",operation=operation,reservation_id=result["id"],
                             status=result["status"],replayed=result.get("replayed",False))
                        return result
                    except Problem as exc:
                        emit("rejected",str(exc),operation=operation,error=exc.code,http_equivalent=exc.status)
                        return {"error":exc.code}
                    finally:
                        with counter_lock:
                            active-=1
                with ThreadPoolExecutor(max_workers=count,thread_name_prefix="hotel-demo-attempt") as pool:
                    return list(pool.map(worker,enumerate(tasks)))
            def reserve(item):
                index,key,quantity=item
                return store.reserve("C"+str(index%80+1),"DEMO-ROOM",config["check_in"],config["check_out"],quantity,key)
            initial_tasks=[(i,"initial-"+str(i),config["quantity"]) for i in range(config["requests"])]
            initial=parallel(initial_tasks,reserve,"book")
            winners=[r for r in initial if "id" in r]
            snapshot("after_booking")
            if winners:
                winner=winners[0]
                index=int(winner["customer_id"][1:])-1
                parallel([(index,winner["idempotency_key"],config["quantity"])]*4,reserve,"same_key_replay")
            targets=winners[:config["cancel_orders"]]
            cancels=parallel([r for r in targets for _ in range(2)],lambda r:store.cancel(r["id"],r["customer_id"]),"cancel")
            verified={r["id"] for r in cancels if r.get("status")=="CANCELLED"}
            if verified!={r["id"] for r in targets}:
                raise AssertionError("Not every requested cancellation completed")
            snapshot("after_cancellation")
            refill=parallel([(i,"refill-"+str(i),config["quantity"]) for i in range(config["refill_requests"])],reserve,"refill")
            snapshot("after_refill")
            summary={"initial_created":len(winners),"initial_rejected":sum("error" in r for r in initial),
                     "distinct_cancelled":len(targets),"refill_created":sum("id" in r for r in refill),
                     "refill_rejected":sum("error" in r for r in refill)}
            emit("complete","真实执行已完成；事件是应用观察顺序，不是全部数据库内部调度",summary=summary)
            with self.lock:
                state["summary"]=summary
                state["status"]="complete"
            (folder/"result.json").write_text(json.dumps(self.get(run_id),ensure_ascii=False,indent=2),encoding="utf-8")
        except Exception as exc:
            emit("failed",type(exc).__name__+": "+str(exc))
            with self.lock:
                state["status"]="failed"
                state["error"]=type(exc).__name__+": "+str(exc)
        finally:
            with self.lock:
                self.active=False

def source(symbol):
    functions={"reserve":Store.reserve,"cancel":Store.cancel,"transaction":Store.transaction,"nights":nights}
    if symbol not in functions:
        raise Problem("not_found","Unknown code symbol",404)
    lines,start=inspect.getsourcelines(functions[symbol])
    return {"path":"hotel/service.py","symbol":symbol,"start_line":start,"code":"".join(lines)}

class DemoHandler(Handler):
    def dispatch(self,method):
        expected="http://127.0.0.1:"+str(self.server.server_port)
        try:
            if self.headers.get("Host")!="127.0.0.1:"+str(self.server.server_port):
                raise Problem("invalid_host","Use the exact loopback URL printed by the server",403)
            if self.headers.get("Origin") not in (None,expected):
                raise Problem("invalid_origin","Cross-origin demo access denied",403)
            route=urlsplit(self.path)
            if method=="GET" and route.path=="/":
                page=Path(__file__).with_name("demo.html").read_text(encoding="utf-8").replace("__TOKEN__",self.server.demo_token)
                body=page.encode("utf-8")
                self.connection.settimeout(self.server.response_timeout)
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(body)))
                self.send_header("Cache-Control","no-store")
                self.send_header("Content-Security-Policy","default-src 'none'; script-src 'nonce-"+self.server.demo_token+"'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(body)
                return
            if not secrets.compare_digest(self.headers.get("X-Demo-Token",""),self.server.demo_token):
                raise Problem("invalid_token","Open the local demo page first",403)
            if method=="POST" and route.path=="/api/runs":
                if int(self.headers.get("Content-Length","0"))>4096:
                    raise Problem("too_large","Demo request limit is 4 KiB",413)
                run_id=self.server.manager.start(self.body())
                self.reply(202,{"id":run_id,"synthetic":True})
                return
            if method=="GET" and re.fullmatch(r"/api/runs/[a-f0-9]{32}",route.path):
                self.reply(200,self.server.manager.get(route.path.rsplit("/",1)[1]))
                return
            if method=="GET" and route.path.startswith("/api/source/"):
                self.reply(200,source(route.path.rsplit("/",1)[1]))
                return
            raise Problem("not_found","Unknown demo endpoint",404)
        except Problem as exc:
            self.reply(exc.status,{"error":exc.code,"message":str(exc)})
        except (ValueError,TypeError):
            self.reply(400,{"error":"invalid_request"})
        except TimeoutError:
            self.reply(408,{"error":"request_timeout"})

class DemoServer(Server):
    def __init__(self,port=8092,manager=None):
        self.manager=manager or Manager()
        self.demo_token=secrets.token_hex(24)
        super().__init__(("127.0.0.1",port),Store(ROOT/".runtime"/"unused-demo-sentinel.db"),
                         max_active=4,queue_size=8,queue_timeout=.5,read_timeout=2)
        self.RequestHandlerClass=DemoHandler

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8092)
    args=parser.parse_args()
    server=DemoServer(args.port)
    print("Isolated synthetic hotel demo: http://127.0.0.1:"+str(server.server_port),flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for job in server.manager.jobs:
            job.join(timeout=20)

if __name__=="__main__":
    main()

