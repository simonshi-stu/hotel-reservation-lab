"""Bounded loopback benchmark; synthetic data, not production/attack certification."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import json
import math
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hotel_demo.server import DemoServer, DemoWorkspace


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def request(server, path, body=None):
    started = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                   "Content-Type": "application/json", "X-Demo-CSRF": server.csrf}
        connection.request("POST" if body else "GET", path,
                           json.dumps(body) if body else None, headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        return {"http": response.status, "status": data.get("status", "READ"),
                "ms": (time.perf_counter() - started) * 1000}
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # A timed-out write is unknown, not a failed reservation. Never auto-retry.
        return {"http": None, "status": "UNCONFIRMED",
                "error": type(exc).__name__, "ms": (time.perf_counter() - started) * 1000}
    finally:
        connection.close()


def phase(server, concurrency, operations):
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda op: request(server, *op), operations))
    seconds = time.perf_counter() - started
    durations = [r["ms"] for r in results]
    return {"requests": len(results), "elapsed_seconds": round(seconds, 4),
            "completed_requests_per_second": round(sum(r["http"] is not None for r in results) / seconds, 2),
            "p50_ms": round(percentile(durations, .5), 2),
            "p95_ms": round(percentile(durations, .95), 2),
            "p99_ms": round(percentile(durations, .99), 2),
            "http_counts": dict(Counter(str(r["http"]) for r in results)),
            "outcome_counts": dict(Counter(r["status"] for r in results)),
            "transport_errors": sum(r["http"] is None for r in results)}


def run_level(concurrency, count):
    with tempfile.TemporaryDirectory(prefix="hotel-http-benchmark-") as tmp:
        workspace = DemoWorkspace(Path(tmp) / "state")
        server = DemoServer(("127.0.0.1", 0), workspace)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            common = {"generation": workspace.generation, "mode": "strict"}
            template = json.loads((ROOT / "data/sqlite_demo_request.json").read_text())
            template.update(child_ages=[], allow_upgrade=False, allow_extra_bed=False)
            shared = {**template, "request_id": "SYN-REPLAY"}
            replay = phase(server, concurrency, [
                ("/api/reserve", {**common, "request": shared}) for _ in range(count)])
            booked = workspace.state("strict")
            cancel = phase(server, concurrency, [
                ("/api/cancel", {**common, "request_id": "SYN-REPLAY"}) for _ in range(count)])
            cancelled = workspace.state("strict")
            distinct = phase(server, concurrency, [
                ("/api/reserve", {**common, "request": {**template, "request_id": f"SYN-{i}"}})
                for i in range(count)])
            reads = phase(server, concurrency, [("/api/state?mode=strict", None)] * count)
            final = workspace.state("strict")
            booked_rows = sum(row["status"] == "BOOKED" for row in final["bookings"])
            expected = min(count, sum(room["room_type"] == "standard"
                                      for room in workspace.scenario["rooms"]))
            checks = {
                "replays_one_record": len(booked["bookings"]) == 1,
                "replays_one_event": booked["audit"]["events"] == 1,
                "replays_three_room_nights": booked["inventory"]["occupied_room_nights"] == 3,
                "cancellation_released_once": cancelled["inventory"]["occupied_room_nights"] == 0
                    and cancelled["audit"]["events"] == 2,
                "distinct_expected_capacity": booked_rows == expected,
                "distinct_expected_room_nights": final["inventory"]["occupied_room_nights"] == expected * 3,
                "database_audit_healthy": all(s["audit"]["healthy"] for s in (booked, cancelled, final)),
            }
            return {"concurrency": concurrency, "phases": {
                "same_key_booking": replay, "same_key_cancellation": cancel,
                "distinct_booking_sellout": distinct, "state_reads": reads},
                "checks": checks, "all_checks_passed": all(checks.values()),
                "final_booked": booked_rows, "final_room_nights": final["inventory"]["occupied_room_nights"]}
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16, 32])
    parser.add_argument("--requests", type=int, default=64, help="requests per phase, 4 phases per level")
    parser.add_argument("--output", type=Path, default=ROOT / "results/http-benchmark.json")
    args = parser.parse_args()
    if not 4 <= args.requests <= 256 or any(not 1 <= n <= 32 for n in args.concurrency) or len(args.concurrency) > 8:
        parser.error("bounded run: 4..256 requests, 1..32 concurrency, at most 8 levels")
    report = {"synthetic_only": True, "started_utc": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "platform": platform.platform(),
              "scope": "single process, loopback, serialized demo writes, fresh temporary DB per level; no external targets",
              "limitations": "closed-loop short bursts, new connection per request; not sustained load, real traffic, distributed capacity or attack resistance",
              "levels": [run_level(n, args.requests) for n in args.concurrency]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(args.output)
    if any(not level["all_checks_passed"] or any(
        p["transport_errors"] or set(p["http_counts"]) != {"200"}
        for p in level["phases"].values()) for level in report["levels"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
