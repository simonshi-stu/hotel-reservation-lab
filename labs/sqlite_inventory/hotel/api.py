"""Loopback-only HTTP server with a fixed connection pool and bounded waiting queue."""
import argparse
import io
import json
import logging
from collections import deque
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
from .service import Problem, Store

class DeadlineReader(io.RawIOBase):
    """Recompute a total I/O deadline before EVERY recv, including partial lines."""
    def __init__(self, connection, deadline, server):
        super().__init__()
        self.connection, self.deadline, self.server = connection, deadline, server
    def readable(self):
        return True
    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise TimeoutError("Total request read deadline exceeded")
            self.connection.settimeout(remaining)
            return self.connection.recv_into(buffer)
        except TimeoutError:
            self.server.count("read_timeouts")
            raise

class SocketWriter(io.BufferedIOBase):
    def __init__(self, connection):
        super().__init__()
        self.connection = connection
    def writable(self):
        return True
    def write(self, data):
        self.connection.sendall(data)
        return len(data)

class Server(HTTPServer):
    allow_reuse_address = True

    def __init__(self, address, store, max_active=8, queue_size=32,
                 queue_timeout=1.0, read_timeout=3.0, response_timeout=0.25):
        if type(max_active) is not int or not 1 <= max_active <= 64:
            raise ValueError("max_active must be 1-64 fixed connection workers")
        if type(queue_size) is not int or not 1 <= queue_size <= 256:
            raise ValueError("queue_size must be 1-256; zero would be unbounded")
        if not 0 < queue_timeout <= 10 or not 0 < read_timeout <= 30 or not 0 < response_timeout <= 2:
            raise ValueError("Invalid queue/read/response timeout")
        self.store = store
        self.worker_limit, self.queue_limit = max_active, queue_size
        self.queue_timeout, self.read_timeout, self.response_timeout = queue_timeout, read_timeout, response_timeout
        self.request_queue_size = min(256, max_active + queue_size)
        self.pending = deque()
        self.closing = threading.Event()
        self.context = threading.local()
        self.metrics_lock = threading.Lock()
        self.queue_condition = threading.Condition(self.metrics_lock)
        self.metrics = {"accepted":0, "active":0, "pending":0, "live":0,
                        "peak_active":0, "peak_pending":0, "peak_live":0,
                        "queue_full":0, "queue_expired":0, "shutdown_rejections":0,
                        "read_timeouts":0, "response_write_errors":0,
                        "handler_errors":0, "finished_connections":0,
                        "reject_drain_deadlines":0,"reject_peer_resets":0,"reject_drain_byte_limits":0}
        self.workers = []
        super().__init__(address, Handler)
        for number in range(max_active):
            worker = threading.Thread(target=self.worker, name="hotel-worker-" + str(id(self)) + "-" + str(number), daemon=True)
            self.workers.append(worker)
            worker.start()

    def count(self, key, amount=1):
        with self.metrics_lock:
            self.metrics[key] = self.metrics.get(key, 0) + amount

    def stats(self):
        with self.metrics_lock:
            result = dict(self.metrics)
        result.update({"worker_limit":self.worker_limit,"queue_limit":self.queue_limit,
                       "worker_threads_alive":sum(t.is_alive() for t in self.workers)})
        return result

    def process_request(self, request, client_address):
        """Runs in the single acceptor; no thread is created for a connection."""
        accepted_at = time.monotonic()
        admitted = False
        with self.queue_condition:
            self.metrics["accepted"] += 1
            self.metrics["live"] += 1
            self.metrics["peak_live"] = max(self.metrics["peak_live"],self.metrics["live"])
            if not self.closing.is_set() and len(self.pending)<self.queue_limit:
                self.pending.append((request,client_address,accepted_at))
                self.metrics["pending"] += 1
                self.metrics["peak_pending"] = max(self.metrics["peak_pending"],self.metrics["pending"])
                self.queue_condition.notify()
                admitted = True
        if not admitted:
            reason = "server_shutdown" if self.closing.is_set() else "server_busy"
            self.count("shutdown_rejections" if self.closing.is_set() else "queue_full")
            self.reject(request,reason)
            self.finish_socket(request)

    def serve_forever(self, poll_interval=0.05):
        super().serve_forever(poll_interval=poll_interval)

    def service_actions(self):
        # Reap expired queued sockets without waiting for a busy connection worker.
        expired=[]
        with self.queue_condition:
            while self.pending and time.monotonic()-self.pending[0][2]>self.queue_timeout:
                expired.append(self.pending.popleft()[0])
                self.metrics["pending"] -= 1
                self.metrics["queue_expired"] += 1
        for connection in expired:
            self.reject(connection,"queue_wait_timeout")
            self.finish_socket(connection)

    def reject(self, connection, reason):
        """A bounded write from the acceptor or an existing worker; no business call."""
        payload={"error":reason,"message":"No business operation started; retry later with the same request key",
                 "retryable":True,"outcome":"not_committed"}
        body=json.dumps(payload).encode("utf-8")
        headers=("HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
                 "Content-Length: "+str(len(body))+"\r\nRetry-After: 1\r\n"
                 "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode("ascii")
        deadline=time.monotonic()+self.response_timeout
        try:
            connection.settimeout(self.response_timeout)
            connection.sendall(headers+body)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            self.count("response_write_errors")
            return
        # Let a client finish sending/reading before close, but never linger without bounds.
        remaining=65536
        try:
            while remaining:
                left=deadline-time.monotonic()
                if left<=0:
                    self.count("reject_drain_deadlines")
                    return
                connection.settimeout(left)
                data=connection.recv(min(8192,remaining))
                if not data:
                    return
                remaining-=len(data)
            self.count("reject_drain_byte_limits")
        except TimeoutError:
            self.count("reject_drain_deadlines")
        except OSError:
            self.count("reject_peer_resets")

    def finish_socket(self, connection):
        self.shutdown_request(connection)
        with self.metrics_lock:
            self.metrics["live"] -= 1
            self.metrics["finished_connections"] += 1

    def worker(self):
        while True:
            with self.queue_condition:
                while not self.pending and not self.closing.is_set():
                    self.queue_condition.wait()
                if not self.pending:
                    return
                connection,address,accepted_at=self.pending.popleft()
                self.metrics["pending"] -= 1
                self.metrics["active"] += 1
                self.metrics["peak_active"] = max(self.metrics["peak_active"],self.metrics["active"])
            try:
                if self.closing.is_set():
                    self.count("shutdown_rejections")
                    self.reject(connection,"server_shutdown")
                elif time.monotonic()-accepted_at > self.queue_timeout:
                    self.count("queue_expired")
                    self.reject(connection,"queue_wait_timeout")
                else:
                    self.context.read_deadline = time.monotonic()+self.read_timeout
                    self.finish_request(connection,address)
            except (ConnectionError, OSError):
                self.count("handler_errors")
            except Exception:
                self.count("handler_errors")
                self.handle_error(connection,address)
            finally:
                self.finish_socket(connection)
                with self.metrics_lock:
                    self.metrics["active"] -= 1

    def server_close(self):
        with self.queue_condition:
            self.closing.set()
            self.queue_condition.notify_all()
        super().server_close()
        for worker in self.workers:
            if worker is not threading.current_thread():
                worker.join(timeout=self.read_timeout+getattr(self.store,"timeout",5)+self.response_timeout+1)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.connection = self.request
        self.rfile = io.BufferedReader(DeadlineReader(self.connection,self.server.context.read_deadline,self.server))
        self.wfile = SocketWriter(self.connection)

    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.connection.settimeout(self.server.response_timeout)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection","close")
            if status == 503:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.server.count("response_write_errors")

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
            payload={"error":exc.code,"message":str(exc)}
            if exc.status == 503:
                payload.update({"retryable":True,"outcome":"not_committed"})
            self.reply(exc.status,payload)
        except (TimeoutError, ConnectionError):
            self.reply(408, {"error": "request_timeout","message":"Request body not completed"})
        except Exception:
            logging.exception("Hotel API unexpected failure")
            self.reply(500, {"error": "internal_error", "message": "See local server log"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--max-active", type=int, default=8,help="Fixed connection worker count (1-64)")
    parser.add_argument("--queue-size",type=int,default=32)
    parser.add_argument("--queue-timeout",type=float,default=1.0)
    parser.add_argument("--read-timeout",type=float,default=3.0)
    parser.add_argument("--response-timeout",type=float,default=0.25)
    args = parser.parse_args()
    from pathlib import Path
    if not Path(args.db).is_file():
        parser.error("Database missing; run scripts.seed first")
    try:
        server = Server(("127.0.0.1", args.port), Store(args.db), args.max_active,
                        args.queue_size,args.queue_timeout,args.read_timeout,args.response_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    print("Synthetic hotel API: http://127.0.0.1:" + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()

