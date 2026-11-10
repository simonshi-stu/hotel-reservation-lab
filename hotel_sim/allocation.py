"""Deterministic, in-memory same-room allocation over half-open date intervals.

All guests count toward the room's occupant limit. Children younger than two
need a cot; other guests need a regular bed place (fixed bed or allowed rollaway).
These are explicit simulation rules, not hotel policy or legal advice.
"""
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from threading import RLock
from typing import Iterable


def integer(value, name, low=0):
    if type(value) is not int or value < low:
        raise ValueError(f"{name} must be an integer >= {low}")


def label(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def nights_between(start: date, end: date) -> tuple[date, ...]:
    if type(start) is not date or type(end) is not date:
        raise ValueError("stay dates must be dates, not datetimes")
    count = (end - start).days
    if not 1 <= count <= 30:
        raise ValueError("stay must contain 1 to 30 nights")
    return tuple(start + timedelta(days=index) for index in range(count))


@dataclass(frozen=True)
class RoomType:
    code: str
    rank: int
    max_occupants: int
    max_adults: int
    max_extra_beds: int
    max_cots: int
    base_rate_cents: int
    max_auxiliary_items: int = 2

    def __post_init__(self):
        label(self.code, "room type")
        for name in ("rank", "max_extra_beds", "max_cots", "base_rate_cents", "max_auxiliary_items"):
            integer(getattr(self, name), name)
        integer(self.max_occupants, "max_occupants", 1)
        integer(self.max_adults, "max_adults", 1)
        if self.max_adults > self.max_occupants:
            raise ValueError("adult capacity cannot exceed occupant capacity")


@dataclass(frozen=True)
class Room:
    room_id: str
    room_type: str
    bed_type: str
    fixed_bed_places: int
    blocked_dates: frozenset[date] = frozenset()

    def __post_init__(self):
        for name in ("room_id", "room_type", "bed_type"):
            label(getattr(self, name), name)
        integer(self.fixed_bed_places, "fixed_bed_places", 1)
        if not isinstance(self.blocked_dates, frozenset) or any(type(day) is not date for day in self.blocked_dates):
            raise ValueError("blocked_dates must be a frozenset of dates")


@dataclass(frozen=True)
class BookingRequest:
    request_id: str
    check_in: date
    check_out: date
    requested_type: str
    accepted_bed_types: tuple[str, ...]
    adults: int
    child_ages: tuple[int, ...] = ()
    allow_upgrade: bool = True
    allow_extra_bed: bool = True
    max_upgrade_cost_cents: int = 100_000

    def __post_init__(self):
        label(self.request_id, "request_id")
        label(self.requested_type, "requested_type")
        nights_between(self.check_in, self.check_out)
        integer(self.adults, "adults", 1)
        integer(self.max_upgrade_cost_cents, "max_upgrade_cost_cents")
        if type(self.allow_upgrade) is not bool or type(self.allow_extra_bed) is not bool:
            raise ValueError("allow flags must be booleans")
        if not isinstance(self.child_ages, tuple):
            raise ValueError("child_ages must be an immutable tuple")
        for age in self.child_ages:
            integer(age, "child age")
            if age > 17:
                raise ValueError("child ages must be between 0 and 17")
        if not isinstance(self.accepted_bed_types, tuple) or not self.accepted_bed_types:
            raise ValueError("accepted_bed_types must be a non-empty tuple")
        for bed in self.accepted_bed_types:
            label(bed, "bed type")
        if len(set(self.accepted_bed_types)) != len(self.accepted_bed_types):
            raise ValueError("duplicate accepted bed type")


@dataclass(frozen=True)
class Policy:
    mode: str = "strict"
    # Exact requested-type bookings may consume this reserve; upgrades may not.
    protection: tuple[tuple[str, date, int], ...] = ()

    def __post_init__(self):
        if self.mode not in ("strict", "upgrade", "protected_upgrade"):
            raise ValueError("unknown policy mode")
        if not isinstance(self.protection, tuple):
            raise ValueError("protection must be immutable")
        seen = set()
        for room_type, day, count in self.protection:
            label(room_type, "protection room type")
            if type(day) is not date:
                raise ValueError("protection day must be a date")
            integer(count, "protection count")
            if (room_type, day) in seen:
                raise ValueError("duplicate protection entry")
            seen.add((room_type, day))


@dataclass(frozen=True)
class Decision:
    request_id: str
    accepted: bool
    nights: tuple[str, ...]
    room_id: str | None = None
    assigned_type: str | None = None
    bed_type: str | None = None
    upgraded: bool = False
    extra_beds: int = 0
    cots: int = 0
    upgrade_cost_cents: int = 0
    rejection_reasons: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


class Allocator:
    """Thread-safe only within this Python object; no database or distributed lock."""

    def __init__(self, room_types: Iterable[RoomType], rooms: Iterable[Room], *,
                 horizon_start: date, horizon_end: date,
                 cot_capacity: dict[date, int], extra_bed_capacity: dict[date, int],
                 upgrades: dict[str, tuple[str, ...]], policy: Policy = Policy(),
                 rates: dict[tuple[str, date], int] | None = None, cot_age_limit: int = 2):
        integer(cot_age_limit, "cot_age_limit", 1)
        if cot_age_limit > 18:
            raise ValueError("cot_age_limit must be <= 18")
        self.cot_age_limit = cot_age_limit
        self.days = nights_between(horizon_start, horizon_end)
        types, room_list = tuple(room_types), tuple(rooms)
        self.types = {item.code: item for item in types}
        if not self.types or len(self.types) != len(types):
            raise ValueError("room types must be non-empty and unique")
        self.rooms = tuple(sorted(room_list, key=lambda room: room.room_id))
        if not self.rooms or len({room.room_id for room in self.rooms}) != len(self.rooms):
            raise ValueError("rooms must be non-empty and unique")
        for room in self.rooms:
            if room.room_type not in self.types:
                raise ValueError("unknown room type")
            if room.fixed_bed_places > self.types[room.room_type].max_occupants:
                raise ValueError("fixed bed places exceed occupant capacity")
            if not room.blocked_dates <= set(self.days):
                raise ValueError("blocked dates must lie inside inventory horizon")
        self.policy = policy
        self.protection = {(typ, day): count for typ, day, count in policy.protection}
        if any(typ not in self.types or day not in self.days for typ, day in self.protection):
            raise ValueError("unknown protection type or date")
        if not isinstance(upgrades, dict) or any(not isinstance(value, (list, tuple)) for value in upgrades.values()):
            raise ValueError("upgrade targets must be lists or tuples, never strings")
        self.upgrades = {key: tuple(value) for key, value in upgrades.items()}
        for source, targets in self.upgrades.items():
            if source not in self.types or len(set(targets)) != len(targets):
                raise ValueError("invalid upgrade source or repeated target")
            for target in targets:
                if target not in self.types or self.types[target].rank <= self.types[source].rank:
                    raise ValueError("explicit upgrade edges must increase type rank")
        self.capacities = {"cots": dict(cot_capacity), "extra_beds": dict(extra_bed_capacity)}
        for resource, capacity in self.capacities.items():
            if set(capacity) != set(self.days):
                raise ValueError(f"{resource} must explicitly cover every inventory date")
            for value in capacity.values():
                integer(value, resource)
        self.rates = dict(rates or {})
        for (typ, day), cents in self.rates.items():
            if typ not in self.types or day not in self.days:
                raise ValueError("rate must refer to a known type and inventory date")
            integer(cents, "rate")
        self._occupied = {}
        self._used = {resource: {day: 0 for day in self.days} for resource in self.capacities}
        self._requests = {}
        self._decisions = {}
        self._lock = RLock()

    def _rate(self, typ, day):
        return self.rates.get((typ, day), self.types[typ].base_rate_cents)

    def _remaining(self, typ, day):
        return sum(room.room_type == typ and day not in room.blocked_dates
                   and (room.room_id, day) not in self._occupied for room in self.rooms)

    def allocate(self, request: BookingRequest) -> Decision:
        with self._lock:
            old = self._requests.get(request.request_id)
            if old is not None:
                if old != request:
                    raise ValueError("request ID reused with different content")
                return self._decisions[request.request_id]
            nights = nights_between(request.check_in, request.check_out)
            if request.requested_type not in self.types:
                raise ValueError("unknown requested room type")
            if not set(nights) <= set(self.days):
                return self._reject(request, nights, {"OUTSIDE_INVENTORY_HORIZON"})
            allowed = {request.requested_type}
            if self.policy.mode != "strict" and request.allow_upgrade:
                allowed.update(self.upgrades.get(request.requested_type, ()))
            candidates, reasons = [], set()
            for room in self.rooms:
                if room.room_type not in allowed:
                    continue
                typ = self.types[room.room_type]
                if room.bed_type not in request.accepted_bed_types:
                    reasons.add("BED_TYPE_INCOMPATIBLE")
                    continue
                infants = sum(age < self.cot_age_limit for age in request.child_ages)
                sleepers = request.adults + len(request.child_ages) - infants
                cots, extras = infants, max(0, sleepers - room.fixed_bed_places)
                if (request.adults > typ.max_adults or request.adults + len(request.child_ages) > typ.max_occupants
                        or cots > typ.max_cots or extras > typ.max_extra_beds or cots + extras > typ.max_auxiliary_items
                        or (extras and not request.allow_extra_bed)):
                    reasons.add("PARTY_OR_CHILD_REQUIREMENTS_UNSUPPORTED")
                    continue
                if any(day in room.blocked_dates or (room.room_id, day) in self._occupied for day in nights):
                    reasons.add("NO_SAME_ROOM_FOR_ENTIRE_STAY")
                    continue
                upgraded = room.room_type != request.requested_type
                cost = sum(max(0, self._rate(room.room_type, day) - self._rate(request.requested_type, day))
                           for day in nights) if upgraded else 0
                if cost > request.max_upgrade_cost_cents:
                    reasons.add("UPGRADE_COST_BUDGET_EXCEEDED")
                    continue
                if upgraded and self.policy.mode == "protected_upgrade" and any(
                        self._remaining(room.room_type, day) - 1 < self.protection.get((room.room_type, day), 0)
                        for day in nights):
                    reasons.add("PROTECTED_INVENTORY")
                    continue
                if any(self._used[resource][day] + needed > self.capacities[resource][day]
                       for resource, needed in (("cots", cots), ("extra_beds", extras)) for day in nights):
                    reasons.add("AUXILIARY_INVENTORY_EXHAUSTED")
                    continue
                # Requested type first; then cheapest modeled upgrade, then rank,
                # equipment demand and stable room ID. This is a greedy heuristic.
                key = (upgraded, cost, typ.rank, cots + extras, room.room_id)
                candidates.append((key, room, extras, cots, cost))
            if not candidates:
                return self._reject(request, nights, reasons or {"NO_ELIGIBLE_ROOM_TYPE"})
            _, room, extras, cots, cost = min(candidates, key=lambda item: item[0])
            # Commit only after the entire stay and every resource have passed.
            for day in nights:
                self._occupied[room.room_id, day] = request.request_id
                self._used["cots"][day] += cots
                self._used["extra_beds"][day] += extras
            decision = Decision(request.request_id, True, tuple(day.isoformat() for day in nights),
                                room.room_id, room.room_type, room.bed_type,
                                room.room_type != request.requested_type, extras, cots, cost)
            self._requests[request.request_id] = request
            self._decisions[request.request_id] = decision
            return decision

    def _reject(self, request, nights, reasons):
        decision = Decision(request.request_id, False, tuple(day.isoformat() for day in nights),
                            rejection_reasons=tuple(sorted(reasons)))
        self._requests[request.request_id] = request
        self._decisions[request.request_id] = decision
        return decision

    def allocate_many(self, requests):
        return [self.allocate(request) for request in requests]

    def snapshot(self):
        """Return detached, JSON-safe operational counts (no guest identities)."""
        with self._lock:
            return {
                "occupied_room_nights": len(self._occupied),
                "occupancy": [{"room_id": rid, "date": day.isoformat(), "request_id": request_id}
                              for (rid, day), request_id in sorted(self._occupied.items())],
                "daily": [{"date": day.isoformat(),
                           "available_by_type": {typ: self._remaining(typ, day) for typ in sorted(self.types)},
                           "cots_used": self._used["cots"][day], "cots_capacity": self.capacities["cots"][day],
                           "extra_beds_used": self._used["extra_beds"][day],
                           "extra_beds_capacity": self.capacities["extra_beds"][day]} for day in self.days],
            }
