"""Generate clearly synthetic hotel data. Existing output is never overwritten."""
import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from hotel.service import Store, Problem

ROOT = Path(__file__).resolve().parents[1]
TABLES = ("hotels", "room_types", "customers", "nightly_inventory", "reservations",
          "reservation_nights", "inventory_events")

def generate(db, output, seed=20261104, customers=2000, attempts=1200, days=60):
    if customers < 10 or attempts < 0 or days < 7:
        raise ValueError("Require >=10 customers, >=0 attempts and >=7 days")
    db, output = Path(db), Path(output)
    if db.exists() or output.exists():
        raise FileExistsError("Choose new DB and output paths; refusing overwrite")
    rng, start = random.Random(seed), date(2027, 1, 1)
    store = Store(db)
    store.initialize()
    capacities = [30, 20, 12, 6]
    types = []
    with store.transaction(write=True) as connection:
        for hotel, city in enumerate(("Shanghai", "Chengdu", "Hangzhou"), 1):
            hid = "H" + str(hotel)
            connection.execute("INSERT INTO hotels VALUES (?,?,?)",
                               (hid, "SIMULATED Hotel " + str(hotel), city))
            for j, name in enumerate(("Standard", "Twin", "Family", "Suite"), 1):
                rid = hid + "-R" + str(j)
                types.append(rid)
                price = [35000, 42000, 58000, 88000][j-1] + (hotel-1)*2000
                connection.execute("INSERT INTO room_types VALUES (?,?,?,?,?)",
                                   (rid, hid, "SIMULATED " + name, price, [2, 2, 4, 2][j-1]))
                for offset in range(days):
                    day = start + timedelta(days=offset)
                    nightly_price = price * (120 if day.weekday() >= 4 else 100) // 100
                    connection.execute("INSERT INTO nightly_inventory VALUES (?,?,?,?,?)",
                                       (rid, day.isoformat(), capacities[j-1], capacities[j-1], nightly_price))
        for i in range(1, customers+1):
            segment = rng.choices(("business","leisure","family"), weights=(35,45,20))[0]
            connection.execute("INSERT INTO customers VALUES (?,?,?,?,?)",
                               ("C" + str(i).zfill(5), "SIMULATED Guest " + str(i),
                                rng.choice(("Beijing","Shanghai","Shenzhen","Chengdu","Wuhan")),
                                segment, "sim-guest-" + str(i) + "@example.invalid"))
    result = Counter()
    for i in range(attempts):
        # 50% of requests target Shanghai Standard: reproducible hotspot.
        rid = rng.choices(types, weights=[11] + [1]*11)[0]
        offset = rng.randrange(days-6)
        length = rng.choices((1,2,3,5), weights=(45,30,20,5))[0]
        day = start + timedelta(days=offset)
        cid = "C" + str(rng.randint(1,customers)).zfill(5)
        try:
            booking = store.reserve(cid, rid, day.isoformat(), (day+timedelta(days=length)).isoformat(),
                                    rng.choices((1,2,3), weights=(80,17,3))[0], "seed-" + str(i).zfill(6))
            # Stable seed identifiers/timestamps make CSV bytes reproducible, not only distributions.
            stable_id = hashlib.sha256(("hotel:" + str(seed) + ":" + str(i)).encode()).hexdigest()[:32]
            stable_time = "2026-11-04T" + str((i // 3600) % 24).zfill(2) + ":" + str((i // 60) % 60).zfill(2) + ":" + str(i % 60).zfill(2) + "+00:00"
            with store.transaction(write=True) as connection:
                connection.execute("PRAGMA defer_foreign_keys=ON")
                connection.execute("UPDATE reservations SET id=?,created_at=? WHERE id=?",
                                   (stable_id, stable_time, booking["id"]))
                for table in ("reservation_nights", "inventory_events"):
                    connection.execute("UPDATE " + table + " SET reservation_id=? WHERE reservation_id=?",
                                       (stable_id, booking["id"]))
                connection.execute("UPDATE inventory_events SET created_at=? WHERE reservation_id=?",
                                   (stable_time, stable_id))
            result["confirmed_created"] += 1
            if rng.random() < 0.18:
                store.cancel(stable_id, cid)
                with store.transaction(write=True) as connection:
                    connection.execute("UPDATE reservations SET cancelled_at=? WHERE id=?", (stable_time, stable_id))
                    connection.execute("UPDATE inventory_events SET created_at=? WHERE reservation_id=?", (stable_time, stable_id))
                result["cancelled"] += 1
        except Problem as exc:
            if exc.code != "sold_out":
                raise
            result["sold_out"] += 1
    output.mkdir(parents=True)
    counts, hashes = {}, {}
    with store.transaction() as connection:
        for table in TABLES:
            rows = connection.execute("SELECT * FROM " + table + " ORDER BY rowid")
            columns = [d[0] for d in rows.description]
            target = output / (table + ".csv")
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                count = 0
                for row in rows:
                    writer.writerow(tuple(row))
                    count += 1
            counts[table] = count
            hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    audit = store.audit()
    if not audit["ok"]:
        raise AssertionError(audit)
    manifest = {"synthetic": True, "seed": seed, "start_date": start.isoformat(), "days": days,
                "customers": customers, "booking_attempts": attempts,
                "outcomes": dict(result), "row_counts": counts, "csv_sha256": hashes,
                "audit": audit, "engine": "SQLite", "reproducibility": "CSV bytes deterministic with same Python and parameters"}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / ".runtime" / "hotel.db"))
    parser.add_argument("--output", default=str(ROOT / "data" / "synthetic-v1"))
    parser.add_argument("--seed", type=int, default=20261104)
    parser.add_argument("--customers", type=int, default=2000)
    parser.add_argument("--attempts", type=int, default=1200)
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(generate(args.db, args.output, args.seed, args.customers, args.attempts, args.days),
                     ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

