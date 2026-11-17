"""Staged local HTTP experiment, with ample-stock trials and a pure-HTTP lifecycle."""
import argparse
import concurrent.futures
import hashlib
import json
import math
import platform
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from hotel.api import Server
from scripts.http_client import Client
from scripts.support import scenario_store,request

ROOT=Path(__file__).resolve().parents[1]

def latency(values):
    ordered=sorted(values)
    def p(f):
        return round(ordered[max(0,math.ceil(len(ordered)*f)-1)],3) if ordered else None
    return {"samples":len(ordered),"p50":p(.5),"p95":p(.95),"p99":p(.99),
            "max":round(max(ordered),3) if ordered else None}

def totals(results):
    attempts=[a for r in results for a in r["attempts"]]
    counts=Counter(str(a["status"]) for a in attempts)
    errors=Counter(a["body"].get("error","none") for a in attempts if a["status"] not in (200,201))
    return {"logical_requests":len(results),"http_attempts":len(attempts),
            "attempt_status_counts":dict(counts),"attempt_error_counts":dict(errors),
            "terminal_status_counts":dict(Counter(str(r["status"]) for r in results)),
            "attempt_latency_ms":latency([a["latency_ms"] for a in attempts]),
            "logical_latency_ms":latency([r["logical_latency_ms"] for r in results]),
            "retry_attempts":len(attempts)-len(results)}

def settle(server):
    end=time.monotonic()+2
    while time.monotonic()<end:
        stats=server.stats()
        if not stats["active"] and not stats["pending"]:
            return
        time.sleep(.005)
    raise AssertionError("Server did not become idle")

def dump_lines(path,rows):
    with path.open("x",encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row,ensure_ascii=False)+"\n")

def stage(folder,run_number,clients,requests,workers,queue_size,queue_timeout):
    name="trial-"+str(run_number)+"-clients-"+str(clients)
    store=scenario_store(folder/(name+".db"),capacity=requests+10)
    server=Server(("127.0.0.1",0),store,workers,queue_size,queue_timeout,3,.25)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    client=Client("http://127.0.0.1:"+str(server.server_port))
    payloads=[request(customer=i+1,key="ample-"+str(i)) for i in range(requests)]
    errors=[]
    try:
        began=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
            results=list(pool.map(lambda payload:client.call("/reservations",payload),payloads))
        elapsed=time.perf_counter()-began
        settle(server)
        recovery=client.call("/health",max_attempts=1)
        settle(server)
        audit=store.audit()
        with store.transaction() as connection:
            committed={r["idempotency_key"]:r["id"] for r in connection.execute("SELECT idempotency_key,id FROM reservations")}
        records=[]
        for payload,result in zip(payloads,results):
            found=committed.get(payload["idempotency_key"])
            result={**result,"idempotency_key":payload["idempotency_key"],"committed_order_id":found}
            records.append(result)
            if result["status"]==201 and result["body"].get("id")!=found:
                errors.append("Acknowledged order missing or different")
            elif result["status"]==503:
                if result["body"].get("outcome")!="not_committed" or found is not None:
                    errors.append("503 no-commit promise violated")
                if result["body"].get("error") not in {"server_busy","queue_wait_timeout","database_busy"}:
                    errors.append("Unexpected 503 reason")
            elif result["status"] not in (201,"transport_error"):
                errors.append("Unexpected HTTP result: "+str(result["status"]))
        stats=server.stats()
        limits=(stats["peak_active"]<=workers and stats["peak_pending"]<=queue_size
                and stats["peak_live"]<=workers+queue_size+1)
        acknowledged=sum(r["status"]==201 for r in results)
        if not acknowledged:
            errors.append("No successful booking observed in this trial")
        record_name=name+".ndjson"
        dump_lines(folder/record_name,records)
        summary=totals(results)
        summary.update({"name":name,"clients":clients,"trial":run_number,"stock_capacity":requests+10,
            "workload":"unique valid bookings; sufficient inventory; no retries","wall_seconds":round(elapsed,4),
            "acknowledged_new_orders":acknowledged,"http_business_success_fraction":acknowledged/requests,
            "committed_orders":len(committed),"unacknowledged_commits":sum(
                r["status"]!="201" and r["status"]!=201 and r["committed_order_id"] is not None for r in records),
            "successful_response_latency_ms":latency([r["latency_ms"] for r in results if r["status"]==201]),
            "overload_response_latency_ms":latency([r["latency_ms"] for r in results if r["status"]==503]),
            "server_stats":stats,"resource_bounds_observed":limits,"audit":audit,
            "recovery_health_status":recovery["status"],"recovery_http_attempts":1,
            "request_log":record_name,"assertions_passed":not errors and audit["ok"] and limits and recovery["status"]==200,
            "errors":errors})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    summary["workers_alive_after_close"]=server.stats()["worker_threads_alive"]
    if summary["workers_alive_after_close"]:
        summary["assertions_passed"]=False
        summary["errors"].append("Connection workers remained after close")
    return summary

def lifecycle(folder,workers,queue_size,queue_timeout):
    store=scenario_store(folder/"pure-http-lifecycle.db",capacity=20)
    server=Server(("127.0.0.1",0),store,workers,queue_size,queue_timeout,3,.25)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    client=Client("http://127.0.0.1:"+str(server.server_port))
    phases=[]
    records=[]
    failure=None
    final_audit=None
    verified_cancelled=0
    def phase(name,tasks):
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8,workers)) as pool:
            results=list(pool.map(lambda task:client.call(task[0],task[1],max_attempts=4),tasks))
        settle(server)
        audit=store.audit()
        phases.append({"name":name,**totals(results),"audit":audit})
        records.extend({"phase":name,"path":task[0],"payload":task[1],"result":result}
                       for task,result in zip(tasks,results))
        if not audit["ok"]:
            raise AssertionError("Inventory audit failed")
        return results
    availability_path="/availability?room_type_id=H1-R1&check_in=2027-01-01&check_out=2027-01-04"
    try:
        initial=phase("reserve_to_capacity",[
            ("/reservations",request(customer=i+1,key="lifecycle-initial-"+str(i))) for i in range(80)])
        winners=[r["body"] for r in initial if r["status"] in (200,201)]
        assert len(winners)==len({r["id"] for r in winners})==20
        assert all(r["status"] in (200,201) or (r["status"]==409 and r["body"].get("error")=="sold_out") for r in initial)
        targets=winners[:10]
        cancellations=phase("duplicate_cancels_over_HTTP",[
            ("/reservations/"+booking["id"]+"/cancel",{"customer_id":booking["customer_id"]})
            for booking in targets for _ in range(4)])
        assert all(r["status"]==200 and r["body"].get("status")=="CANCELLED" for r in cancellations)
        looked_up=phase("verify_every_cancel_over_HTTP",[
            ("/reservations/"+booking["id"]+"?customer_id="+booking["customer_id"],None) for booking in targets])
        assert all(r["status"]==200 and r["body"].get("status")=="CANCELLED" for r in looked_up)
        verified_cancelled=len(looked_up)
        released=phase("check_released_inventory_over_HTTP",[(availability_path,None)])
        assert released[0]["status"]==200 and released[0]["body"]["available"]==10
        refill=phase("refill_over_HTTP",[
            ("/reservations",request(customer=i+201,key="lifecycle-refill-"+str(i))) for i in range(50)])
        accepted=[r["body"] for r in refill if r["status"] in (200,201)]
        assert len(accepted)==len({r["id"] for r in accepted})==10
        assert all(r["status"] in (200,201) or (r["status"]==409 and r["body"].get("error")=="sold_out") for r in refill)
        final=phase("check_sold_out_inventory_over_HTTP",[(availability_path,None)])
        assert final[0]["status"]==200 and final[0]["body"]["available"]==0
        final_audit=store.audit()
    except Exception as exc:
        failure=type(exc).__name__+": "+str(exc)
        final_audit=store.audit()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    dump_lines(folder/"pure-http-lifecycle.ndjson",records)
    return {"passed":failure is None and final_audit["ok"],"failure":failure,"phases":phases,
            "distinct_cancelled_orders_confirmed_by_HTTP":verified_cancelled,
            "direct_service_mutation_calls_after_fixture_seed":0,
            "http_attempts":sum(p["http_attempts"] for p in phases),
            "logical_requests":sum(p["logical_requests"] for p in phases),
            "retry_policy":"Up to four attempts; honor Retry-After; preserve payload/key; retry explicit no-commit 503 or uncertain transport.",
            "audit":final_audit,"server_stats_after_close":server.stats(),
            "request_log":"pure-http-lifecycle.ndjson"}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients",default="1,8,32,64,96")
    parser.add_argument("--trials",type=int,default=2)
    parser.add_argument("--requests",type=int,default=160)
    parser.add_argument("--workers",type=int,default=8)
    parser.add_argument("--queue-size",type=int,default=16)
    parser.add_argument("--queue-timeout",type=float,default=.5)
    args=parser.parse_args()
    clients=[int(v) for v in args.clients.split(",")]
    if not clients or any(not 1<=v<=128 for v in clients) or not 1<=args.trials<=3 or not 40<=args.requests<=1000:
        parser.error("clients 1-128, trials 1-3, requests 40-1000")
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder=ROOT/".runtime"/("bounded-http-"+stamp)
    folder.mkdir(parents=True)
    stages=[]
    failure=None
    e2e=None
    try:
        for number in range(1,args.trials+1):
            for concurrency in clients:
                result=stage(folder,number,concurrency,args.requests,args.workers,args.queue_size,args.queue_timeout)
                stages.append(result)
                print(result["name"]+": "+json.dumps(result["attempt_status_counts"])+
                      " audit="+str(result["audit"]["ok"]),flush=True)
        e2e=lifecycle(folder,args.workers,args.queue_size,args.queue_timeout)
        print("Pure HTTP lifecycle: "+str(e2e["passed"]),flush=True)
    except Exception as exc:
        failure=type(exc).__name__+": "+str(exc)
    sources={}
    for name in ("hotel/api.py","hotel/service.py","hotel/schema.sql","scripts/http_client.py",
                 "scripts/load_matrix.py","tests/test_booking.py","tests/test_bounded_http.py"):
        sources[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    report={"synthetic":True,"utc":stamp,"kind":"bounded_local_HTTP_staged_experiment",
            "environment":{"python":sys.version,"platform":platform.platform(),"sqlite":sqlite3.sqlite_version,
                "fixed_connection_workers":args.workers,"waiting_queue_limit":args.queue_size,
                "queue_timeout_seconds":args.queue_timeout,"read_deadline_seconds":3,
                "response_write_timeout_seconds":.25,"load_generator_same_machine":True},
            "stages":stages,"pure_http_lifecycle":e2e,"run_error":failure,
            "correctness_assertions_passed":failure is None and bool(stages) and all(s["assertions_passed"] for s in stages) and bool(e2e and e2e["passed"]),
            "performance_SLO_defined":False,"production_capacity_claim":False,
            "source_sha256":sources,"request_logs_directory":str(folder),
            "observed_http_attempts":sum(s["http_attempts"]+s["recovery_http_attempts"] for s in stages)+(e2e["http_attempts"] if e2e else 0),
            "boundaries":["Local loopback SQLite only; no PostgreSQL or production validation.",
                "Ample-stock trials do not retry, so overload is visible in success fractions.",
                "Success fraction is acknowledged new bookings divided by offered logical requests.",
                "Each trial uses fresh synthetic stock; no cancellation cleanup via service methods.",
                "Retrying lifecycle reports both logical requests and physical HTTP attempts.",
                "Bounded threads/accepted queue do not protect the OS from every form of network overload.",
                "Queue expiry eligibility is polled; write/OS scheduling may delay delivery of its response.",
                "No arrival-rate-controlled workload or capacity/SLA conclusion; trials are a local sample."]}
    destination=ROOT/"reports"/("bounded-http-"+stamp+".json")
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Report: "+str(destination),flush=True)
    print("Correctness assertions: "+str(report["correctness_assertions_passed"]),flush=True)
    raise SystemExit(0 if report["correctness_assertions_passed"] else 1)

if __name__=="__main__":
    main()

