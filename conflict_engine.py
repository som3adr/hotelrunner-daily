"""
conflict_engine.py
──────────────────
Detects room, bed, and capacity conflicts across all 3 houses.

Rules:
  - Same physical room, overlapping dates = conflict
  - Checkout/check-in on same day = NOT a conflict (guest A leaves, guest B arrives)
  - Dorm: count total guests vs bed capacity — no per-bed ID conflict
  - Private room: any 2 different guests in same room = conflict
  - Group booking (same guest name, multiple rooms) = legitimate, NOT a conflict
  - Villa (Tide): max capacity check
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from data_model import NormalizedReservation, load_config


# ── Conflict result types ─────────────────────────────────────────────────────

@dataclass
class RoomConflict:
    severity: str           # action_required | check | info
    room: str
    house: str
    conflict_start: dt.date
    conflict_end: dt.date
    reservations: list[NormalizedReservation]
    description: str
    recommended_action: str
    first_detected: dt.date = field(default_factory=dt.date.today)
    alert_id: str = ""

    def __post_init__(self):
        if not self.alert_id:
            ids = "_".join(sorted(r.reservation_id for r in self.reservations))
            self.alert_id = f"conflict_{self.room}_{self.conflict_start}_{ids}"[:80]

    @property
    def days_until_arrival(self) -> int:
        return (self.conflict_start - dt.date.today()).days

    @property
    def escalated_severity(self) -> str:
        cfg = load_config()
        conflict_cfg = cfg.get("conflicts", {})
        urgent_days = conflict_cfg.get("urgent_days_before", 3)
        warning_days = conflict_cfg.get("warning_days_before", 7)
        days = self.days_until_arrival
        if days <= urgent_days:
            return "urgent"
        if days <= warning_days:
            return "action_required"
        return "check"


@dataclass
class CapacityWarning:
    severity: str
    room: str
    house: str
    date: dt.date
    booked: int
    capacity: int
    guests: list[NormalizedReservation]
    description: str

    @property
    def overflow(self) -> int:
        return self.booked - self.capacity


# ── Room key helpers ──────────────────────────────────────────────────────────

def _room_key(res: NormalizedReservation) -> str:
    """Canonical key for grouping reservations into the same physical room."""
    bed = res.bed.strip().casefold()
    room = res.room.strip().casefold()
    if bed:
        return f"bed:{bed}"
    return f"room:{room}"


def _is_shared_room(room: str, house: str) -> bool:
    """True for dormitories and Sunrise rooms where guests share beds up to room capacity."""
    if house.casefold() == "sunrise":
        return True
    cfg = load_config()
    rooms_cfg = cfg.get("houses", {}).get(house, {}).get("rooms", {})
    room_lower = room.casefold()
    for room_key_cfg, room_data in rooms_cfg.items():
        if room_key_cfg.casefold() in room_lower or room_lower in room_key_cfg.casefold():
            return room_data.get("type", "") in {"dorm", "shared", "triple", "quad"}
    return "dorm" in room_lower



def _room_capacity(room: str, house: str) -> int | None:
    cfg = load_config()
    rooms_cfg = cfg.get("houses", {}).get(house, {}).get("rooms", {})
    room_lower = room.casefold()
    # Try to match by bed number or name
    for room_key_cfg, room_data in rooms_cfg.items():
        bed_num = str(room_data.get("bed_number", ""))
        if (room_key_cfg.casefold() in room_lower
                or room_lower in room_key_cfg.casefold()
                or (bed_num and bed_num in room_lower)):
            return room_data.get("capacity")
    return None


# ── Date overlap logic ────────────────────────────────────────────────────────

def _overlaps(a_arr: dt.date, a_dep: dt.date, b_arr: dt.date, b_dep: dt.date) -> bool:
    """
    True if two stays overlap as OVERNIGHT stays.
    checkout/checkin on same day is NOT an overnight overlap.
    Overlap iff: a_arr < b_dep AND b_arr < a_dep
    """
    return a_arr < b_dep and b_arr < a_dep


def _overlap_range(a_arr: dt.date, a_dep: dt.date, b_arr: dt.date, b_dep: dt.date) -> tuple[dt.date, dt.date]:
    start = max(a_arr, b_arr)
    end = min(a_dep, b_dep)
    return start, end


# ── Group booking detection ───────────────────────────────────────────────────

def _is_group_booking(res_a: NormalizedReservation, res_b: NormalizedReservation) -> bool:
    """Same guest name in two reservations = group booking, not a conflict."""
    return res_a.guest_name.casefold() == res_b.guest_name.casefold()


def _find_available_rooms(
    house: str,
    needed_capacity: int,
    conflict_start: dt.date,
    conflict_end: dt.date,
    all_reservations: list[NormalizedReservation],
) -> list[str]:
    """Find rooms in a house that are free during the conflict period."""
    cfg = load_config()
    rooms_cfg = cfg.get("houses", {}).get(house, {}).get("rooms", {})

    available = []
    for room_name, room_data in rooms_cfg.items():
        cap = room_data.get("capacity", 0)
        if cap < needed_capacity:
            continue
        # Check if this room is free
        occupied = False
        for res in all_reservations:
            if res.house != house:
                continue
            room_match = (
                room_name.casefold() in res.room.casefold()
                or res.room.casefold() in room_name.casefold()
                or (room_data.get("bed_number") and str(room_data["bed_number"]) == res.bed)
            )
            if room_match and res.arrival_date and res.departure_date:
                if _overlaps(res.arrival_date, res.departure_date, conflict_start, conflict_end):
                    occupied = True
                    break
        if not occupied:
            available.append(f"{house} — {room_name} (capacity {cap})")
    return available


# ── Main conflict detection ───────────────────────────────────────────────────

def detect_conflicts(
    reservations: list[NormalizedReservation],
    check_date: dt.date | None = None,
    lookahead_days: int = 30,
) -> tuple[list[RoomConflict], list[CapacityWarning]]:
    """
    Scan all reservations for conflicts over the next `lookahead_days` days.

    Returns:
      - room_conflicts: overlapping reservations in same room
      - capacity_warnings: total guests > room capacity on any night
    """
    today = check_date or dt.date.today()
    horizon = today + dt.timedelta(days=lookahead_days)

    # Filter to reservations within lookahead window
    active = [
        r for r in reservations
        if r.arrival_date and r.departure_date
        and r.departure_date > today
        and r.arrival_date <= horizon
        and r.status not in ("cancelled", "canceled")
    ]

    room_conflicts: list[RoomConflict] = []
    capacity_warnings: list[CapacityWarning] = []
    seen_conflict_ids: set[str] = set()

    # Group by house + room key
    by_room: dict[str, list[NormalizedReservation]] = {}
    for res in active:
        key = f"{res.house}::{_room_key(res)}"
        by_room.setdefault(key, []).append(res)

    for room_key_str, room_res in by_room.items():
        if len(room_res) < 2:
            continue

        house_name = room_key_str.split("::")[0]
        is_shared = _is_shared_room(room_res[0].room, house_name)

        if is_shared:
            # For shared rooms / dorms: check capacity per night
            _check_dorm_capacity(room_res, house_name, capacity_warnings, today, horizon)
        else:

            # For private rooms: check any overlap between different guests
            for i, res_a in enumerate(room_res):
                for res_b in room_res[i + 1:]:
                    if _is_group_booking(res_a, res_b):
                        continue  # Same guest = group booking, OK
                    if not (res_a.arrival_date and res_a.departure_date and
                            res_b.arrival_date and res_b.departure_date):
                        continue
                    if not _overlaps(res_a.arrival_date, res_a.departure_date,
                                     res_b.arrival_date, res_b.departure_date):
                        continue

                    start, end = _overlap_range(
                        res_a.arrival_date, res_a.departure_date,
                        res_b.arrival_date, res_b.departure_date
                    )

                    conflict_id = f"conflict_{res_a.reservation_id}_{res_b.reservation_id}"
                    if conflict_id in seen_conflict_ids:
                        continue
                    seen_conflict_ids.add(conflict_id)

                    # Try to find alternative rooms
                    total_needed = max(res_a.guest_count, res_b.guest_count)
                    alternatives: list[str] = []
                    for alt_house in ("Olas", "Tide", "Sunrise"):
                        alts = _find_available_rooms(alt_house, total_needed, start, end, active)
                        alternatives.extend(alts[:2])  # max 2 per house

                    room_name = res_a.room or res_b.room
                    alt_text = ""
                    if alternatives:
                        alt_text = "Possible alternatives: " + " | ".join(alternatives[:3])
                    else:
                        alt_text = "No obvious alternative found — review manually"

                    conflict = RoomConflict(
                        severity="action_required",
                        room=room_name,
                        house=house_name,
                        conflict_start=start,
                        conflict_end=end,
                        reservations=[res_a, res_b],
                        description=(
                            f"{res_a.guest_name} ({res_a.guest_count}p, {res_a.hr_number or res_a.reservation_id}) "
                            f"vs {res_b.guest_name} ({res_b.guest_count}p, {res_b.hr_number or res_b.reservation_id})"
                        ),
                        recommended_action=alt_text,
                        alert_id=conflict_id,
                    )
                    room_conflicts.append(conflict)

    return room_conflicts, capacity_warnings


def _check_dorm_capacity(
    dorm_res: list[NormalizedReservation],
    house: str,
    warnings: list[CapacityWarning],
    today: dt.date,
    horizon: dt.date,
) -> None:
    """Check dorm occupancy per night vs bed capacity."""
    if not dorm_res:
        return
    room_name = dorm_res[0].room
    capacity = _room_capacity(room_name, house)
    if not capacity:
        return  # Unknown capacity, skip

    current = today
    while current < horizon:
        guests_tonight = [r for r in dorm_res if r.is_active_on(current)]
        total_guests = sum(r.guest_count for r in guests_tonight)
        if total_guests > capacity:
            warnings.append(CapacityWarning(
                severity="action_required",
                room=room_name,
                house=house,
                date=current,
                booked=total_guests,
                capacity=capacity,
                guests=guests_tonight,
                description=f"{total_guests} guests booked in {room_name} (capacity {capacity})",
            ))
        current += dt.timedelta(days=1)
