"""Load explicit synthetic inputs and compare policies on exactly the same demand."""
from collections import Counter
from datetime import date
import hashlib
import json
import re
from pathlib import Path

from .allocation import Allocator, BookingRequest, Policy, Room, RoomType


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("scenario dates must use canonical YYYY-MM-DD")
    return date.fromisoformat(value)


def array(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array, never a string")
    return value


def records(value, name):
    rows = array(value, name)
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{name} must contain JSON objects")
    return rows


def load_scenario(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported scenario schema")
    return data, hashlib.sha256(raw).hexdigest()


def build_allocator(data, mode):
    start, end = parse_date(data["horizon_start"]), parse_date(data["horizon_end"])
    types = tuple(RoomType(**row) for row in records(data["room_types"], "room_types"))
    rooms = tuple(Room(**{**row, "blocked_dates": frozenset(parse_date(day) for day in array(row.get("blocked_dates", []), "blocked_dates"))})
                  for row in records(data["rooms"], "rooms"))
    resources = records(data["daily_resources"], "daily_resources")
    resource_days = [parse_date(row["date"]) for row in resources]
    if len(set(resource_days)) != len(resource_days):
        raise ValueError("duplicate resource date")
    rates = records(data.get("daily_rates", []), "daily_rates")
    rate_keys = [(row["room_type"], parse_date(row["date"])) for row in rates]
    if len(set(rate_keys)) != len(rate_keys):
        raise ValueError("duplicate nightly rate")
    upgrades = data["allowed_upgrades"]
    if not isinstance(upgrades, dict):
        raise ValueError("allowed_upgrades must be a JSON object")
    return Allocator(types, rooms, horizon_start=start, horizon_end=end,
                     cot_capacity={day: row["cots"] for day, row in zip(resource_days, resources)},
                     extra_bed_capacity={day: row["extra_beds"] for day, row in zip(resource_days, resources)},
                     upgrades={key: tuple(array(value, "upgrade targets")) for key, value in upgrades.items()},
                     policy=Policy(mode, tuple((row["room_type"], parse_date(row["date"]), row["count"])
                                               for row in records(data.get("protection", []), "protection"))),
                     rates={key: row["rate_cents"] for key, row in zip(rate_keys, rates)},
                     cot_age_limit=data.get("cot_age_limit", 2))


def parse_requests(data):
    requests = tuple(BookingRequest(**{**row,
        "check_in": parse_date(row["check_in"]), "check_out": parse_date(row["check_out"]),
        "accepted_bed_types": tuple(array(row["accepted_bed_types"], "accepted_bed_types")),
        "child_ages": tuple(array(row.get("child_ages", []), "child_ages"))})
        for row in records(data["requests"], "requests"))
    if len({request.request_id for request in requests}) != len(requests):
        raise ValueError("scenario request IDs must be unique")
    return requests


def simulate(data, input_sha256=None):
    requests = parse_requests(data)
    demand = Counter(request.check_in.isoformat() for request in requests)
    result = {"schema_version": 1, "candidate_status": "new_independent_cloud_simulation_not_production",
              "data_origin": data.get("data_origin", "unspecified"), "input_sha256": input_sha256,
              "input_request_count": len(requests), "processing_order": [request.request_id for request in requests],
              "daily_arrival_demand": dict(sorted(demand.items())), "policies": {}}
    for mode in ("strict", "upgrade", "protected_upgrade"):
        engine = build_allocator(data, mode)
        decisions = engine.allocate_many(requests)
        accepted = [decision for decision in decisions if decision.accepted]
        state = engine.snapshot()
        available_nights = sum(day not in room.blocked_dates for room in engine.rooms for day in engine.days)
        reasons = Counter(reason for decision in decisions for reason in decision.rejection_reasons)
        result["policies"][mode] = {
            "summary": {"accepted": len(accepted), "rejected": len(decisions) - len(accepted),
                        "upgraded": sum(decision.upgraded for decision in accepted),
                        "accepted_room_nights": state["occupied_room_nights"],
                        "available_room_nights": available_nights,
                        "occupancy_fraction": round(state["occupied_room_nights"] / available_nights, 6) if available_nights else 0,
                        "modeled_upgrade_cost_cents": sum(decision.upgrade_cost_cents for decision in accepted),
                        "accepted_with_children": sum(decision.accepted and bool(request.child_ages)
                                                       for request, decision in zip(requests, decisions)),
                        "rejection_reason_counts_nonexclusive": dict(sorted(reasons.items()))},
            "decisions": [decision.to_dict() for decision in decisions], "inventory": state}
    return result
