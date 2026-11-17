PRAGMA foreign_keys = ON;
CREATE TABLE hotels (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL
);
CREATE TABLE room_types (
 id TEXT PRIMARY KEY, hotel_id TEXT NOT NULL REFERENCES hotels(id),
 name TEXT NOT NULL, base_price_cents INTEGER NOT NULL CHECK(base_price_cents>0),
 max_guests INTEGER NOT NULL CHECK(max_guests>0)
);
CREATE TABLE customers (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL,
 segment TEXT NOT NULL CHECK(segment IN ('business','leisure','family')),
 email TEXT NOT NULL UNIQUE
);
CREATE TABLE nightly_inventory (
 room_type_id TEXT NOT NULL REFERENCES room_types(id),
 stay_date TEXT NOT NULL,
 capacity INTEGER NOT NULL CHECK(capacity>=0),
 available INTEGER NOT NULL CHECK(available>=0 AND available<=capacity),
 price_cents INTEGER NOT NULL CHECK(price_cents>0),
 PRIMARY KEY(room_type_id,stay_date)
);
CREATE TABLE reservations (
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 room_type_id TEXT NOT NULL REFERENCES room_types(id),
 check_in TEXT NOT NULL, check_out TEXT NOT NULL CHECK(check_out>check_in),
 quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 10),
 total_cents INTEGER NOT NULL CHECK(total_cents>0),
 status TEXT NOT NULL CHECK(status IN ('CONFIRMED','CANCELLED')),
 idempotency_key TEXT NOT NULL,
 created_at TEXT NOT NULL, cancelled_at TEXT,
 UNIQUE(customer_id,idempotency_key)
);
CREATE INDEX reservation_customer_idx ON reservations(customer_id,created_at);
CREATE TABLE reservation_nights (
 reservation_id TEXT NOT NULL REFERENCES reservations(id),
 room_type_id TEXT NOT NULL, stay_date TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0),
 unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents>0),
 PRIMARY KEY(reservation_id,stay_date),
 FOREIGN KEY(room_type_id,stay_date) REFERENCES nightly_inventory(room_type_id,stay_date)
);
CREATE TABLE inventory_events (
 id INTEGER PRIMARY KEY, reservation_id TEXT NOT NULL REFERENCES reservations(id),
 room_type_id TEXT NOT NULL, stay_date TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('BOOK','CANCEL')),
 delta INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 CHECK((kind='BOOK' AND delta<0) OR (kind='CANCEL' AND delta>0)),
 UNIQUE(reservation_id,stay_date,kind),
 FOREIGN KEY(room_type_id,stay_date) REFERENCES nightly_inventory(room_type_id,stay_date)
);

