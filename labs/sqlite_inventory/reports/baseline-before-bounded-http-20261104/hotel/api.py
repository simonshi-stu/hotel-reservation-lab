"""Loopback-only educational HTTP API, not an authenticated production server."""
import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from .service import Problem, Store

class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, store, max_active=32):
        self.store = store
        self.slots = threading.BoundedSemaphore(max_active)
        super().__init__(address, Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status == 503:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def body(self):
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError()
            sizes = self.headers.get_all("Content-Length", [])
            if len(sizes) != 1:
                raise ValueError()
            size = int(sizes[0])
            if not 0 < size <= 16384:
                raise ValueError()
            if self.headers.get_content_type() != "application/json":
                raise Problem("invalid_content_type", "Use application/json", 415)
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, UnicodeError):
            raise Problem("invalid_json", "Send a JSON object with Content-Length of 1-16384 bytes")

    def dispatch(self, method):
        self.connection.settimeout(5)
        if not self.server.slots.acquire(blocking=False):
            self.reply(503, {"error": "server_busy", "message": "Too many active requests; retry later"})
            return
        try:
            route = urlsplit(self.path)
            store = self.server.store
            if method == "GET" and route.path == "/health":
                self.reply(200, {"status": "ok", "database": "sqlite", "synthetic": True})
                return
            if method == "GET" and route.path == "/availability":
                values = parse_qs(route.query, keep_blank_values=True)
                expected = {"room_type_id", "check_in", "check_out"}
                if set(values) != expected or any(len(v) != 1 for v in values.values()):
                    raise Problem("invalid_input", "Supply room_type_id, check_in and check_out once")
                self.reply(200, store.availability(**{k: v[0] for k, v in values.items()}))
                return
            if method == "POST" and route.path == "/reservations":
                payload = self.body()
                expected = {"customer_id", "room_type_id", "check_in", "check_out", "quantity", "idempotency_key"}
                if set(payload) != expected:
                    raise Problem("invalid_input", "Required fields: " + ", ".join(sorted(expected)))
                result = store.reserve(**payload)
                self.reply(200 if result["replayed"] else 201, result)
                return
            parts = route.path.strip("/").split("/")
            if len(parts) in (2, 3) and parts[0] == "reservations":
                reservation_id = parts[1]
                if method == "GET" and len(parts) == 2:
                    query = parse_qs(route.query, keep_blank_values=True)
                    if set(query) != {"customer_id"} or len(query["customer_id"]) != 1:
                        raise Problem("invalid_input", "Supply customer_id once")
                    self.reply(200, store.get(reservation_id, query["customer_id"][0]))
                    return
                if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                    payload = self.body()
                    if set(payload) != {"customer_id"}:
                        raise Problem("invalid_input", "Required field: customer_id")
                    self.reply(200, store.cancel(reservation_id, payload["customer_id"]))
                    return
            raise Problem("not_found", "Unknown endpoint", 404)
        except Problem as exc:
            self.reply(exc.status, {"error": exc.code, "message": str(exc)})
        except (TimeoutError, ConnectionError):
            self.reply(408, {"error": "request_timeout"})
        except Exception:
            logging.exception("Hotel API unexpected failure")
            self.reply(500, {"error": "internal_error", "message": "See local server log"})
        finally:
            self.server.slots.release()

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--max-active", type=int, default=32)
    args = parser.parse_args()
    if not 1 <= args.max_active <= 256:
        parser.error("--max-active must be 1-256")
    from pathlib import Path
    if not Path(args.db).is_file():
        parser.error("Database missing; run scripts.seed first")
    server = Server(("127.0.0.1", args.port), Store(args.db), args.max_active)
    print("Synthetic hotel API: http://127.0.0.1:" + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()

