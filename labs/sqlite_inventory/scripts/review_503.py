"""Independently classify the historical HTTP sample and probe rejected-request effects."""
import json
import socket
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import importlib.util
from hotel.service import Store
from scripts.support import scenario_store, request

ROOT=Path(__file__).resolve().parents[1]
_spec=importlib.util.spec_from_file_location("hotel._historical_http",ROOT/"reports"/"baseline-before-bounded-http-20261104"/"hotel"/"api.py")
_baseline=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_baseline)
Server=_baseline.Server

SOURCE=ROOT/"reports"/"load-20261104T035926232657Z.json"

def call(base,path,payload=None):
    data=None if payload is None else json.dumps(payload).encode()
    req=Request(base+path,data=data,headers={"Content-Type":"application/json"})
    try:
        with urlopen(req,timeout=5) as response:
            return response.status,dict(response.headers),json.load(response)
    except HTTPError as error:
        with error:
            return error.code,dict(error.headers),json.load(error)

def state(store):
    with store.transaction() as connection:
        return {"reservations":connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
                "events":connection.execute("SELECT COUNT(*) FROM inventory_events").fetchone()[0],
                "available_sum":connection.execute("SELECT SUM(available) FROM nightly_inventory").fetchone()[0],
                "statuses":{r[0]:r[1] for r in connection.execute("SELECT status,COUNT(*) FROM reservations GROUP BY status")}}

class ObservedServer(Server):
    def __init__(self,*args,**kwargs):
        self.worker_count=0
        self.worker_lock=threading.Lock()
        self.two_connections=threading.Event()
        super().__init__(*args,**kwargs)
    def process_request_thread(self,request_socket,client_address):
        with self.worker_lock:
            self.worker_count+=1
            if self.worker_count>=2:
                self.two_connections.set()
        try:
            super().process_request_thread(request_socket,client_address)
        finally:
            with self.worker_lock:
                self.worker_count-=1

def main():
    source=json.loads(SOURCE.read_text(encoding="utf-8"))
    statuses,errors=Counter(),Counter()
    for scenario in source["scenarios"]:
        statuses.update(scenario["http_status_counts"])
        errors.update(scenario["error_counts"])
    assert sum(statuses.values())==source["requests_total"]==2450
    assert statuses["503"]==errors["server_busy"]==1216
    assert errors["database_busy"]==0
    new_phases=[s for s in source["scenarios"] if s["name"] in
                ("same_type_three_nights","after_cancellation_refill") or s["name"].startswith("mixed_batch_")]
    new=Counter()
    for scenario in new_phases:
        new.update(scenario["http_status_counts"])
    assert sum(new.values())==1440 and new==Counter({"201":81,"409":297,"503":1062})
    historical_store=Store(source["environment"]["db"])
    historical_state=state(historical_store)
    historical_audit=historical_store.audit()
    assert historical_audit["ok"]
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder=ROOT/".runtime"/("review-503-"+stamp)
    folder.mkdir(parents=True)
    store=scenario_store(folder/"probe.db")
    existing=store.reserve(**request(key="cancel-probe"))
    server=ObservedServer(("127.0.0.1",0),store,1)
    server_thread=threading.Thread(target=server.serve_forever,daemon=True)
    server_thread.start()
    base="http://127.0.0.1:"+str(server.server_port)
    partial_connections=[]
    try:
        server.slots.acquire()
        before=state(store)
        denied_book=call(base,"/reservations",request(key="denied-probe",customer=2))
        denied_cancel=call(base,"/reservations/"+existing["id"]+"/cancel",{"customer_id":"C00001"})
        denied_health=call(base,"/health")
        after=state(store)
        assert before==after
        for response in (denied_book,denied_cancel,denied_health):
            assert response[0]==503 and response[2]["error"]=="server_busy"
            assert response[1].get("Retry-After")=="1"
        server.slots.release()
        accepted_book=call(base,"/reservations",request(key="denied-probe",customer=2))
        accepted_cancel=call(base,"/reservations/"+existing["id"]+"/cancel",{"customer_id":"C00001"})
        assert accepted_book[0]==201 and accepted_cancel[0]==200
        # Two incomplete request headers consume two connection workers even with one application slot.
        for _ in range(2):
            connection=socket.create_connection(server.server_address,timeout=5)
            connection.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n")
            partial_connections.append(connection)
        assert server.two_connections.wait(5), "Expected two connection workers"
        with server.worker_lock:
            active_workers=server.worker_count
        still_available=server.slots.acquire(blocking=False)
        if still_available:
            server.slots.release()
        assert active_workers>=2 and still_available
        probe_audit=store.audit()
        assert probe_audit["ok"]
    finally:
        for connection in partial_connections:
            connection.close()
        server.shutdown()
        server.server_close()
        server_thread.join()
    report={"synthetic":True,"review_utc":stamp,"source_report":SOURCE.name,
            "source_report_modified":False,"server_implementation":"historical_snapshot_before_bounded_http","total_requests":2450,
            "http_status_counts":dict(statuses),"error_counts":dict(errors),
            "overload":{"all_server_busy":True,"server_busy":1216,"database_busy":0,
                        "fraction_all_requests":1216/2450},
            "new_booking_attempts":{"requests":1440,"created":81,"sold_out":297,
                                    "rejected_before_business":1062,"overload_fraction":1062/1440},
            "historical_final_state":historical_state,"historical_audit":historical_audit,
            "independent_probe":{"before":before,"after_rejected_requests":after,
                "no_database_side_effects":before==after,"retry_after_seconds":1,
                "same_rejected_booking_key_later_succeeded":accepted_book[0]==201,
                "rejected_cancel_later_succeeded":accepted_cancel[0]==200,"audit":probe_audit},
            "connection_limit_probe":{"application_slots":1,
                "simultaneous_partial_header_workers":active_workers,
                "application_slot_still_available":still_available,
                "finding":"Application admission is not a connection/thread limit; partial headers precede dispatch timeout."},
            "limits":["Historical report has aggregate responses, not per-request cancellation IDs.",
                      "41 successful cancellation responses do not establish how many distinct orders HTTP cancelled.",
                      "Original load script directly cancels all 20 targets before refill; no pure-HTTP lifecycle conclusion.",
                      "Allowed-status assertions permit 503; passed=true is a correctness assertion result, not an availability SLO.",
                      "No repeated steady-state benchmark, target arrival rate, retry-policy test, or capacity claim."]}
    destination=ROOT/"reports"/("503-review-"+stamp+".json")
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print(str(destination))

if __name__=="__main__":
    main()

