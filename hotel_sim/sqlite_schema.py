"""SQLite v1: statements execute one by one in the initialization transaction."""
VERSION = 1
STATEMENTS = [
"""CREATE TABLE configuration(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    mode TEXT NOT NULL, config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL)""",
"""CREATE TABLE room_calendar(room_id TEXT NOT NULL, night TEXT NOT NULL,
    room_type TEXT NOT NULL, blocked INTEGER NOT NULL CHECK(blocked IN (0,1)),
    PRIMARY KEY(room_id,night))""",
"""CREATE TABLE auxiliary_inventory(night TEXT PRIMARY KEY,
    cot_capacity INTEGER NOT NULL CHECK(typeof(cot_capacity)='integer' AND cot_capacity>=0),
    extra_capacity INTEGER NOT NULL CHECK(typeof(extra_capacity)='integer' AND extra_capacity>=0),
    cots_used INTEGER NOT NULL DEFAULT 0 CHECK(typeof(cots_used)='integer' AND cots_used BETWEEN 0 AND cot_capacity),
    extras_used INTEGER NOT NULL DEFAULT 0 CHECK(typeof(extras_used)='integer' AND extras_used BETWEEN 0 AND extra_capacity))""",
"""CREATE TABLE bookings(request_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL, decision_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('BOOKED','CANCELLED','REJECTED')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    cancelled_at TEXT,
    CHECK((status='CANCELLED' AND cancelled_at IS NOT NULL) OR (status<>'CANCELLED' AND cancelled_at IS NULL)))""",
"""CREATE TABLE booking_nights(request_id TEXT NOT NULL REFERENCES bookings(request_id),
    room_id TEXT NOT NULL, night TEXT NOT NULL,
    cots INTEGER NOT NULL CHECK(typeof(cots)='integer' AND cots>=0),
    extra_beds INTEGER NOT NULL CHECK(typeof(extra_beds)='integer' AND extra_beds>=0),
    PRIMARY KEY(request_id,night), UNIQUE(request_id,room_id,night),
    FOREIGN KEY(room_id,night) REFERENCES room_calendar(room_id,night))""",
"""CREATE TABLE active_room_nights(room_id TEXT NOT NULL, night TEXT NOT NULL, request_id TEXT NOT NULL,
    PRIMARY KEY(room_id,night), UNIQUE(request_id,night),
    FOREIGN KEY(request_id,room_id,night) REFERENCES booking_nights(request_id,room_id,night))""",
"""CREATE TABLE reservation_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES bookings(request_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('BOOKED','CANCELLED','REJECTED')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(request_id,event_type))""",
"""CREATE TRIGGER bookings_identity_guard BEFORE UPDATE ON bookings
 WHEN NEW.request_id<>OLD.request_id OR NEW.request_json<>OLD.request_json
 OR NEW.request_sha256<>OLD.request_sha256 OR NEW.decision_json<>OLD.decision_json
 OR NEW.created_at<>OLD.created_at
 BEGIN SELECT RAISE(ABORT,'booking identity and original decision are immutable'); END""",
"""CREATE TRIGGER booking_status_guard BEFORE UPDATE OF status ON bookings
 WHEN NEW.status<>OLD.status AND NOT(OLD.status='BOOKED' AND NEW.status='CANCELLED')
 BEGIN SELECT RAISE(ABORT,'invalid booking state transition'); END""",
"""CREATE TRIGGER cancellation_requires_release BEFORE UPDATE OF status ON bookings
 WHEN NEW.status='CANCELLED' AND EXISTS(SELECT 1 FROM active_room_nights WHERE request_id=OLD.request_id)
 BEGIN SELECT RAISE(ABORT,'cancel must release active nights'); END""",
"""CREATE TRIGGER active_room_insert_guard BEFORE INSERT ON active_room_nights
 WHEN (SELECT status FROM bookings WHERE request_id=NEW.request_id)<>'BOOKED'
 OR (SELECT blocked FROM room_calendar WHERE room_id=NEW.room_id AND night=NEW.night)<>0
 BEGIN SELECT RAISE(ABORT,'room night is not bookable'); END""",
"""CREATE TRIGGER history_insert_guard BEFORE INSERT ON booking_nights
 WHEN (SELECT status FROM bookings WHERE request_id=NEW.request_id)<>'BOOKED'
 BEGIN SELECT RAISE(ABORT,'only booked requests may receive history'); END""",
"""CREATE TRIGGER auxiliary_capacity_guard BEFORE UPDATE ON auxiliary_inventory
 WHEN NEW.night<>OLD.night OR NEW.cot_capacity<>OLD.cot_capacity OR NEW.extra_capacity<>OLD.extra_capacity
 BEGIN SELECT RAISE(ABORT,'inventory configuration is immutable'); END""",
]
for table in ('configuration', 'room_calendar', 'booking_nights', 'reservation_events'):
    for operation in ('UPDATE','DELETE'):
        STATEMENTS.append(f"CREATE TRIGGER {table}_{operation.lower()}_guard BEFORE {operation} ON {table} "
                          "BEGIN SELECT RAISE(ABORT,'immutable configuration or history'); END")
STATEMENTS.extend([
    "CREATE TRIGGER bookings_delete_guard BEFORE DELETE ON bookings BEGIN SELECT RAISE(ABORT,'booking history is retained'); END",
    "CREATE TRIGGER active_update_guard BEFORE UPDATE ON active_room_nights BEGIN SELECT RAISE(ABORT,'active nights are inserted or released'); END",
    "CREATE TRIGGER auxiliary_delete_guard BEFORE DELETE ON auxiliary_inventory BEGIN SELECT RAISE(ABORT,'inventory configuration is retained'); END",
])
