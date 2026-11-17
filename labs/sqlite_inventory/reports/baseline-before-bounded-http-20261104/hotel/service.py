"""Atomic bookings across all occupied nights, safe between OS processes."""
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

class Problem(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status

def text_id(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise Problem("invalid_input", field + " must contain 1-80 letters, digits, _ or -")
    return value

def nights(check_in, check_out):
    try:
        if not all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)
                   for v in (check_in, check_out)):
            raise ValueError()
        start, end = date.fromisoformat(check_in), date.fromisoformat(check_out)
    except (ValueError, TypeError):
        raise Problem("invalid_dates", "Use valid ISO dates YYYY-MM-DD")
    count = (end - start).days
    if not 1 <= count <= 30:
        raise Problem("invalid_dates", "Stay must be 1-30 nights; checkout is exclusive")
    return [(start + timedelta(days=i)).isoformat() for i in range(count)]

def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

class Store:
    def __init__(self, path, timeout=5.0):
        self.path = str(Path(path).resolve())
        self.timeout = timeout

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def initialize(self):
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError("Refusing to overwrite existing database: " + self.path)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

    @contextmanager
    def transaction(self, write=False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise Problem("database_busy", "Writer busy; retry with the same idempotency key", 503) from exc
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def availability(self, room_type_id, check_in, check_out):
        text_id(room_type_id, "room_type_id")
        days = nights(check_in, check_out)
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT stay_date,capacity,available,price_cents FROM nightly_inventory "
                "WHERE room_type_id=? AND stay_date>=? AND stay_date<? ORDER BY stay_date",
                (room_type_id, check_in, check_out)).fetchall()
            if [r["stay_date"] for r in rows] != days:
                raise Problem("inventory_not_found", "Room type or one or more nights unavailable in calendar", 404)
            return {"room_type_id": room_type_id, "check_in": check_in, "check_out": check_out,
                    "available": min(r["available"] for r in rows),
                    "unit_stay_price_cents": sum(r["price_cents"] for r in rows),
                    "nights": [dict(r) for r in rows]}

    def reserve(self, customer_id, room_type_id, check_in, check_out, quantity, idempotency_key):
        for field, value in (("customer_id", customer_id), ("room_type_id", room_type_id),
                             ("idempotency_key", idempotency_key)):
            text_id(value, field)
        days = nights(check_in, check_out)
        if type(quantity) is not int or not 1 <= quantity <= 10:
            raise Problem("invalid_quantity", "Quantity must be an integer between 1 and 10")
        with self.transaction(write=True) as connection:
            old = connection.execute("SELECT * FROM reservations WHERE customer_id=? AND idempotency_key=?",
                                     (customer_id, idempotency_key)).fetchone()
            if old:
                same = (old["room_type_id"], old["check_in"], old["check_out"], old["quantity"])
                if same != (room_type_id, check_in, check_out, quantity):
                    raise Problem("idempotency_conflict", "Key already used with another request", 409)
                return {**dict(old), "replayed": True}
            if not connection.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                raise Problem("customer_not_found", "Unknown synthetic customer", 404)
            rows = connection.execute(
                "SELECT * FROM nightly_inventory WHERE room_type_id=? AND stay_date>=? "
                "AND stay_date<? ORDER BY stay_date", (room_type_id, check_in, check_out)).fetchall()
            if [r["stay_date"] for r in rows] != days:
                raise Problem("inventory_not_found", "Room type or one or more nights outside calendar", 404)
            if any(r["available"] < quantity for r in rows):
                raise Problem("sold_out", "Not enough rooms on every occupied night", 409)
            reservation_id, timestamp = uuid.uuid4().hex, now()
            total = quantity * sum(r["price_cents"] for r in rows)
            connection.execute(
                "INSERT INTO reservations VALUES (?,?,?,?,?,?,?,'CONFIRMED',?,?,NULL)",
                (reservation_id, customer_id, room_type_id, check_in, check_out, quantity,
                 total, idempotency_key, timestamp))
            for row in rows:
                changed = connection.execute(
                    "UPDATE nightly_inventory SET available=available-? "
                    "WHERE room_type_id=? AND stay_date=? AND available>=?",
                    (quantity, room_type_id, row["stay_date"], quantity)).rowcount
                if changed != 1:
                    raise Problem("sold_out", "Inventory changed; entire stay rolled back", 409)
                connection.execute("INSERT INTO reservation_nights VALUES (?,?,?,?,?)",
                                   (reservation_id, room_type_id, row["stay_date"], quantity, row["price_cents"]))
                connection.execute(
                    "INSERT INTO inventory_events(reservation_id,room_type_id,stay_date,kind,delta,created_at) "
                    "VALUES (?,?,?,'BOOK',?,?)",
                    (reservation_id, room_type_id, row["stay_date"], -quantity, timestamp))
            return {**dict(connection.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()),
                    "replayed": False}

    def get(self, reservation_id, customer_id):
        text_id(reservation_id, "reservation_id")
        text_id(customer_id, "customer_id")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM reservations WHERE id=? AND customer_id=?",
                                     (reservation_id, customer_id)).fetchone()
            if not row:
                raise Problem("reservation_not_found", "Reservation not found for customer", 404)
            return dict(row)

    def cancel(self, reservation_id, customer_id):
        text_id(reservation_id, "reservation_id")
        text_id(customer_id, "customer_id")
        with self.transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM reservations WHERE id=? AND customer_id=?",
                                     (reservation_id, customer_id)).fetchone()
            if not row:
                raise Problem("reservation_not_found", "Reservation not found for customer", 404)
            if row["status"] == "CANCELLED":
                return {**dict(row), "replayed": True}
            timestamp = now()
            occupied = connection.execute("SELECT * FROM reservation_nights WHERE reservation_id=?",
                                          (reservation_id,)).fetchall()
            for night in occupied:
                changed = connection.execute(
                    "UPDATE nightly_inventory SET available=available+? "
                    "WHERE room_type_id=? AND stay_date=? AND available+?<=capacity",
                    (night["quantity"], night["room_type_id"], night["stay_date"], night["quantity"])).rowcount
                if changed != 1:
                    raise Problem("inventory_inconsistent", "Cancellation rejected by inventory constraint", 500)
                connection.execute(
                    "INSERT INTO inventory_events(reservation_id,room_type_id,stay_date,kind,delta,created_at) "
                    "VALUES (?,?,?,'CANCEL',?,?)",
                    (reservation_id, night["room_type_id"], night["stay_date"], night["quantity"], timestamp))
            connection.execute("UPDATE reservations SET status='CANCELLED',cancelled_at=? WHERE id=?",
                               (timestamp, reservation_id))
            return {**dict(connection.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()),
                    "replayed": False}

    def audit(self):
        """Independent reconciliation against active bookings AND event ledger."""
        with self.transaction() as connection:
            violations = []
            for row in connection.execute(
                "SELECT i.*,COALESCE(a.used,0) AS used,COALESCE(e.delta,0) AS delta "
                "FROM nightly_inventory i LEFT JOIN "
                "(SELECT n.room_type_id,n.stay_date,SUM(n.quantity) AS used FROM reservation_nights n "
                "JOIN reservations r ON r.id=n.reservation_id WHERE r.status='CONFIRMED' "
                "GROUP BY n.room_type_id,n.stay_date) a USING(room_type_id,stay_date) LEFT JOIN "
                "(SELECT room_type_id,stay_date,SUM(delta) AS delta FROM inventory_events "
                "GROUP BY room_type_id,stay_date) e USING(room_type_id,stay_date)"):
                if row["available"] != row["capacity"] - row["used"] or row["available"] != row["capacity"] + row["delta"]:
                    violations.append(dict(row))
            bad_bookings = connection.execute(
                "SELECT r.id FROM reservations r LEFT JOIN reservation_nights n ON n.reservation_id=r.id "
                "GROUP BY r.id HAVING COUNT(n.stay_date)!=CAST(julianday(r.check_out)-julianday(r.check_in) AS INTEGER) "
                "OR SUM(n.quantity*n.unit_price_cents)!=r.total_cents "
                "OR MIN(n.quantity)!=r.quantity OR MAX(n.quantity)!=r.quantity "
                "OR MIN(n.stay_date)!=r.check_in OR MAX(n.stay_date)!=date(r.check_out,'-1 day') "
                "OR MIN(n.room_type_id)!=r.room_type_id OR MAX(n.room_type_id)!=r.room_type_id").fetchall()
            bad_events = connection.execute(
                "SELECT n.reservation_id,n.stay_date FROM reservation_nights n "
                "JOIN reservations r ON r.id=n.reservation_id "
                "LEFT JOIN inventory_events b ON b.reservation_id=n.reservation_id AND b.stay_date=n.stay_date AND b.kind='BOOK' "
                "LEFT JOIN inventory_events c ON c.reservation_id=n.reservation_id AND c.stay_date=n.stay_date AND c.kind='CANCEL' "
                "WHERE b.id IS NULL OR b.delta!=-n.quantity OR b.room_type_id!=n.room_type_id "
                "OR (r.status='CANCELLED' AND (c.id IS NULL OR c.delta!=n.quantity OR c.room_type_id!=n.room_type_id)) "
                "OR (r.status='CONFIRMED' AND c.id IS NOT NULL)").fetchall()
            foreign_keys = [tuple(r) for r in connection.execute("PRAGMA foreign_key_check")]
            integrity = [r[0] for r in connection.execute("PRAGMA integrity_check")]
            return {"ok": not (violations or bad_bookings or bad_events or foreign_keys) and integrity == ["ok"],
                    "inventory_violations": violations, "booking_violations": [r[0] for r in bad_bookings],
                    "event_violations": [tuple(r) for r in bad_events],
                    "foreign_key_violations": foreign_keys, "integrity": integrity}

