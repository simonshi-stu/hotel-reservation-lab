"""Synthetic room compatibility and consented upgrade planning; separate databases."""
import csv
import hashlib
import json
import random
import sqlite3
from datetime import date,timedelta
from pathlib import Path
from .service import Store,Problem,nights

TYPES=[
 {"id":"standard-king","tier":0,"bed":"king","adults":2,"children":0,"child_friendly":False,"capacity":5,"price":35000},
 {"id":"standard-twin","tier":0,"bed":"twin","adults":2,"children":1,"child_friendly":True,"capacity":5,"price":37000},
 {"id":"deluxe-king","tier":1,"bed":"king","adults":2,"children":0,"child_friendly":False,"capacity":2,"price":55000},
 {"id":"deluxe-twin","tier":1,"bed":"twin","adults":2,"children":2,"child_friendly":True,"capacity":2,"price":57000},
 {"id":"suite-king","tier":2,"bed":"king","adults":2,"children":2,"child_friendly":True,"capacity":1,"price":90000},
 {"id":"suite-twin","tier":2,"bed":"twin","adults":4,"children":2,"child_friendly":True,"capacity":1,"price":95000}]
BY_ID={r["id"]:r for r in TYPES}

def compatible(room,request):
    return (room["tier"]>=request["tier"] and room["bed"]==request["bed"] and
            room["adults"]>=request["adults"] and room["children"]>=request["children"] and
            (not request["child_friendly"] or room["child_friendly"]) and
            (room["tier"]==request["tier"] or request["consent"]))

def validate_request(r):
    fields={"id","tier","bed","adults","children","child_friendly","consent","check_in","check_out"}
    if set(r)!=fields or not isinstance(r["id"],str) or not r["id"].isalnum() or len(r["id"])>40:
        raise Problem("invalid_request","Invalid demand fields")
    for key,lo,hi in (("tier",0,2),("adults",1,4),("children",0,2)):
        if type(r[key]) is not int or not lo<=r[key]<=hi:raise Problem("invalid_request",key)
    if r["bed"] not in ("king","twin") or type(r["consent"]) is not bool or type(r["child_friendly"]) is not bool:
        raise Problem("invalid_request","Invalid preference")
    if r["children"] and not r["child_friendly"]:raise Problem("invalid_request","Children require child-friendly room")
    return nights(r["check_in"],r["check_out"])

class PlanningStore(Store):
    def initialize_calendar(self,calendar):
        target=Path(self.path)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():raise FileExistsError(self.path)
        connection=self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
CREATE TABLE inventory(room TEXT NOT NULL,day TEXT NOT NULL,capacity INTEGER NOT NULL CHECK(capacity>=0),
 remaining INTEGER NOT NULL CHECK(remaining>=0 AND remaining<=capacity),PRIMARY KEY(room,day));
CREATE TABLE bookings(id TEXT PRIMARY KEY,payload TEXT NOT NULL,assigned TEXT NOT NULL,total_cents INTEGER NOT NULL CHECK(total_cents>0));
CREATE TABLE moves(seq INTEGER PRIMARY KEY,booking_id TEXT REFERENCES bookings(id),old_room TEXT,new_room TEXT,dates TEXT NOT NULL);
""")
            connection.execute("BEGIN IMMEDIATE")
            for row in calendar:connection.execute("INSERT INTO inventory VALUES (?,?,?,?)",(row["room"],row["day"],row["capacity"],row["capacity"]))
            connection.commit()
        finally:connection.close()

    def book(self,r,upgrades=False):
        days=validate_request(r)
        payload=json.dumps(r,sort_keys=True)
        with self.transaction(write=True) as c:
            old=c.execute("SELECT * FROM bookings WHERE id=?",(r["id"],)).fetchone()
            if old:
                if old["payload"]!=payload:raise Problem("idempotency_conflict","Same id with changed demand",409)
                return {**dict(old),"replayed":True}
            for room in TYPES:
                if c.execute("SELECT COUNT(*) FROM inventory WHERE room=? AND day>=? AND day<?",
                             (room["id"],r["check_in"],r["check_out"])).fetchone()[0]!=len(days):
                    raise Problem("calendar_missing","Entire stay must be in calendar",404)
            def free(room,ds):
                return all(c.execute("SELECT remaining FROM inventory WHERE room=? AND day=?",(room,day)).fetchone()[0]>0 for day in ds)
            def adjust(room,ds,delta):
                for day in ds:
                    count=c.execute("UPDATE inventory SET remaining=remaining+? WHERE room=? AND day=? AND remaining+? BETWEEN 0 AND capacity",
                                    (delta,room,day,delta)).rowcount
                    if count!=1:raise Problem("inventory_conflict","Atomic inventory guard",409)
            exact=[t for t in TYPES if t["tier"]==r["tier"] and compatible(t,r)]
            assigned=None
            for target in exact:
                if free(target["id"],days):
                    assigned=target["id"];break
                if not upgrades:continue
                c.execute("SAVEPOINT rearrange")
                # Deterministic bounded greedy search, never a global optimum claim.
                existing=c.execute("SELECT * FROM bookings WHERE assigned=? ORDER BY id",(target["id"],)).fetchall()
                for booking in existing[:40]:
                    previous=json.loads(booking["payload"])
                    occupied=nights(previous["check_in"],previous["check_out"])
                    if not set(days)&set(occupied) or not previous["consent"]:continue
                    destinations=[t for t in TYPES if t["tier"]>BY_ID[booking["assigned"]]["tier"] and compatible(t,previous)]
                    dest=next((t for t in destinations if free(t["id"],occupied)),None)
                    if not dest:continue
                    adjust(dest["id"],occupied,-1);adjust(booking["assigned"],occupied,1)
                    c.execute("UPDATE bookings SET assigned=? WHERE id=?",(dest["id"],booking["id"]))
                    c.execute("INSERT INTO moves(booking_id,old_room,new_room,dates) VALUES (?,?,?,?)",
                              (booking["id"],booking["assigned"],dest["id"],json.dumps(occupied)))
                    if free(target["id"],days):
                        assigned=target["id"];break
                if assigned:
                    c.execute("RELEASE rearrange");break
                c.execute("ROLLBACK TO rearrange");c.execute("RELEASE rearrange")
            if assigned is None and upgrades:
                assigned=next((t["id"] for t in TYPES if t["tier"]>r["tier"] and compatible(t,r) and free(t["id"],days)),None)
            if assigned is None:raise Problem("no_compatible_inventory","No compatible room for entire stay",409)
            adjust(assigned,days,-1)
            requested=next(t for t in TYPES if t["tier"]==r["tier"] and t["bed"]==r["bed"])
            price=sum(requested["price"]*(120 if date.fromisoformat(day).weekday()>=4 else 100)//100 for day in days)
            c.execute("INSERT INTO bookings VALUES (?,?,?,?)",(r["id"],payload,assigned,price))
            return {"id":r["id"],"assigned":assigned,"total_cents":price,"replayed":False}

    def report(self):
        with self.transaction() as c:
            inventory=[dict(x) for x in c.execute("SELECT * FROM inventory ORDER BY day,room")]
            bookings=[dict(x) for x in c.execute("SELECT * FROM bookings ORDER BY id")]
            moves=[dict(x) for x in c.execute("SELECT * FROM moves ORDER BY seq")]
            used={}
            violations=[]
            people=room_nights=revenue=upgraded=0
            for b in bookings:
                r=json.loads(b["payload"]);ds=nights(r["check_in"],r["check_out"]);room=BY_ID.get(b["assigned"])
                if not room or not compatible(room,r):violations.append(b["id"]+": incompatibility")
                if room and room["tier"]>r["tier"]:upgraded+=1
                expected=next(t for t in TYPES if t["tier"]==r["tier"] and t["bed"]==r["bed"])
                expected_price=sum(expected["price"]*(120 if date.fromisoformat(day).weekday()>=4 else 100)//100 for day in ds)
                if b["total_cents"]!=expected_price:violations.append(b["id"]+": price changed")
                for day in ds:used[(b["assigned"],day)]=used.get((b["assigned"],day),0)+1
                people+=r["adults"]+r["children"];room_nights+=len(ds);revenue+=b["total_cents"]
            for row in inventory:
                if row["remaining"]!=row["capacity"]-used.get((row["room"],row["day"]),0):
                    violations.append(str(row)+": inventory")
            if c.execute("PRAGMA foreign_key_check").fetchall():violations.append("foreign key")
            if c.execute("PRAGMA integrity_check").fetchone()[0]!="ok":violations.append("integrity")
            return {"accepted":len(bookings),"people":people,"room_nights":room_nights,"revenue_cents":revenue,
                    "upgraded_reservations":upgraded,"existing_moves":len(moves),"violations":violations,
                    "inventory":inventory,"bookings":bookings,"moves":moves}

def generate(seed=20261104,scale=1):
    rng=random.Random(seed);start=date(2027,1,1)
    calendar=[]
    for i in range(16):
        day=start+timedelta(days=i)
        for t in TYPES:
            cap=t["capacity"]*scale
            if i in (5,12) and t["tier"]==1:cap=max(0,cap-1)
            calendar.append({"room":t["id"],"day":day.isoformat(),"capacity":cap})
    daily=[];demand=[]
    for i in range(10):
        day=start+timedelta(days=i)
        visits=rng.randint(28,55)+(20 if day.weekday()>=4 else 0)
        converted=0
        for visitor in range(visits):
            if rng.random()>.32:continue
            bed=rng.choice(("king","twin"));children=rng.choices((0,1,2),(65,25,10))[0]
            tier=rng.choices((0,1,2),(75,20,5))[0]
            checkin=day+timedelta(days=rng.randint(0,2));checkout=checkin+timedelta(days=rng.randint(1,4))
            demand.append({"id":"D"+str(len(demand)+1).zfill(4),"tier":tier,"bed":bed,
                           "adults":rng.choices((1,2,3,4),(20,70,7,3))[0],"children":children,
                           "child_friendly":children>0,"consent":rng.random()<.7,
                           "check_in":checkin.isoformat(),"check_out":checkout.isoformat()})
            converted+=1
        daily.append({"arrival_day":day.isoformat(),"visits":visits,"booking_requests":converted})
    return {"seed":seed,"scale":scale,"types":TYPES,"calendar":calendar,"daily":daily,"demand":demand}

def compare(folder,seed=20261104,scale=1):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    data=generate(seed,scale)
    (folder/"input.json").write_text(json.dumps(data,indent=2),encoding="utf-8")
    for name in ("calendar","daily","demand","types"):
        rows=data[name]
        with (folder/(name+".csv")).open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    reports={}
    for enabled in (False,True):
        label="upgrade" if enabled else "no_upgrade"
        store=PlanningStore(folder/(label+".db"));store.initialize_calendar(data["calendar"])
        rejected=[]
        for r in data["demand"]:
            try:store.book(r,enabled)
            except Problem as exc:
                if exc.code!="no_compatible_inventory":raise
                rejected.append({"id":r["id"],"error":exc.code})
        result=store.report();result["rejected"]=rejected
        result["rejection_rate"]=len(rejected)/len(data["demand"])
        if result["violations"]:raise AssertionError(result["violations"])
        reports[label]=result
    result={"synthetic":True,"seed":seed,"scale":scale,"request_count":len(data["demand"]),"daily":data["daily"],
            "types":TYPES,"demand":data["demand"],"strategies":reports,
            "input_sha256":hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest(),
            "assumptions":"One room per party; exact bed; 0-17 synthetic child band; free upgrades only with prior consent; locked requested-room rate; no downgrades; one assigned type for entire stay; bounded greedy relocation, not optimum.",
            "warning":"Counterfactual synthetic simulation, not observed revenue or guaranteed improvement."}
    (folder/"comparison.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    return result

