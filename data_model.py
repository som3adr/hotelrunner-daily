"""
data_model.py
─────────────
Unified NormalizedReservation dataclass that represents reservations
from ANY source (HotelRunner, Google Sheet, direct booking) in a single
consistent format for the Manager Report engine.

Also contains adapters from:
  - StayLine (HotelRunner) → NormalizedReservation
  - Google Sheet row (dict) → NormalizedReservation
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Config loader ─────────────────────────────────────────────────────────────

_CONFIG: dict[str, Any] | None = None

def load_config(path: Path | None = None) -> dict[str, Any]:
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    config_path = path or (Path(__file__).parent / "config.json")
    if config_path.exists():
        _CONFIG = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        _CONFIG = {}
    return _CONFIG


# ── NormalizedExtra ───────────────────────────────────────────────────────────

@dataclass
class NormalizedExtra:
    raw_label: str
    category: str           # transfer | surf_equipment | surf_lesson | meal | tax | unknown
    quantity: int | None = None
    amount: float | None = None
    dates: list[dt.date] = field(default_factory=list)
    operational_note: str | None = None

    def is_actionable(self) -> bool:
        return self.category not in {"tax"}


# ── MealEntitlement ───────────────────────────────────────────────────────────

@dataclass
class MealEntitlement:
    """Auditable record of one guest's meal entitlement for a specific meal and date."""
    reservation_id: str
    guest_name: str
    house: str
    date: dt.date
    meal: str               # breakfast | lunch | dinner
    count: int              # number of covers (usually 1 per adult)
    source: str             # meal_plan | extra | note
    reason_text: str        # human-readable e.g. "Half Board" or "dinner extra"


# ── TransferRecord ────────────────────────────────────────────────────────────

@dataclass
class TransferRecord:
    """Normalized transfer record for one guest arrival or departure."""
    reservation_id: str
    guest_name: str
    house: str
    direction: str              # arrival | departure
    date: dt.date
    passenger_count: int = 1
    flight_number: str = ""
    airport: str = ""
    pickup_time: str = ""       # HH:MM for departures
    destination: str = ""
    raw_source: str = ""        # all_inclusive | note | extra
    status: str = "needs_info"  # needs_info | ready_to_send | sent

    @property
    def is_arrival(self) -> bool:
        return self.direction == "arrival"

    @property
    def is_departure(self) -> bool:
        return self.direction == "departure"


# ── AttentionItem ─────────────────────────────────────────────────────────────

@dataclass
class AttentionItem:
    """An operational item requiring manager attention — not a hard conflict."""
    category: str       # meal | transfer | extra | room | surf
    severity: str       # info | check | action_required
    title: str
    description: str
    reservation_id: str = ""


# ── NormalizedReservation ─────────────────────────────────────────────────────

@dataclass
class NormalizedReservation:
    # Identity
    reservation_id: str
    source: str             # hotelrunner | google_sheet | direct
    hr_number: str = ""

    # Guest
    guest_name: str = ""
    adults: int = 0
    children: int = 0

    # Location
    house: str = ""         # Olas | Tide | Sunrise
    room: str = ""
    bed: str = ""

    # Dates
    arrival_date: dt.date | None = None
    departure_date: dt.date | None = None

    # Meal & booking
    meal_plan: str = ""
    status: str = ""        # confirmed | reserved | cancelled
    channel: str = ""

    # Extras
    extras: list[NormalizedExtra] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    bed_request: str = ""

    # Transfers (parsed from sheet or extras)
    arrival_transfer: str | None = None    # e.g. "13:05 - Agadir Airport"
    departure_transfer: str | None = None  # e.g. "FR9347" or "05h45 from the house"

    # Surf (Google Sheet)
    surf_package: str | None = None
    surf_level: str | None = None          # Beginner | Intermediate | Advanced
    surf_goals: str | None = None

    # Health (Google Sheet)
    diet: str | None = None
    allergies: str | None = None
    medical_notes: str | None = None

    # Payment (HotelRunner)
    total_amount: float = 0.0
    paid_amount: float = 0.0
    currency: str = "EUR"
    payment_record_count: int = 0
    booking_date: dt.date | None = None

    # Group booking flag
    is_group_booking: bool = False
    group_leader: str = ""

    @property
    def guest_count(self) -> int:
        return self.adults + self.children

    @property
    def nights(self) -> int:
        if self.arrival_date and self.departure_date:
            return (self.departure_date - self.arrival_date).days
        return 0

    @property
    def unpaid_amount(self) -> float:
        return max(0.0, self.total_amount - self.paid_amount)

    @property
    def has_unpaid_balance(self) -> bool:
        return self.unpaid_amount > 0.5  # tolerance for rounding

    @property
    def has_transfer_in(self) -> bool:
        return bool(self.arrival_transfer)

    @property
    def has_transfer_out(self) -> bool:
        return bool(self.departure_transfer)

    @property
    def transfer_extras(self) -> list[NormalizedExtra]:
        return [e for e in self.extras if e.category == "transfer"]

    @property
    def surf_extras(self) -> list[NormalizedExtra]:
        return [e for e in self.extras if e.category in {"surf_equipment", "surf_lesson"}]

    @property
    def meal_extras(self) -> list[NormalizedExtra]:
        return [e for e in self.extras if e.category == "meal"]

    @property
    def unknown_extras(self) -> list[NormalizedExtra]:
        return [e for e in self.extras if e.category == "unknown"]

    def is_active_on(self, date: dt.date) -> bool:
        """True if guest is in-house on this date (arrival <= date < departure)."""
        if not self.arrival_date or not self.departure_date:
            return False
        return self.arrival_date <= date < self.departure_date

    def is_arriving_on(self, date: dt.date) -> bool:
        return self.arrival_date == date

    def is_departing_on(self, date: dt.date) -> bool:
        return self.departure_date == date


# ── House detection helpers ───────────────────────────────────────────────────

def detect_house_from_stayline(room_name: str, channel: str) -> str:
    """Map StayLine room/channel to house name.

    Sunrise guests booked via HotelRunner have room/bed IDs starting with
    9200, 9300, 9400, 9500, or 9600 (Sunrise private rooms + 9300 dorm).
    Tide rooms are identified by name keywords.
    Everything else → Olas.
    """
    # Check Sunrise HotelRunner prefixes first
    cfg = load_config()
    sunrise_prefixes = tuple(
        cfg.get("sunrise_hotelrunner", {}).get("all_sunrise_prefixes", ["9200", "9300", "9400", "9500", "9600"])
    )
    for part in (room_name.strip(), channel.strip()):
        if part and any(part.startswith(p) for p in sunrise_prefixes):
            return "Sunrise"

    # Check Tide keywords
    text = f"{room_name} {channel}".casefold()
    if any(w in text for w in ["tidehunter", "bay", "slab", "cathedral", "reef"]):
        return "Tide"

    return "Olas"


def detect_house_from_sheet_room(room: str) -> str:
    """Map Google Sheet room name to house name."""
    if room.casefold().startswith("sunrise"):
        return "Sunrise"
    return "Unknown"


# ── Adapter: StayLine → NormalizedReservation ─────────────────────────────────

def stayline_to_normalized(line: Any, raw_reservation: dict[str, Any] | None = None) -> NormalizedReservation:
    """
    Convert a StayLine (from hotelrunner_daily_summary.py) to a NormalizedReservation.
    Optionally pass the raw reservation dict for payment data.
    """
    house = detect_house_from_stayline(line.room_name, line.channel)

    # Payment info from raw reservation if available
    total_amount = 0.0
    paid_amount = 0.0
    payment_record_count = 0
    booking_date = None
    if raw_reservation:
        try:
            total_amount = float(raw_reservation.get("total") or 0)
        except (TypeError, ValueError):
            total_amount = 0.0
        try:
            paid_amount = float(raw_reservation.get("paid_amount") or 0)
        except (TypeError, ValueError):
            paid_amount = 0.0
        payments = raw_reservation.get("payments")
        if isinstance(payments, list):
            payment_record_count = len([
                payment for payment in payments
                if not isinstance(payment, dict)
                or str(payment.get("state") or "").casefold() not in {"failed", "cancelled", "canceled"}
            ])
        completed_at = str(raw_reservation.get("completed_at") or "").strip()
        currency = str(raw_reservation.get("currency") or "EUR").strip().upper()
        if completed_at:
            try:
                booking_date = dt.date.fromisoformat(completed_at[:10])
            except ValueError:
                booking_date = None
    else:
        total_amount = float(getattr(line, "total_amount", 0) or 0)
        paid_amount = float(getattr(line, "paid_amount", 0) or 0)
        payment_record_count = int(getattr(line, "payment_record_count", 0) or 0)
        booking_date = getattr(line, "booking_date", None)
        currency = str(getattr(line, "currency", "EUR") or "EUR").strip().upper()

    return NormalizedReservation(
        reservation_id=line.reservation_id,
        source="hotelrunner",
        hr_number=line.hr_number,
        guest_name=line.guest_name,
        adults=line.adults,
        children=line.children,
        house=house,
        room=line.room_name,
        bed=line.bed_number,
        arrival_date=line.arrival,
        departure_date=line.departure,
        meal_plan=line.meal_plan,
        status="confirmed",
        channel=line.channel,
        notes=list(line.notes),
        bed_request=line.bed_request,
        total_amount=total_amount,
        paid_amount=paid_amount,
        currency=currency,
        payment_record_count=payment_record_count,
        booking_date=booking_date,
    )


def _reservation_identity_key(res: NormalizedReservation) -> tuple[str, dt.date | None, dt.date | None]:
    name = re.sub(r"[^a-z0-9]+", "", res.guest_name.casefold())
    return name, res.arrival_date, res.departure_date


def merge_cross_source_duplicates(
    reservations: list[NormalizedReservation],
) -> list[NormalizedReservation]:
    """Merge a matching Sunrise Sheet row into HotelRunner without double counting.

    Only exact guest/date matches across different sources are merged. Multiple
    HotelRunner stay lines for the same group remain separate.
    """
    hotelrunner_by_key = {
        _reservation_identity_key(res): res
        for res in reservations
        if res.source == "hotelrunner"
    }
    merged: list[NormalizedReservation] = []
    for res in reservations:
        if res.source != "google_sheet":
            merged.append(res)
            continue
        target = hotelrunner_by_key.get(_reservation_identity_key(res))
        if target is None:
            merged.append(res)
            continue
        for field_name in (
            "meal_plan", "surf_package", "surf_level", "surf_goals",
            "diet", "allergies", "medical_notes", "arrival_transfer",
            "departure_transfer",
        ):
            if not getattr(target, field_name) and getattr(res, field_name):
                setattr(target, field_name, getattr(res, field_name))
        for note in res.notes:
            if note not in target.notes:
                target.notes.append(note)
    return merged


# ── Adapter: Google Sheet row → NormalizedReservation ─────────────────────────

def _parse_date_flexible(value: str | None) -> dt.date | None:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


def _normalize_room_name(raw: str) -> str:
    """Extract clean room identifier from sheet Room column.
    e.g. 'Sunrise 4 - 1 double + 1 single' → 'Sunrise 4'
    """
    match = re.match(r"(Sunrise\s*\d+)", raw, re.I)
    if match:
        return match.group(1).strip()
    return raw.strip()


def sheet_row_to_normalized(row: dict[str, str], row_index: int = 0) -> NormalizedReservation | None:
    """
    Convert a Google Sheet row (column name → value dict) to a NormalizedReservation.
    Returns None if the row has no guest name or dates (skip empty rows).
    """
    cfg = load_config()
    col_map = cfg.get("google_sheets", {}).get("sunrise_columns", {})

    def get(col_key: str) -> str:
        col_name = col_map.get(col_key, col_key)
        return str(row.get(col_name) or row.get(col_key) or "").strip()

    guest_name = get("guest_name")
    if not guest_name:
        return None

    arrival = _parse_date_flexible(get("checkin"))
    departure = _parse_date_flexible(get("checkout"))
    if not arrival or not departure:
        return None

    # Each row in the Sunrise sheet represents 1 guest
    adults = 1
    raw_room = get("room")
    room = _normalize_room_name(raw_room)
    house = detect_house_from_sheet_room(room)

    return NormalizedReservation(
        reservation_id=f"sheet-{row_index}-{guest_name.replace(' ', '_')}",
        source="google_sheet",
        guest_name=guest_name,
        adults=adults,
        children=0,

        house=house,
        room=room,
        bed="",
        arrival_date=arrival,
        departure_date=departure,
        meal_plan=_infer_meal_from_package(get("surf_package")),
        status="confirmed",
        channel="direct",
        arrival_transfer=get("estimated_arrival") or None,
        departure_transfer=get("estimated_departure") or None,
        surf_package=get("surf_package") or None,
        surf_level=get("surf_level") or None,
        surf_goals=get("surf_goals") or None,
        diet=get("diet") or None,
        allergies=get("allergies") or None,
        medical_notes=get("notes") or None,
    )


def _infer_guest_count_from_room(room_str: str) -> int:
    """Guess guest count from room description like 'Sunrise 4 - 1 double + 1 single'."""
    total = 0
    for match in re.finditer(r"(\d+)\s*x?\s*(single|double|queen|king|bed)", room_str, re.I):
        n = int(match.group(1))
        kind = match.group(2).casefold()
        total += 2 * n if kind == "double" else n
    return total if total > 0 else 1


def _infer_meal_from_package(package: str) -> str:
    """Infer meal plan from surf package string."""
    if not package:
        return "Room Only"
    p = package.casefold()
    if "full board" in p or "all inclusive" in p:
        return "All Inclusive"
    if "half board" in p or "hb" in p:
        return "Half Board"
    if "breakfast" in p or "bb" in p or "b&b" in p:
        return "Bed And Breakfast"
    return "Room Only"


# ── Meal counting ─────────────────────────────────────────────────────────────

def meals_for_reservation(res: NormalizedReservation, date: dt.date) -> dict[str, int]:
    """Return {breakfast: n, lunch: n, dinner: n} for a guest on a specific date."""
    if not res.is_active_on(date):
        return {"breakfast": 0, "lunch": 0, "dinner": 0}

    cfg = load_config()
    plans = cfg.get("meals", {}).get("plans", {})
    plan_key = res.meal_plan.casefold()

    # Find matching plan (fuzzy match)
    matched = None
    for key, value in plans.items():
        if key in plan_key or plan_key in key:
            matched = value
            break

    if not matched:
        matched = {"breakfast": False, "lunch": False, "dinner": False}

    guests = res.guest_count
    result = {
        "breakfast": guests if matched.get("breakfast") else 0,
        "lunch":     guests if matched.get("lunch") else 0,
        "dinner":    guests if matched.get("dinner") else 0,
    }

    # Add meal extras (e.g. half board formula extra on top of room only)
    for extra in res.meal_extras:
        qty = extra.quantity or guests
        label = extra.raw_label.casefold()
        if "lunch" in label:
            result["lunch"] += qty
        elif "dinner" in label or "half board" in label or "souper" in label:
            result["dinner"] += qty
        elif "breakfast" in label:
            result["breakfast"] += qty

    return result


# ── Dinner setup rule ─────────────────────────────────────────────────────────

def dinner_setup_rule(count: int) -> str:
    """
    Returns the dinner setup status based on guest count.
    Thresholds loaded from config (safe defaults: 13 / 24).

    Returns: 'normal' | 'extra_tables' | 'capacity_warning'
    """
    cfg = load_config()
    thresholds = cfg.get("meals", {}).get("dinner_thresholds", {})
    extra_tables_above = thresholds.get("extra_tables_above", 13)
    capacity_warning_above = thresholds.get("capacity_warning_above", 24)

    if count > capacity_warning_above:
        return "capacity_warning"
    if count > extra_tables_above:
        return "extra_tables"
    return "normal"
