"""Real loopback HTTP load; local SQLite sample, never a production capacity claim."""
import argparse
import concurrent.futures
import json
import math
import platform
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from hotel.api import Server
from scripts.support import scenario_store, request

ROOT = Path(__file__).resolve().parents[1]

def percentile(values, fraction):
    values=sorted(values)
    return round(values[max(0, math.ceil(len(values)*fraction)-1)],3) if values else None

def run(clients=64, hot_requests=600, output=None):
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir=ROOT/".runtime"/("load-"+stamp)
    run_dir.mkdir(parents=True)
    store=scenario_store(run_dir/"hotel.db")
    server=Server(("127.0.0.1",0),store,32)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base="http://127.0.0.1:"+str(server.server_port)
    scenarios=[]
    responses={}
    failure=None

    def call(task):
        path,payload=task
        start=time.perf_counter()
        data=None if payload is None else json.dumps(payload).encode()
        try:
            req=Request(base+path,data=data,headers={"Content-Type":"application/json"})
            try:
                with urlopen(req,timeout=15) as response:
                    status,body=response.status,json.load(response)
            except HTTPError as error:
                with error:
                    status,body=error.code,json.load(error)
            return {"status":status,"body":body,"ms":(time.perf_counter()-start)*1000}
        except (URLError,TimeoutError,ConnectionError,OSError,ValueError) as exc:
            return {"status":"transport_error","body":{"error":type(exc).__name__},
                    "ms":(time.perf_counter()-start)*1000}

    def phase(name,tasks,allowed):
        started=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
            results=list(pool.map(call,tasks))
        elapsed=time.perf_counter()-started
        codes=Counter(str(r["status"]) for r in results)
        errors=Counter(r["body"].get("error","none") for r in results if r["status"] not in (200,201))
        latencies=[r["ms"] for r in results]
        unexpected=[r for r in results if r["status"] not in allowed]
        audit=store.audit()
        sample={"name":name,"requests":len(tasks),"wall_seconds":round(elapsed,4),
                "completed_rps":round(len(tasks)/elapsed,2),"http_status_counts":dict(codes),
                "error_counts":dict(errors),"latency_ms":{"p50":percentile(latencies,.50),
                "p95":percentile(latencies,.95),"p99":percentile(latencies,.99),"max":round(max(latencies),3)},
                "audit":audit,"unexpected_responses":unexpected[:3]}
        scenarios.append(sample)
        responses[name]=results
        print(name+": "+json.dumps(codes),flush=True)
        if not audit["ok"] or unexpected:
            raise AssertionError(name+" has unexpected responses or failed reconciliation")
        return results

    try:
        hot=[request(customer=(i%2000)+1,key="hot-"+str(i)) for i in range(hot_requests)]
        results=phase("same_type_three_nights", [("/reservations",p) for p in hot], {201,409,503})
        winners=[(p,r["body"]) for p,r in zip(hot,results) if r["status"]==201]
        assert len(winners)==40, "Expected exactly 40 confirmed bookings; got "+str(len(winners))
        payload=winners[0][0]
        replays=phase("same_key_retries",[("/reservations",payload)]*200,{200,503})
        successful=[r for r in replays if r["status"]==200]
        assert successful and all(r["body"]["id"]==winners[0][1]["id"] for r in successful)
        changed={**payload,"quantity":2}
        phase("same_key_changed_payload",[("/reservations",changed)]*50,{409,503})
        phase("quantity_over_limit",[("/reservations",request(key="invalid-"+str(i),quantity=11)) for i in range(60)],{400,503})
        for batch in range(4):
            tasks=[]
            for i in range(160):
                tasks.append(("/reservations",request(customer=(batch*160+i)%2000+1,
                    key="batch-"+str(batch)+"-"+str(i),room="H1-R"+str(2+i%2),
                    check_in="2027-01-0"+str(2+batch%2),check_out="2027-01-06",
                    quantity=1+i%3)))
            phase("mixed_batch_"+str(batch+1),tasks,{201,409,503})
        targets=winners[:20]
        cancellation_tasks=[("/reservations/"+r["id"]+"/cancel",{"customer_id":p["customer_id"]})
                            for p,r in targets for _ in range(5)]
        phase("concurrent_duplicate_cancellation",cancellation_tasks,{200,503})
        # Explicitly finish any cancellation rejected by the admission gate before refill assertions.
        for payload,booking in targets:
            store.cancel(booking["id"],payload["customer_id"])
        phase("after_cancellation_refill",[("/reservations",request(customer=i+1,key="refill-"+str(i)))
                                         for i in range(200)],{201,409,503})
        assert sum(r["status"]==201 for r in responses["after_cancellation_refill"])==20
        phase("availability_browse",[("/availability?room_type_id=H1-R1&check_in=2027-01-01&check_out=2027-01-04",None)]*600,{200,503})
    except Exception as exc:
        failure=type(exc).__name__+": "+str(exc)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    audit=store.audit()
    report={"synthetic":True,"kind":"local_loopback_HTTP_load_sample","utc":stamp,
            "environment":{"python":sys.version,"platform":platform.platform(),"sqlite":sqlite3.sqlite_version,
                           "clients":clients,"application_active_slots":32,"server_processes":1,
                           "journal_mode":"WAL","synchronous":"FULL","writer_timeout_seconds":5,
                           "db":str(run_dir/"hotel.db"),"load_generator_same_machine":True},
            "passed":failure is None and audit["ok"],"failure":failure,"scenarios":scenarios,
            "requests_total":sum(s["requests"] for s in scenarios),"final_audit":audit,
            "boundaries":["Synthetic data and local SQLite only; PostgreSQL not exercised.",
                          "Thread pool clients are overlapping in-flight requests, not requests arriving in one instant.",
                          "No production capacity/SLA, distributed failover, disk-full or network partition claim.",
                          "Admission gate limits application operations; http.server still creates a thread per connection.",
                          "Browse results are advisory; only booking transaction guarantees stock.",
                          "Cancellation cleanup is direct service invocation after HTTP phase, excluded from throughput."]}
    destination=Path(output) if output else ROOT/"reports"/("load-"+stamp+".json")
    if destination.exists():
        raise FileExistsError("Refusing to overwrite report")
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Report: "+str(destination),flush=True)
    print("Passed: "+str(report["passed"]),flush=True)
    return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients",type=int,default=64)
    parser.add_argument("--hot-requests",type=int,default=600)
    parser.add_argument("--output")
    args=parser.parse_args()
    if not 1<=args.clients<=128 or not 100<=args.hot_requests<=10000:
        parser.error("clients must be 1-128 and hot-requests 100-10000")
    report=run(args.clients,args.hot_requests,args.output)
    raise SystemExit(0 if report["passed"] else 1)

if __name__=="__main__":
    main()

