from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


API_BASE_URL = "https://app.hotelrunner.com/api/v2/apps"
DEFAULT_DAYS_AHEAD = 7
DEFAULT_UPDATE_LOOKBACK_DAYS = 14
DEFAULT_CACHE_PATH = "reservations_cache.json"
DEFAULT_MAX_PAGES = 15


BED_PATTERNS = [
    ("single + large double requested", re.compile(r"\b(single bed|1 single).{0,80}\b(large double|double bed|king|queen|grand lit)\b|\b(large double|double bed|king|queen|grand lit).{0,80}\b(single bed|1 single)\b", re.I)),
    ("separate beds", re.compile(r"\b(separate|twin beds|two single|2 single|2 singles|two singles|lits separes)\b", re.I)),
    ("single bed requested", re.compile(r"\b(1 single bed|single bed)\b", re.I)),
    ("large double bed", re.compile(r"\b(1 large double|large double|large bed|double bed|king|queen|matrimonial|lit double|grand lit)\b", re.I)),
]

NEGATED_SEPARATE_BED_PATTERN = re.compile(
    r"\b(don't|do not|dont|not|no)\b.{0,50}\b(separate|twin|two single|2 single|single beds)\b",
    re.I,
)

NOISY_NOTE_PATTERNS = [
    re.compile(r"\*\*\s*THIS RESERVATION HAS BEEN PRE-PAID\s*\*\*", re.I),
    re.compile(r"payment charge is .*?(?=(?:Flag:|VAT|City tax|Adult Count:|$))", re.I),
    re.compile(r"Payment \(.*?\)", re.I),
    re.compile(r"VAT .*?(?=(?:,|Adult Count:|$))", re.I),
    re.compile(r"City tax .*?(?=(?:,|Adult Count:|$))", re.I),
    re.compile(r"Adult Count:\s*\d+", re.I),
    re.compile(r"Flag:\s*\S+", re.I),
    re.compile(r"https?://\S+", re.I),
]


@dataclass(frozen=True)
class StayLine:
    reservation_id: str
    hr_number: str
    guest_name: str
    channel: str
    room_name: str
    bed_number: str
    arrival: dt.date
    departure: dt.date
    adults: int
    children: int
    meal_plan: str
    notes: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    bed_request: str = ""
    total_amount: float = 0.0
    paid_amount: float = 0.0
    payment_record_count: int = 0
    booking_date: dt.date | None = None

    @property
    def guests(self) -> int:
        return self.adults + self.children


@dataclass
class DaySummary:
    date: dt.date
    arrivals: list[StayLine] = field(default_factory=list)
    departures: list[StayLine] = field(default_factory=list)
    in_house: list[StayLine] = field(default_factory=list)
    meals: Counter[str] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    bed_requests: list[str] = field(default_factory=list)
    possible_duplicates: list[str] = field(default_factory=list)
    room_conflicts: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    block_conflicts: list[str] = field(default_factory=list)

    @property
    def adults(self) -> int:
        return sum(line.adults for line in self.in_house)

    @property
    def children(self) -> int:
        return sum(line.children for line in self.in_house)

    @property
    def guests(self) -> int:
        return self.adults + self.children


@dataclass(frozen=True)
class RoomBlock:
    label: str
    room: str
    start: dt.date
    end: dt.date
    guests: int = 0
    note: str = ""


@dataclass(frozen=True)
class RunStatus:
    safe_to_send: bool
    title: str
    message: str
    details: tuple[str, ...] = ()


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines from .env without adding a dependency."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing {name}. Add it to your .env file or environment variables.")
    return value


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value

    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass

    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def first_value(data: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def nested_text(value: Any) -> list[str]:
    """Collect useful text from unknown note/message structures."""
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(nested_text(item))
        return output
    if isinstance(value, dict):
        output = []
        for key in ("message", "body", "text", "note", "notes", "description", "content", "request", "value"):
            output.extend(nested_text(value.get(key)))
        return output
    return []


def extract_notes(reservation: dict[str, Any]) -> list[str]:
    keys = [
        "notes",
        "note",
        "guest_notes",
        "reservation_note",
        "special_requests",
        "customer_note",
        "messages",
        "message",
        "remarks",
    ]
    notes: list[str] = []
    for key in keys:
        notes.extend(nested_text(reservation.get(key)))

    seen: set[str] = set()
    clean_notes: list[str] = []
    for note in notes:
        note = re.sub(r"\s+", " ", note).strip()
        note = clean_note(note)
        if note and note.lower() not in seen:
            seen.add(note.lower())
            clean_notes.append(note)
    return clean_notes


def clean_note(note: str) -> str:
    for pattern in NOISY_NOTE_PATTERNS:
        note = pattern.sub("", note)

    note = re.sub(r"\bBOOKING NOTE\s*:\s*", "", note, flags=re.I)
    note = re.sub(r"\bReservation has a cancellation grace period\..*?(?=(?:Approximate time|$))", "", note, flags=re.I)
    note = re.sub(r"\bThis is a Smart Flex reservation\..*?(?=(?:Approximate time|$))", "", note, flags=re.I)
    note = re.sub(r"\s+,", ",", note)
    note = re.sub(r"\s{2,}", " ", note)
    return note.strip(" ,.;")


def detect_bed_request(notes: list[str], reservation: dict[str, Any]) -> str:
    text_parts = notes + nested_text(
        first_value(
            reservation,
            [
                "bed_type",
                "bed_preference",
                "bedding",
                "room_bed_type",
                "system_message",
                "meta_data_conversions",
                "integration_echo_details",
                "channel_profile_data_conversions",
            ],
        )
    )
    text = " ".join(text_parts)
    text = re.sub(r"\bDouble or Twin Room with Private Bathroom\b", "", text, flags=re.I)
    text = re.sub(r"\bDouble or Twin Room\b", "", text, flags=re.I)
    found: list[str] = []
    for label, pattern in BED_PATTERNS:
        if pattern.search(text):
            if label in {"separate beds", "single + large double requested"} and NEGATED_SEPARATE_BED_PATTERN.search(text):
                continue
            found.append(label)
    if "single + large double requested" in found:
        found = ["single + large double requested"]
    return ", ".join(dict.fromkeys(found))


def reservation_name(reservation: dict[str, Any]) -> str:
    direct_name = first_value(reservation, ["guest", "guest_name", "customer_name", "name", "holder_name"])
    if isinstance(direct_name, str) and direct_name.strip():
        return direct_name.strip()

    guest = first_value(reservation, ["guest", "customer", "primary_guest"], {})
    if isinstance(guest, dict):
        full_name = first_value(guest, ["name", "full_name"])
        if full_name:
            return str(full_name)
        names = [str(first_value(guest, ["first_name"], "")).strip(), str(first_value(guest, ["last_name"], "")).strip()]
        joined = " ".join(name for name in names if name)
        if joined:
            return joined

    names = [str(first_value(reservation, ["firstname"], "")).strip(), str(first_value(reservation, ["lastname"], "")).strip()]
    joined = " ".join(name for name in names if name)
    return joined or "Unknown guest"


def reservation_room(reservation: dict[str, Any]) -> str:
    rooms = first_value(reservation, ["rooms", "room_stays", "booked_rooms"], [])
    if isinstance(rooms, list) and rooms:
        names = []
        for room in rooms:
            if isinstance(room, dict):
                names.append(str(first_value(room, ["room_name", "name", "room_type_name", "number", "room_number"], "")).strip())
        names = [name for name in names if name]
        if names:
            return ", ".join(names)

    return str(first_value(reservation, ["room_name", "room_type", "room_number", "unit_name"], "Room not assigned"))


def guest_counts(reservation: dict[str, Any]) -> tuple[int, int]:
    adults = int(first_value(reservation, ["adults", "adult_count", "number_of_adults"], 0) or 0)
    children = int(first_value(reservation, ["children", "child_count", "number_of_children"], 0) or 0)

    rooms = first_value(reservation, ["rooms", "room_stays", "booked_rooms"], [])
    if (adults == 0 and children == 0) and isinstance(rooms, list):
        for room in rooms:
            if isinstance(room, dict):
                adults += int(first_value(room, ["adults", "adult_count"], 0) or 0)
                children += int(first_value(room, ["children", "child_count"], 0) or 0)

    return adults, children


def meal_plan(reservation: dict[str, Any]) -> str:
    meal = first_value(reservation, ["meal_plan", "board_type", "pension_type", "rate_plan_meal", "meal"])
    if isinstance(meal, dict):
        meal = first_value(meal, ["name", "title", "code"])
    return str(meal or "No meal plan listed").strip()


def is_active_state(value: Any) -> bool:
    return str(value or "").strip().casefold() not in {"canceled", "cancelled", "deleted", "void"}


def infer_meal_plan(reservation: dict[str, Any], room: dict[str, Any]) -> str:
    room_meal = first_value(room, ["meal_plan_presentation", "meal_plan", "board_type", "pension_type"])
    if room_meal:
        return str(room_meal).replace("-", " ").title()

    explicit = meal_plan(reservation)
    if explicit != "No meal plan listed":
        return explicit

    text = " ".join(
        str(value or "")
        for value in [
            first_value(room, ["name_presentation", "name", "room_type_name"]),
            first_value(room, ["rate_plan_name", "rate_name", "board_type", "meal_plan"]),
        ]
    )
    if re.search(r"\b(bed\s*&\s*breakfast|breakfast|bb)\b", text, re.I):
        return "Breakfast"
    if re.search(r"\b(half board|demi pension|hb)\b", text, re.I):
        return "Half board"
    if re.search(r"\b(full board|pension complete|fb)\b", text, re.I):
        return "Full board"
    return explicit


def format_money(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def format_extra(extra: dict[str, Any]) -> str:
    name = str(first_value(extra, ["name", "title", "description", "label"], "") or "").strip()
    if not name or name.casefold() in {"city tax", "tax", "vat"}:
        return ""

    quantity = first_value(extra, ["quantity", "qty", "count"])
    total = first_value(extra, ["total", "price", "amount"])
    base_price = first_value(extra, ["base_price", "unit_price"])

    parts = [name]
    if quantity not in (None, "", 0, "0"):
        parts.append(f"x{quantity}")
    if total not in (None, "", 0, "0", 0.0):
        parts.append(f"{format_money(total)}")
    elif base_price not in (None, "", 0, "0", 0.0):
        parts.append(f"{format_money(base_price)}")

    return " ".join(parts)


def extract_extras(reservation: dict[str, Any], room: dict[str, Any]) -> list[str]:
    raw_extras: list[Any] = []
    raw_extras.extend(room.get("extras") or [])
    raw_extras.extend(reservation.get("extras") or [])
    for key in ("extra_adjustments_details", "adjustment_details", "price_adjustments_details"):
        raw_extras.extend(reservation.get(key) or [])

    output: list[str] = []
    seen: set[str] = set()
    for extra in raw_extras:
        if not isinstance(extra, dict):
            continue
        formatted = format_extra(extra)
        if formatted and formatted.casefold() not in seen:
            seen.add(formatted.casefold())
            output.append(formatted)
    return output


def reservation_rooms(reservation: dict[str, Any]) -> list[dict[str, Any]]:
    rooms = first_value(reservation, ["rooms", "room_stays", "booked_rooms"], [])
    return rooms if isinstance(rooms, list) else []


def room_name(room: dict[str, Any], reservation: dict[str, Any]) -> str:
    name = str(
        first_value(
            room,
            ["name_presentation", "room_name", "name", "room_type_name", "number", "room_number"],
            reservation_room(reservation),
        )
    )
    bed_number = first_value(room, ["number", "room_number"])
    if bed_number and str(bed_number) not in name:
        return f"{name} #{bed_number}"
    return name


def room_base_name(room: dict[str, Any], reservation: dict[str, Any]) -> str:
    name = str(
        first_value(
            room,
            ["name_presentation", "room_name", "name", "room_type_name"],
            reservation_room(reservation),
        )
    )
    return clean_room_name(name)


def clean_room_name(name: str) -> str:
    name = re.sub(r"\s+-\s+Room Only Non remboursable\b", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+Room Only Non refundable\b", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+Bed\s*&\s*Breakfast\s+-\s+Non refundable\b", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+BB\s+Non refundable\b", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+Non refundable\b", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+Bed\s*&\s*Breakfast\b", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+Bed and breakfast\b", "", name, flags=re.I)
    name = re.sub(r"\s{2,}", " ", name)
    return name.strip()


def room_bed_number(room: dict[str, Any]) -> str:
    value = first_value(room, ["number", "room_number"])
    return str(value).strip() if value not in (None, "") else ""


def room_key(line: StayLine) -> str:
    if line.bed_number:
        return f"number:{line.bed_number.casefold()}"
    return f"name:{line.room_name.casefold()}"


def block_room_key(room: str) -> str:
    room = room.strip()
    if room.casefold() in {"whole house", "entire house", "all", "all rooms"}:
        return "whole-house"
    number_match = re.search(r"#?\b(\d{2,5})\b", room)
    if number_match:
        return f"number:{number_match.group(1).casefold()}"
    return f"name:{clean_room_name(room).casefold()}"


def room_dates(room: dict[str, Any], reservation: dict[str, Any]) -> tuple[dt.date | None, dt.date | None]:
    arrival = parse_date(first_value(room, ["checkin_date", "check_in", "arrival_date", "start_date"]))
    departure = parse_date(first_value(room, ["checkout_date", "check_out", "departure_date", "end_date"]))
    if arrival and departure:
        return arrival, departure

    return (
        parse_date(first_value(reservation, ["checkin_date", "check_in", "arrival_date", "start_date"])),
        parse_date(first_value(reservation, ["checkout_date", "check_out", "departure_date", "end_date"])),
    )


def room_guest_counts(room: dict[str, Any], reservation: dict[str, Any]) -> tuple[int, int]:
    adult_value = first_value(room, ["total_adult", "adults", "adult_count", "number_of_adults"])
    child_ages = first_value(room, ["child_ages"], [])
    child_value = first_value(room, ["total_child", "children", "child_count", "number_of_children"])

    adults = int(adult_value or 0)
    if isinstance(child_ages, list):
        children = len(child_ages)
    else:
        children = int(child_value or 0)

    if adults == 0 and children == 0:
        return guest_counts(reservation)
    return adults, children


def fetch_reservations(
    token: str,
    hr_id: str,
    *,
    from_last_update_date: dt.date | None = None,
    lookback_days: int | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_delay: float = 0.25,
    retries: int = 3,
    retry_wait: float = 30.0,
    fetch_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    reservations: list[dict[str, Any]] = []
    pages_read = 0
    stopped_reason = "max_pages"
    last_page_count = 0

    for page in range(1, max_pages + 1):
        query_params = {
            "token": token,
            "hr_id": hr_id,
            "page": page,
            "per_page": 100,
            "undelivered": "false",
        }
        if from_last_update_date:
            query_params["from_last_update_date"] = from_last_update_date.isoformat()
        elif lookback_days is not None:
            query_params["from_date"] = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
        else:
            query_params["from_last_update_date"] = (dt.date.today() - dt.timedelta(days=DEFAULT_UPDATE_LOOKBACK_DAYS)).isoformat()

        params = urllib.parse.urlencode(query_params)
        url = f"{API_BASE_URL}/reservations?{params}"
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "Accept": "application/json"})

        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt >= retries:
                    if exc.code == 429:
                        raise SystemExit(
                            "HotelRunner is rate-limiting requests right now. Try again in a few minutes, "
                            "or run with a smaller range like --lookback-days 90."
                        ) from exc
                    raise
                wait_seconds = retry_wait * (attempt + 1)
                print(f"HotelRunner rate limit on page {page}; waiting {wait_seconds:.0f} seconds before retry...")
                time.sleep(wait_seconds)

        page_reservations = payload.get("reservations", [])
        pages_read = page
        last_page_count = len(page_reservations)
        if not page_reservations:
            stopped_reason = "empty_page"
            break

        reservations.extend(page_reservations)

        total_pages = payload.get("total_pages") or payload.get("pages")
        if total_pages and page >= int(total_pages):
            stopped_reason = "api_total_pages"
            break
        if page_delay:
            time.sleep(page_delay)

    if fetch_meta is not None:
        fetch_meta.update(
            {
                "pages_read": pages_read,
                "last_page_count": last_page_count,
                "max_pages": max_pages,
                "max_pages_hit": pages_read >= max_pages and last_page_count > 0 and stopped_reason == "max_pages",
                "stopped_reason": stopped_reason,
                "records_returned": len(reservations),
            }
        )

    return reservations


def reservation_cache_key(reservation: dict[str, Any]) -> str:
    value = first_value(reservation, ["reservation_id", "hr_number", "provider_number"])
    return str(value or "").strip()


def load_reservation_cache(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read reservation cache {path}: {exc}") from exc

    if isinstance(payload, dict):
        records = payload.get("reservations", [])
    else:
        records = payload
    if not isinstance(records, list):
        raise SystemExit(f"Reservation cache {path} is not a valid reservation list.")
    return [item for item in records if isinstance(item, dict)]


def save_reservation_cache(path: Path, reservations: list[dict[str, Any]]) -> None:
    payload = {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "reservation_count": len(reservations),
        "reservations": reservations,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def merge_reservation_cache(existing: list[dict[str, Any]], updates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    by_id: dict[str, dict[str, Any]] = {}
    stats = {"loaded": len(existing), "updates": len(updates), "added": 0, "updated": 0, "removed": 0, "skipped": 0}

    for reservation in existing:
        key = reservation_cache_key(reservation)
        if key:
            by_id[key] = reservation
        else:
            stats["skipped"] += 1

    for reservation in updates:
        key = reservation_cache_key(reservation)
        if not key:
            stats["skipped"] += 1
            continue

        if not is_active_state(first_value(reservation, ["state"])):
            if key in by_id:
                del by_id[key]
                stats["removed"] += 1
            continue

        if key in by_id:
            stats["updated"] += 1
        else:
            stats["added"] += 1
        by_id[key] = reservation

    merged = sorted(by_id.values(), key=lambda item: str(first_value(item, ["checkin_date", "updated_at", "reservation_id"], "")))
    return merged, stats


def write_last_fetch_debug(path: Path, *, mode: str, stats: dict[str, int], fetched: list[dict[str, Any]], max_pages: int, lookback_days: int | None, fetch_meta: dict[str, Any] | None = None) -> None:
    """Save a compact fetch summary to make missing-reservation debugging quick."""
    sample = []
    for reservation in fetched[:25]:
        sample.append(
            {
                "reservation_id": first_value(reservation, ["reservation_id", "hr_number", "provider_number"]),
                "guest": reservation_name(reservation),
                "state": first_value(reservation, ["state"]),
                "checkin": first_value(reservation, ["checkin_date", "check_in", "arrival_date", "start_date"]),
                "checkout": first_value(reservation, ["checkout_date", "check_out", "departure_date", "end_date"]),
                "updated_at": first_value(reservation, ["updated_at", "last_update_date", "modified_at"]),
                "created_at": first_value(reservation, ["created_at", "reservation_date"]),
            }
        )

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "max_pages": max_pages,
        "lookback_days": lookback_days,
        "stats": stats,
        "fetch_meta": fetch_meta or {},
        "first_fetched_reservations": sample,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def build_run_status(cache_stats: dict[str, int], fetch_meta: dict[str, Any], *, mode: str) -> RunStatus:
    details = [
        f"{cache_stats.get('loaded', 0)} cached reservations loaded",
        f"{cache_stats.get('updates', 0)} HotelRunner updates read",
        f"{cache_stats.get('added', 0)} new, {cache_stats.get('updated', 0)} changed, {cache_stats.get('removed', 0)} canceled/removed",
        f"{fetch_meta.get('pages_read', 0)} page(s) read",
    ]

    if fetch_meta.get("max_pages_hit"):
        return RunStatus(
            safe_to_send=False,
            title="Needs attention before sending",
            message=(
                "HotelRunner still had data when the script reached the page limit. "
                "The dashboard may be missing new or changed reservations."
            ),
            details=tuple(details + ["Run again with a higher --max-pages value before sending the WhatsApp message."]),
        )

    if mode == "recent_updates" and cache_stats.get("updates", 0) == 0:
        return RunStatus(
            safe_to_send=False,
            title="Needs attention before sending",
            message="HotelRunner returned zero recent updates. This can be okay, but it is unusual enough to double-check before sending.",
            details=tuple(details),
        )

    return RunStatus(
        safe_to_send=True,
        title="Ready to review",
        message="HotelRunner data was read and merged into the local cache. Review attention items, then copy the team message.",
        details=tuple(details),
    )


def load_update_and_save_cache(
    *,
    cache_path: Path,
    token: str,
    hr_id: str,
    update_lookback_days: int,
    max_pages: int,
    page_delay: float,
    retries: int,
    retry_wait: float,
    fetch_meta: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    cached = load_reservation_cache(cache_path)
    update_start = dt.date.today() - dt.timedelta(days=update_lookback_days)
    updates = fetch_reservations(
        token=token,
        hr_id=hr_id,
        from_last_update_date=update_start,
        max_pages=max_pages,
        page_delay=page_delay,
        retries=retries,
        retry_wait=retry_wait,
        fetch_meta=fetch_meta,
    )
    merged, stats = merge_reservation_cache(cached, updates)
    save_reservation_cache(cache_path, merged)
    latest_updates_path = cache_path.parent / "hotelrunner_latest_updates.json"
    latest_updates_path.write_text(json.dumps(updates, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged, stats, updates



def safe_preview(value: Any) -> Any:
    """Return a short, private-friendly preview of reservation structure."""
    if isinstance(value, dict):
        return {key: safe_preview(item) for key, item in list(value.items())[:30]}
    if isinstance(value, list):
        return [safe_preview(item) for item in value[:2]]
    if isinstance(value, str):
        if len(value) > 80:
            return value[:77] + "..."
        return value
    return value


def write_debug_sample(reservations: list[dict[str, Any]], output_path: Path) -> None:
    sample = {
        "reservation_count": len(reservations),
        "first_reservation_keys": sorted(reservations[0].keys()) if reservations else [],
        "first_reservation_preview": safe_preview(reservations[0]) if reservations else None,
    }
    output_path.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")


def private_guest_preview(reservation: dict[str, Any]) -> dict[str, Any]:
    """Keep enough structure for mapping while trimming long private/noisy values."""
    keep_keys = [
        "reservation_id",
        "hr_number",
        "provider_number",
        "channel_display",
        "source_display",
        "state",
        "guest",
        "firstname",
        "lastname",
        "total_guests",
        "total_rooms",
        "checkin_date",
        "checkout_date",
        "note",
        "system_message",
        "rooms",
        "meta_data_conversions",
        "integration_echo_details",
        "channel_profile_data_conversions",
    ]
    preview = {key: safe_preview(reservation.get(key)) for key in keep_keys if key in reservation}
    preview["clean_notes"] = extract_notes(reservation)
    preview["detected_bed_request"] = detect_bed_request(extract_notes(reservation), reservation)
    return preview


def write_guest_debug(reservations: list[dict[str, Any]], search: str, output_path: Path) -> int:
    needle = search.casefold()
    matches = [
        reservation
        for reservation in reservations
        if needle in reservation_name(reservation).casefold()
        or needle in str(first_value(reservation, ["hr_number", "provider_number", "reservation_id"], "")).casefold()
    ]

    payload = {
        "search": search,
        "matches": len(matches),
        "reservations": [private_guest_preview(reservation) for reservation in matches[:10]],
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(matches)


def write_room_debug(lines: list[StayLine], search: str, output_path: Path, metadata: dict[str, Any] | None = None) -> int:
    needle = search.casefold().lstrip("#")
    matches = [
        line
        for line in lines
        if needle in line.room_name.casefold()
        or needle == line.bed_number.casefold()
        or needle in display_room(line).casefold()
    ]

    payload = {
        "search": search,
        "metadata": metadata or {},
        "matches": len(matches),
        "reservations": [
            {
                "guest": line.guest_name,
                "room": display_room(line),
                "room_name": line.room_name,
                "bed_or_room_number": line.bed_number,
                "checkin_date": line.arrival.isoformat(),
                "checkout_date": line.departure.isoformat(),
                "guests": line.guests,
                "adults": line.adults,
                "children": line.children,
                "meal_plan": line.meal_plan,
                "channel": line.channel,
                "hr_number": line.hr_number,
                "reservation_id": line.reservation_id,
                "notes": list(line.notes),
                "extras": list(line.extras),
                "bed_request": line.bed_request,
            }
            for line in sorted(matches, key=lambda item: (item.arrival, item.departure, item.guest_name))
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(matches)


def load_room_blocks(path: Path) -> list[RoomBlock]:
    if not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw_blocks = raw.get("blocks", [])
    elif isinstance(raw, list):
        raw_blocks = raw
    else:
        raw_blocks = []

    blocks: list[RoomBlock] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        start = parse_date(first_value(item, ["start", "from", "checkin_date"]))
        end = parse_date(first_value(item, ["end", "to", "checkout_date"]))
        room = str(first_value(item, ["room", "room_name", "scope"], "whole house") or "whole house")
        if not start or not end:
            continue
        blocks.append(
            RoomBlock(
                label=str(first_value(item, ["label", "name", "reason"], "Manual block")),
                room=room,
                start=start,
                end=end,
                guests=int(first_value(item, ["guests", "people", "count"], 0) or 0),
                note=str(first_value(item, ["note", "notes"], "") or ""),
            )
        )
    return blocks


def active_stay_lines(reservations: list[dict[str, Any]]) -> list[StayLine]:
    lines: list[StayLine] = []

    for reservation in reservations:
        if not is_active_state(first_value(reservation, ["state"])):
            continue

        name = reservation_name(reservation)
        channel = str(first_value(reservation, ["channel_display", "source_display"], "") or "")
        reservation_id = str(first_value(reservation, ["reservation_id"], "") or "")
        hr_number = str(first_value(reservation, ["hr_number", "provider_number"], "") or "")
        notes = tuple(extract_notes(reservation))
        bed = detect_bed_request(list(notes), reservation)
        try:
            total_amount = float(reservation.get("total") or 0)
        except (TypeError, ValueError):
            total_amount = 0.0
        try:
            paid_amount = float(reservation.get("paid_amount") or 0)
        except (TypeError, ValueError):
            paid_amount = 0.0
        payments = reservation.get("payments")
        payment_record_count = len([
            payment for payment in payments
            if not isinstance(payment, dict)
            or str(payment.get("state") or "").casefold() not in {"failed", "cancelled", "canceled"}
        ]) if isinstance(payments, list) else 0
        booking_date = parse_date(str(reservation.get("completed_at") or "")[:10])
        rooms = reservation_rooms(reservation) or [{}]

        for room_data in rooms:
            if not isinstance(room_data, dict):
                continue
            if not is_active_state(first_value(room_data, ["state"], first_value(reservation, ["state"]))):
                continue

            arrival, departure = room_dates(room_data, reservation)
            if not arrival or not departure:
                continue

            adults, children = room_guest_counts(room_data, reservation)
            lines.append(
                StayLine(
                    reservation_id=reservation_id,
                    hr_number=hr_number,
                    guest_name=name,
                    channel=channel,
                    room_name=room_base_name(room_data, reservation),
                    bed_number=room_bed_number(room_data),
                    arrival=arrival,
                    departure=departure,
                    adults=adults,
                    children=children,
                    meal_plan=infer_meal_plan(reservation, room_data),
                    notes=notes,
                    extras=tuple(extract_extras(reservation, room_data)),
                    bed_request=bed,
                    total_amount=total_amount,
                    paid_amount=paid_amount,
                    payment_record_count=payment_record_count,
                    booking_date=booking_date,
                )
            )

    return lines


def display_room(line: StayLine) -> str:
    if line.bed_number:
        return f"{line.room_name} #{line.bed_number}"
    return line.room_name


def meal_servings(line: StayLine) -> int:
    return line.guests


def build_day_summaries(lines: list[StayLine], start: dt.date, days_ahead: int, blocks: list[RoomBlock] | None = None) -> list[DaySummary]:
    summaries: list[DaySummary] = []
    blocks = blocks or []

    for offset in range(days_ahead + 1):
        day = start + dt.timedelta(days=offset)
        summary = DaySummary(date=day)
        guest_day_counts: Counter[str] = Counter()
        occupied_rooms: dict[str, list[StayLine]] = {}

        for line in lines:
            if line.arrival == day:
                summary.arrivals.append(line)
            if line.departure == day:
                summary.departures.append(line)
            if line.arrival <= day < line.departure:
                summary.in_house.append(line)
                summary.meals[line.meal_plan] += meal_servings(line)
                guest_day_counts[line.guest_name.casefold()] += 1
                occupied_rooms.setdefault(room_key(line), []).append(line)

                if line.bed_request and line.arrival == day:
                    summary.bed_requests.append(f"{line.guest_name} | {display_room(line)}: {line.bed_request}")
                # Extras and notes are only shown on the arrival day to avoid
                # repeating the same information for every in-house day.
                if line.arrival == day:
                    for extra in line.extras:
                        summary.extras.append(f"{line.guest_name} | {display_room(line)}: {extra}")
                    for note in line.notes:
                        summary.notes.append(f"{line.guest_name} | {display_room(line)}: {note}")

        for guest_key, count in guest_day_counts.items():
            if count > 1:
                names = [line.guest_name for line in summary.in_house if line.guest_name.casefold() == guest_key]
                rooms = ", ".join(display_room(line) for line in summary.in_house if line.guest_name.casefold() == guest_key)
                summary.possible_duplicates.append(f"{names[0]} appears {count} times: {rooms}")

        for same_room_lines in occupied_rooms.values():
            guest_names = {line.guest_name.casefold() for line in same_room_lines}
            if len(same_room_lines) > 1 and len(guest_names) > 1:
                room = display_room(same_room_lines[0])
                guests = "; ".join(f"{line.guest_name} ({line.guests} guest(s), {line.hr_number})" for line in same_room_lines)
                summary.room_conflicts.append(f"{room} has multiple active reservations: {guests}")

        for block in blocks:
            if block.start <= day < block.end:
                block_line = f"{block.label} | {block.room}"
                if block.guests:
                    block_line += f" | {block.guests} expected guest(s)"
                if block.note:
                    block_line += f" | {block.note}"
                summary.blocks.append(block_line)

                key = block_room_key(block.room)
                matching_lines = summary.in_house if key == "whole-house" else occupied_rooms.get(key, [])
                if matching_lines:
                    guests = "; ".join(f"{line.guest_name} in {display_room(line)}" for line in matching_lines)
                    summary.block_conflicts.append(f"{block.label} block overlaps reservation(s): {guests}")

        summaries.append(summary)

    return summaries


def format_line(line: StayLine) -> str:
    channel = f" | {line.channel}" if line.channel else ""
    return f"{line.guest_name} | {display_room(line)} | {line.adults} adults, {line.children} children | {line.meal_plan}{channel}"


def build_report(summaries: list[DaySummary], run_status: RunStatus | None = None) -> str:
    start = summaries[0].date
    end = summaries[-1].date
    lines = [
        "# HotelRunner Day-by-Day In-House Summary",
        "",
        f"Period: {start.isoformat()} to {end.isoformat()}",
        "",
    ]
    if run_status:
        lines.extend(
            [
                f"Status: {run_status.title}",
                run_status.message,
                *(f"- {item}" for item in run_status.details),
                "",
            ]
        )

    for summary in summaries:
        lines.extend(
            [
                f"## {summary.date.strftime('%A %d %B %Y')}",
                "",
                f"In-house guests: {summary.guests} total ({summary.adults} adults, {summary.children} children)",
                f"Occupied bed/room lines: {len(summary.in_house)}",
                f"Arrivals: {len(summary.arrivals)} | Departures: {len(summary.departures)}",
                "",
                "### Check-ins",
                *(f"- {format_line(item)}" for item in summary.arrivals),
                *([] if summary.arrivals else ["- None"]),
                "",
                "### Check-outs",
                *(f"- {format_line(item)}" for item in summary.departures),
                *([] if summary.departures else ["- None"]),
                "",
                "### Meal Plans",
                *(f"- {meal}: {count}" for meal, count in sorted(summary.meals.items())),
                *([] if summary.meals else ["- None"]),
                "",
                "### Possible Duplicates / Multi-Bed Reservations",
                *(f"- {item}" for item in summary.possible_duplicates),
                *([] if summary.possible_duplicates else ["- None"]),
                "",
                "### Room Conflicts",
                *(f"- {item}" for item in summary.room_conflicts),
                *([] if summary.room_conflicts else ["- None"]),
                "",
                "### Manual Blocks",
                *(f"- {item}" for item in summary.blocks),
                *([] if summary.blocks else ["- None"]),
                "",
                "### Block Conflicts",
                *(f"- {item}" for item in summary.block_conflicts),
                *([] if summary.block_conflicts else ["- None"]),
                "",
                "### Bed Requests",
                *(f"- {item}" for item in summary.bed_requests),
                *([] if summary.bed_requests else ["- None found in notes/messages"]),
                "",
                "### Extras / Additional Fees",
                *(f"- {item}" for item in summary.extras),
                *([] if summary.extras else ["- None"]),
                "",
                "### Notes & Messages",
                *(f"- {item}" for item in summary.notes),
                *([] if summary.notes else ["- None"]),
                "",
            ]
        )

    return "\n".join(lines)


def room_disposition(summary: DaySummary) -> Counter[str]:
    rooms: Counter[str] = Counter()
    for line in summary.in_house:
        rooms[line.room_name] += line.guests
    return rooms


def short_guest_line(line: StayLine) -> str:
    extras = f" - extras: {', '.join(line.extras)}" if line.extras else ""
    return f"{line.guest_name} - {display_room(line)} - {line.guests} guest(s) - {line.meal_plan}{extras}"


def normalized_from_stayline_with_extras(line: StayLine):
    from data_model import stayline_to_normalized
    from extras_engine import classify_raw_extra

    res = stayline_to_normalized(line)
    res.total_amount = line.total_amount
    res.paid_amount = line.paid_amount
    res.payment_record_count = line.payment_record_count
    res.booking_date = line.booking_date
    classified = []
    for label in line.extras:
        extra = classify_raw_extra({"name": label})
        if extra:
            classified.append(extra)
    res.extras = classified
    return res


def team_house(line: StayLine) -> str:
    text = f"{line.room_name} {line.bed_number} {line.channel}".casefold()
    if any(prefix in text for prefix in ["9200", "9300", "9400", "9500", "9600"]):
        return "Sunrise"
    if any(word in text for word in ["tidehunter", "bay", "slab", "cathedral", "reef"]):
        return "Tide"
    return "Olas"


def team_room_label(line: StayLine) -> str:
    text = display_room(line).casefold()
    if "bay" in text or line.bed_number == "6500":
        return "bay"
    if "slab" in text or line.bed_number == "6700":
        return "slab"
    if "cathedral" in text or line.bed_number == "6600":
        return "cathedral"
    if "reef" in text or line.bed_number == "6800":
        return "reef"
    if "dorm" in text:
        return "dorm"
    if line.bed_number == "110":
        return "RDC1"
    if line.bed_number == "112":
        return "RDC2"
    if line.bed_number == "101":
        return "RDC3"
    if line.bed_number == "113" or "twin room" in text:
        return "balcony"
    return display_room(line)


def team_extra_note(line: StayLine, movement: str = "") -> str:
    parts: list[str] = []
    meal = line.meal_plan.casefold()
    if "all inclusive" in meal:
        parts.append("all meals")
    elif "half board" in meal:
        parts.append("half board")
    elif "full board" in meal:
        parts.append("full board")
    elif movement == "arrival" and "bed and breakfast" in meal:
        parts.append("breakfast")
    elif "room only" in meal:
        parts.append("room only")

    if line.bed_request:
        parts.append(line.bed_request)

    for extra in line.extras:
        if extra.casefold() not in {part.casefold() for part in parts}:
            parts.append(extra)

    note_text = " ".join(line.notes)
    arrival_match = re.search(r"arrival(?: time)?\D{0,25}(\d{1,2}[:h]\d{2}|\d{1,2}:00|\d{1,2}h)", note_text, re.I)
    if arrival_match:
        parts.append(f"arrive at {arrival_match.group(1).replace('h', ':')}")
    elif "between" in note_text.casefold() and "arrival" in note_text.casefold():
        between_match = re.search(r"between\s+([0-9:]+)\s+and\s+([0-9:]+)", note_text, re.I)
        if between_match:
            parts.append(f"arrive {between_match.group(1)}-{between_match.group(2)}")

    due_match = re.search(r"\bdue:?\s*([0-9]+(?:[.,][0-9]+)?)", note_text, re.I)
    paid_match = re.search(r"\bpaid:?\s*([0-9]+(?:[.,][0-9]+)?)", note_text, re.I)
    if due_match:
        due_amount = due_match.group(1).replace(",", ".")
        if due_amount not in {"0", "0.0", "0.00"}:
            parts.append(f"to pay {due_amount} dhs")
    elif paid_match:
        paid_amount = paid_match.group(1).replace(",", ".")
        if paid_amount not in {"0", "0.0", "0.00"}:
            parts.append("paid")

    return f" ({', '.join(parts)})" if parts else ""


def team_line(line: StayLine, movement: str = "") -> str:
    return f"{line.guest_name} x{line.guests} -> {team_room_label(line)}{team_extra_note(line, movement)}"


HOUSE_EMOJI = {"Olas": "🏠", "Tide": "🌊", "Sunrise": "🌅"}


def grouped_team_lines(lines: list[StayLine], movement: str = "") -> list[str]:
    output: list[str] = []
    for house in ("Olas", "Tide", "Sunrise"):
        house_lines = [line for line in lines if team_house(line) == house]
        if not house_lines:
            continue
        emoji = HOUSE_EMOJI.get(house, "")
        output.extend(["", f"{emoji} {house}"])
        output.extend(team_line(line, movement) for line in house_lines)
    return output


def _sheet_guest_matches_hr(sheet_res: Any, hr_lines: list[StayLine], movement: str, date: dt.date) -> bool:
    sheet_name = _normalize_name_key(getattr(sheet_res, "guest_name", ""))
    if not sheet_name:
        return False

    for line in hr_lines:
        if team_house(line) != "Sunrise":
            continue
        if _normalize_name_key(line.guest_name) != sheet_name:
            continue
        if movement == "arrival" and line.arrival == date:
            return True
        if movement == "departure" and line.departure == date:
            return True
    return False


def _sunrise_sheet_line(res: Any) -> str:
    guest_count = getattr(res, "guest_count", 0) or 1
    room = getattr(res, "room", "") or "Sunrise"
    details = [
        getattr(res, "surf_level", "") or "",
        getattr(res, "meal_plan", "") or "",
        "⚠️ check/add HR",
    ]
    suffix = " · ".join(part for part in details if part)
    return f"{res.guest_name} x{guest_count} -> {room} · {suffix}"


def grouped_team_lines_with_sunrise_sheet(
    lines: list[StayLine],
    sheet_reservations: list[Any],
    date: dt.date,
    movement: str,
) -> list[str]:
    output: list[str] = []
    for house in ("Olas", "Tide", "Sunrise"):
        house_lines = [line for line in lines if team_house(line) == house]
        sheet_lines: list[str] = []
        if house == "Sunrise":
            for res in sheet_reservations:
                if getattr(res, "house", "") != "Sunrise":
                    continue
                is_movement = (
                    getattr(res, "is_arriving_on")(date)
                    if movement == "arrival"
                    else getattr(res, "is_departing_on")(date)
                )
                if not is_movement:
                    continue
                if _sheet_guest_matches_hr(res, lines, movement, date):
                    continue
                sheet_lines.append(_sunrise_sheet_line(res))
        if not house_lines and not sheet_lines:
            continue
        emoji = HOUSE_EMOJI.get(house, "")
        output.extend(["", f"{emoji} {house}"])
        output.extend(team_line(line, movement) for line in house_lines)
        output.extend(sheet_lines)
    return output


def sunrise_sheet_reminder_lines(
    lines: list[StayLine],
    sheet_reservations: list[Any],
    date: dt.date,
    movement: str,
) -> list[str]:
    """Google Sheet-only Sunrise lines for visible dashboard cards."""
    reminders: list[str] = []
    for res in sheet_reservations:
        if getattr(res, "house", "") != "Sunrise":
            continue
        is_movement = (
            getattr(res, "is_arriving_on")(date)
            if movement == "arrival"
            else getattr(res, "is_departing_on")(date)
        )
        if not is_movement:
            continue
        if _sheet_guest_matches_hr(res, lines, movement, date):
            continue
        reminders.append(_sunrise_sheet_line(res))
    return reminders


def sunrise_sheet_active_reservations(
    hr_lines: list[StayLine],
    sheet_reservations: list[Any],
    date: dt.date,
) -> list[Any]:
    """Active Google Sheet-only Sunrise reservations for meal/in-house views."""
    active: list[Any] = []
    for res in sheet_reservations:
        if getattr(res, "house", "") != "Sunrise":
            continue
        if not getattr(res, "is_active_on")(date):
            continue
        if _sheet_guest_matches_hr(res, hr_lines, "arrival", getattr(res, "arrival_date", date) or date):
            continue
        active.append(res)
    return active


def sunrise_sheet_guest_line(res: Any) -> str:
    details = [
        getattr(res, "room", "") or "Sunrise",
        getattr(res, "surf_level", "") or "",
        getattr(res, "meal_plan", "") or "",
        "Sheet",
        "⚠️ check/add HR",
    ]
    return f"{res.guest_name} - " + " - ".join(part for part in details if part)


def _normalize_name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def detect_extensions(departures: list[StayLine], arrivals: list[StayLine]) -> tuple[list[StayLine], list[StayLine], list[str]]:
    """
    If a guest appears in departures AND arrivals on the same day, they extended their stay.
    Removes them from departures and arrivals and generates clean extension notes.
    """
    dep_by_name: dict[str, list[StayLine]] = {}
    for line in departures:
        dep_by_name.setdefault(_normalize_name_key(line.guest_name), []).append(line)

    matched_dep_idx: set[int] = set()
    matched_arr_idx: set[int] = set()
    extension_notes: list[str] = []

    for a_idx, arr_line in enumerate(arrivals):
        key = _normalize_name_key(arr_line.guest_name)
        if key in dep_by_name and dep_by_name[key]:
            dep_line = dep_by_name[key].pop(0)
            # Find index of dep_line in departures
            for d_idx, d in enumerate(departures):
                if d is dep_line and d_idx not in matched_dep_idx:
                    matched_dep_idx.add(d_idx)
                    break
            matched_arr_idx.add(a_idx)

            nights = (arr_line.departure - arr_line.arrival).days
            nights_str = "one night" if nights == 1 else f"{nights} nights"

            old_room = team_room_label(dep_line)
            new_room = team_room_label(arr_line)

            if old_room == new_room:
                note = f"{arr_line.guest_name} x{arr_line.guests} -> {new_room} extended {nights_str}"
            else:
                note = f"{arr_line.guest_name} x{arr_line.guests} -> moved from {old_room} to {new_room} (extended {nights_str})"

            extension_notes.append(note)

    clean_deps = [line for idx, line in enumerate(departures) if idx not in matched_dep_idx]
    clean_arrs = [line for idx, line in enumerate(arrivals) if idx not in matched_arr_idx]
    return clean_deps, clean_arrs, extension_notes


def build_whatsapp_block(summary: DaySummary) -> str:
    """Check-outs and check-ins only — the part you copy to WhatsApp."""
    date_str = summary.date.strftime("%d %B")
    clean_deps, clean_arrs, extensions = detect_extensions(summary.departures, summary.arrivals)

    lines = [f"🏁 CHECK-OUTS · {date_str}"]
    lines.extend(grouped_team_lines(clean_deps, "departure") or ["", "No check-outs"])

    if extensions:
        lines.extend(["", "note:"])
        lines.extend(extensions)

    lines.extend(["", "————", "", f"🏨 CHECK-INS · {date_str}"])
    lines.extend(grouped_team_lines(clean_arrs, "arrival") or ["", "No check-ins"])
    return "\n".join(lines)


def build_whatsapp_block_with_sunrise_sheet(summary: DaySummary, sheet_reservations: list[Any]) -> str:
    """Team report enriched with Google Sheet-only Sunrise reminders."""
    date_str = summary.date.strftime("%d %B")
    clean_deps, clean_arrs, extensions = detect_extensions(summary.departures, summary.arrivals)

    dep_lines = grouped_team_lines_with_sunrise_sheet(clean_deps, sheet_reservations, summary.date, "departure")
    arr_lines = grouped_team_lines_with_sunrise_sheet(clean_arrs, sheet_reservations, summary.date, "arrival")

    lines = [f"🏁 CHECK-OUTS · {date_str}"]
    lines.extend(dep_lines or ["", "No check-outs"])

    if extensions:
        lines.extend(["", "note:"])
        lines.extend(extensions)

    lines.extend(["", "————", "", f"🏨 CHECK-INS · {date_str}"])
    lines.extend(arr_lines or ["", "No check-ins"])
    return "\n".join(lines)



def build_manager_block(summary: DaySummary) -> str:
    """Summary + attention + notes — your private manager view."""
    lines = ["📊 SUMMARY"]
    lines.append(f"👥 In-house: {summary.guests} guest(s)")
    if summary.meals:
        lines.append("🍽️  Meals: " + ", ".join(f"{meal}: {count}" for meal, count in sorted(summary.meals.items())))

    attention = summary.block_conflicts + summary.room_conflicts + summary.possible_duplicates + summary.bed_requests
    if attention:
        lines.extend(["", "⚠️  ATTENTION:"])
        lines.extend(f"• {item}" for item in attention)

    if summary.notes:
        lines.extend(["", "📝 NOTES:"])
        lines.extend(f"• {item}" for item in summary.notes)

    return "\n".join(lines)


def build_team_message(summary: DaySummary) -> str:
    """Full combined message — used by the HTML dashboard textarea."""
    return build_whatsapp_block(summary) + "\n\n————\n\n" + build_manager_block(summary)



def render_list(items: list[str], css_class: str = "") -> str:
    if not items:
        return '<p class="muted">None</p>'
    class_attr = f' class="{css_class}"' if css_class else ""
    return "<ul{}>{}</ul>".format(class_attr, "".join(f"<li>{html.escape(item)}</li>" for item in items))


def collect_audit_results(summaries: list[DaySummary]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for summary in summaries:
        day = summary.date.strftime("%a %d %b")
        for item in summary.block_conflicts:
            results.append({"type": "block", "label": "Block conflict", "day": day, "text": item})
        for item in summary.room_conflicts:
            results.append({"type": "room", "label": "Room conflict", "day": day, "text": item})
        for item in summary.possible_duplicates:
            results.append({"type": "duplicate", "label": "Duplicate / multi-bed", "day": day, "text": item})
        for item in summary.bed_requests:
            results.append({"type": "bed", "label": "Bed request", "day": day, "text": item})
        for item in summary.extras:
            results.append({"type": "extra", "label": "Extra / fee", "day": day, "text": item})
        for item in summary.notes:
            results.append({"type": "note", "label": "Note", "day": day, "text": item})
    return results


def render_audit_results(results: list[dict[str, str]]) -> str:
    if not results:
        return '<p class="muted">No audit items found for this period.</p>'
    rows = []
    for item in results:
        rows.append(
            f"""
            <article class="audit-item" data-audit-type="{html.escape(item['type'])}">
              <div>
                <span class="audit-label">{html.escape(item['label'])}</span>
                <strong>{html.escape(item['day'])}</strong>
              </div>
              <p>{html.escape(item['text'])}</p>
            </article>
            """
        )
    return "".join(rows)


def build_dashboard_html(summaries: list[DaySummary], generated_at: dt.datetime, run_status: RunStatus | None = None) -> str:
    day_buttons = []
    day_sections = []
    audit_results = collect_audit_results(summaries)
    audit_counts = Counter(item["type"] for item in audit_results)

    for index, summary in enumerate(summaries):
        day_id = f"day-{index}"
        active = " active" if index == 0 else ""
        attention_count = (
            len(summary.bed_requests)
            + len(summary.possible_duplicates)
            + len(summary.room_conflicts)
            + len(summary.block_conflicts)
        )
        day_buttons.append(
            f"""
            <button class="day-tab{active}" data-target="{day_id}">
              <span>{html.escape(summary.date.strftime('%a %d %b'))}</span>
              <strong>{summary.guests}</strong>
              <small>{len(summary.arrivals)} in / {len(summary.departures)} out</small>
            </button>
            """
        )

        meal_items = [f"{meal}: {count}" for meal, count in sorted(summary.meals.items())]
        room_items = [f"{room}: {count} guest(s)" for room, count in sorted(room_disposition(summary).items())]
        arrival_items = [short_guest_line(line) for line in summary.arrivals]
        departure_items = [short_guest_line(line) for line in summary.departures]
        in_house_items = [short_guest_line(line) for line in summary.in_house]
        attention_items = summary.block_conflicts + summary.room_conflicts + summary.possible_duplicates + summary.bed_requests
        block_items = summary.blocks
        team_message = build_team_message(summary)

        day_sections.append(
            f"""
            <section id="{day_id}" class="day-panel{active}">
              <div class="panel-header">
                <div>
                  <p class="eyebrow">{html.escape(summary.date.strftime('%A'))}</p>
                  <h2>{html.escape(summary.date.strftime('%d %B %Y'))}</h2>
                </div>
                <div class="attention-pill">{attention_count} attention item(s)</div>
              </div>

              <div class="metrics">
                <div class="metric primary"><span>In-house</span><strong>{summary.guests}</strong><small>{summary.adults} adults, {summary.children} children</small></div>
                <div class="metric"><span>Arrivals</span><strong>{len(summary.arrivals)}</strong><small>check-ins</small></div>
                <div class="metric"><span>Departures</span><strong>{len(summary.departures)}</strong><small>check-outs</small></div>
                <div class="metric"><span>Occupied lines</span><strong>{len(summary.in_house)}</strong><small>rooms/beds</small></div>
              </div>

              <div class="split">
                <section class="block">
                  <h3>Meals</h3>
                  {render_list(meal_items)}
                </section>
                <section class="block">
                  <h3>Attention</h3>
                  {render_list(attention_items, "attention-list")}
                </section>
              </div>

              <section class="block team-message-block">
                <div class="block-heading">
                  <h3>Team Message</h3>
                  <button class="copy-team-message" type="button">Copy</button>
                </div>
                <textarea class="team-message" rows="12">{html.escape(team_message)}</textarea>
              </section>

              <section class="block">
                <h3>Manual Blocks</h3>
                {render_list(block_items)}
              </section>

              <div class="columns">
                <section class="block">
                  <h3>Arrivals</h3>
                  {render_list(arrival_items)}
                </section>
                <section class="block">
                  <h3>Departures</h3>
                  {render_list(departure_items)}
                </section>
              </div>

              <details class="block">
                <summary>In-house guests and beds</summary>
                {render_list(in_house_items)}
              </details>

              <details class="block">
                <summary>Room / bed disposition</summary>
                {render_list(room_items)}
              </details>

              <details class="block">
                <summary>Notes and messages</summary>
                {render_list(summary.notes)}
              </details>
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HotelRunner Daily Dashboard</title>
  <style>
    :root {{
      --bg: #f6f7f4;
      --surface: #ffffff;
      --surface-soft: #eef3ec;
      --text: #1f2a24;
      --muted: #66726b;
      --line: #d9dfd8;
      --accent: #24745a;
      --accent-soft: #dfeee7;
      --warning: #8a5a12;
      --warning-soft: #fbefd8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.4;
    }}
    header {{
      padding: 22px 24px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }}
    h1, h2, h3, p {{ margin: 0; }}
    h1 {{ font-size: 24px; }}
    h2 {{ font-size: 22px; }}
    h3 {{ font-size: 16px; margin-bottom: 10px; }}
    .subhead {{ color: var(--muted); margin-top: 6px; }}
    .layout {{
      display: grid;
      grid-template-columns: 230px 1fr;
      min-height: calc(100vh - 82px);
    }}
    .sidebar {{
      border-right: 1px solid var(--line);
      padding: 16px;
      background: var(--surface);
    }}
    .day-tab {{
      width: 100%;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 2px 10px;
      align-items: center;
      text-align: left;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      margin-bottom: 8px;
    }}
    .day-tab strong {{ font-size: 18px; color: var(--accent); }}
    .day-tab small {{ grid-column: 1 / -1; color: var(--muted); }}
    .day-tab.active {{ background: var(--accent-soft); border-color: var(--accent); }}
    main {{ padding: 20px 24px 36px; }}
    .day-panel {{ display: none; max-width: 1180px; }}
    .day-panel.active {{ display: block; }}
    .panel-header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 18px;
    }}
    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .attention-pill {{
      background: var(--warning-soft);
      color: var(--warning);
      border: 1px solid #edd39e;
      border-radius: 8px;
      padding: 8px 10px;
      white-space: nowrap;
      font-weight: 700;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: var(--surface);
    }}
    .metric.primary {{ background: var(--surface-soft); border-color: #c7d8cc; }}
    .metric span, .metric small {{ display: block; color: var(--muted); }}
    .metric strong {{ display: block; font-size: 28px; margin: 4px 0; }}
    .split, .columns {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-bottom: 14px;
    }}
    .block {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 14px;
    }}
    .block-heading {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 10px;
    }}
    .block-heading h3 {{ margin-bottom: 0; }}
    .copy-team-message {{
      border: 1px solid var(--accent);
      border-radius: 8px;
      background: var(--accent);
      color: #ffffff;
      padding: 8px 12px;
      cursor: pointer;
      font-weight: 700;
    }}
    .team-message {{
      width: 100%;
      min-height: 230px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      color: var(--text);
      background: #fbfcfa;
      font: 14px/1.45 Arial, Helvetica, sans-serif;
    }}
    summary {{ cursor: pointer; font-weight: 700; }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin: 6px 0; }}
    .muted {{ color: var(--muted); }}
    .attention-list li {{ color: var(--warning); font-weight: 700; }}
    .audit-panel {{
      max-width: 1180px;
      margin-bottom: 18px;
    }}
    .audit-controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 12px 0;
    }}
    .audit-filter {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      padding: 8px 10px;
      cursor: pointer;
      font-weight: 700;
    }}
    .audit-filter.active {{
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent);
    }}
    .quick-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .quick-action {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-soft);
      color: var(--text);
      padding: 8px 10px;
      cursor: pointer;
      font-weight: 700;
    }}
    .audit-results {{
      display: grid;
      gap: 8px;
    }}
    .audit-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--surface);
    }}
    .audit-item div {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 4px;
    }}
    .audit-label {{
      color: var(--warning);
      font-weight: 700;
    }}
    @media (max-width: 850px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .sidebar {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .metrics, .split, .columns {{ grid-template-columns: 1fr; }}
      .panel-header {{ flex-direction: column; }}
      .attention-pill {{ white-space: normal; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>HotelRunner Daily Dashboard</h1>
    <p class="subhead">Generated {html.escape(generated_at.strftime('%Y-%m-%d %H:%M'))}. Read-only view from HotelRunner reservations.</p>
  </header>
  <div class="layout">
    <nav class="sidebar" aria-label="Days">
      {"".join(day_buttons)}
    </nav>
    <main>
      {"".join(day_sections)}
      <section class="block audit-panel">
        <h2>Audit Results</h2>
        <p class="subhead">Use these buttons to show conflicts, duplicates, bed requests, and notes across the whole period.</p>
        <div class="audit-controls" aria-label="Audit filters">
          <button class="audit-filter active" data-audit-filter="all">All ({len(audit_results)})</button>
          <button class="audit-filter" data-audit-filter="room">Room conflicts ({audit_counts.get("room", 0)})</button>
          <button class="audit-filter" data-audit-filter="block">Block conflicts ({audit_counts.get("block", 0)})</button>
          <button class="audit-filter" data-audit-filter="duplicate">Duplicates ({audit_counts.get("duplicate", 0)})</button>
          <button class="audit-filter" data-audit-filter="bed">Bed requests ({audit_counts.get("bed", 0)})</button>
          <button class="audit-filter" data-audit-filter="note">Notes ({audit_counts.get("note", 0)})</button>
        </div>
        <div class="audit-results">
          {render_audit_results(audit_results)}
        </div>
        <div class="quick-actions">
          <button class="quick-action" type="button" data-open-details="team">Open all team messages</button>
          <button class="quick-action" type="button" data-open-details="notes">Open all notes</button>
          <button class="quick-action" type="button" data-open-details="beds">Open bed disposition</button>
        </div>
      </section>
    </main>
  </div>
  <script>
    document.querySelectorAll('.day-tab').forEach((button) => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('.day-tab').forEach((item) => item.classList.remove('active'));
        document.querySelectorAll('.day-panel').forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        document.getElementById(button.dataset.target).classList.add('active');
      }});
    }});
    document.querySelectorAll('.audit-filter').forEach((button) => {{
      button.addEventListener('click', () => {{
        const filter = button.dataset.auditFilter;
        document.querySelectorAll('.audit-filter').forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        document.querySelectorAll('.audit-item').forEach((item) => {{
          item.style.display = filter === 'all' || item.dataset.auditType === filter ? '' : 'none';
        }});
      }});
    }});
    document.querySelectorAll('.copy-team-message').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const textarea = button.closest('.team-message-block').querySelector('.team-message');
        textarea.select();
        try {{
          await navigator.clipboard.writeText(textarea.value);
          button.textContent = 'Copied';
          setTimeout(() => button.textContent = 'Copy', 1400);
        }} catch (error) {{
          document.execCommand('copy');
          button.textContent = 'Copied';
          setTimeout(() => button.textContent = 'Copy', 1400);
        }}
      }});
    }});
    document.querySelectorAll('[data-open-details]').forEach((button) => {{
      button.addEventListener('click', () => {{
        const action = button.dataset.openDetails;
        if (action === 'team') {{
          document.querySelectorAll('.team-message').forEach((item) => item.closest('.team-message-block').scrollIntoView({{ block: 'nearest' }}));
        }}
        if (action === 'notes') {{
          document.querySelectorAll('details').forEach((item) => {{
            if (item.textContent.includes('Notes and messages')) item.open = true;
          }});
        }}
        if (action === 'beds') {{
          document.querySelectorAll('details').forEach((item) => {{
            if (item.textContent.includes('Room / bed disposition')) item.open = true;
          }});
        }}
      }});
    }});
  </script>
</body>
</html>
"""


def build_dashboard_html(summaries: list[DaySummary], generated_at: dt.datetime, run_status: RunStatus | None = None) -> str:
    """Render the modern Tailwind dashboard. This intentionally overrides the legacy renderer above."""
    day_buttons: list[str] = []
    day_sections: list[str] = []
    audit_results = collect_audit_results(summaries)
    audit_counts = Counter(item["type"] for item in audit_results)

    def badge(text: str, tone: str) -> str:
        tones = {
            "meal": "bg-emerald-400/10 text-emerald-200 ring-1 ring-emerald-400/20",
            "block": "bg-amber-400/10 text-amber-200 ring-1 ring-amber-400/20",
            "conflict": "bg-rose-400/10 text-rose-200 ring-1 ring-rose-400/20",
            "neutral": "bg-slate-800 text-slate-200 ring-1 ring-white/10",
        }
        return f'<span class="inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold {tones[tone]}">{html.escape(text)}</span>'

    def list_cards(items: list[str], tone: str = "neutral") -> str:
        if not items:
            return '<p class="text-sm text-slate-500">None</p>'
        color = {
            "conflict": "border-rose-400/20 bg-rose-500/5 text-rose-100",
            "block": "border-amber-400/20 bg-amber-500/5 text-amber-100",
            "meal": "border-emerald-400/20 bg-emerald-500/5 text-emerald-100",
            "neutral": "border-white/10 bg-slate-900/60 text-slate-200",
        }[tone]
        return "".join(f'<div class="rounded-lg border {color} px-3 py-2 text-sm">{html.escape(item)}</div>' for item in items)

    def audit_details(title: str, items: list[str], tone: str) -> str:
        return f"""
        <details class="rounded-xl border border-white/10 bg-slate-950/70 p-4">
          <summary class="cursor-pointer text-sm font-semibold text-slate-100">{html.escape(title)} <span class="text-slate-500">({len(items)})</span></summary>
          <div class="mt-3 grid gap-2">{list_cards(items, tone)}</div>
        </details>
        """

    if run_status is None:
        run_status = RunStatus(True, "Ready to review", "Review attention items, then copy the team message.", ())
    status_tone = "border-emerald-400/30 bg-emerald-400/10 text-emerald-100" if run_status.safe_to_send else "border-rose-400/40 bg-rose-500/10 text-rose-100"
    status_badge = "READY" if run_status.safe_to_send else "CHECK FIRST"
    status_details = "".join(f"<li>{html.escape(item)}</li>" for item in run_status.details)

    all_block_conflicts = [item["text"] for item in audit_results if item["type"] == "block"]
    all_room_conflicts = [item["text"] for item in audit_results if item["type"] == "room"]
    all_duplicates = [item["text"] for item in audit_results if item["type"] == "duplicate"]
    all_bed_requests = [item["text"] for item in audit_results if item["type"] == "bed"]
    all_extras = [f"{item['day']}: {item['text']}" for item in audit_results if item["type"] == "extra"]
    all_notes = [f"{item['day']}: {item['text']}" for item in audit_results if item["type"] == "note"]

    from meal_engine import (
        compute_meal_entitlements, summarize_dinner,
        dinner_preparation_notice, format_dinner_team_message,
    )
    from settlement_engine import build_settlement_reminders
    from transfer_engine import build_transfer_records, format_driver_message
    from transfer_state import TransferStateStore
    from system_health import SystemHealthStore

    transfer_store = TransferStateStore()
    health_store = SystemHealthStore()
    health_status = health_store.get_health_status()
    try:
        from google_sheets import load_sunrise_reservations
        sunrise_sheet_reservations = load_sunrise_reservations()
    except Exception:
        sunrise_sheet_reservations = []

    for index, summary in enumerate(summaries):
        day_id = f"day-{index}"
        active = "block" if index == 0 else "hidden"
        button_active = "border-emerald-300/50 bg-emerald-400/10 text-emerald-100" if index == 0 else "border-white/10 bg-slate-950/60 text-slate-300"
        attention_count = len(summary.block_conflicts) + len(summary.room_conflicts) + len(summary.possible_duplicates) + len(summary.bed_requests)
        day_buttons.append(
            f"""
            <button class="day-tab rounded-xl border {button_active} p-3 text-left transition hover:border-emerald-300/40" data-target="{day_id}">
              <span class="block text-xs uppercase tracking-wide text-slate-500">{html.escape(summary.date.strftime('%A'))}</span>
              <span class="mt-1 block text-base font-bold">{html.escape(summary.date.strftime('%d %b'))}</span>
              <span class="mt-2 flex items-center justify-between text-xs text-slate-400"><span>{len(summary.arrivals)} in / {len(summary.departures)} out</span><strong class="text-slate-100">{summary.guests}</strong></span>
            </button>
            """
        )

        # ── Normalized Meals & Transfers via Domain Engines ───────────────────
        day_staylines = summary.in_house + summary.arrivals + summary.departures
        seen_res_ids = set()
        day_norm_res = []
        for line in day_staylines:
            if line.reservation_id not in seen_res_ids:
                seen_res_ids.add(line.reservation_id)
                day_norm_res.append(normalized_from_stayline_with_extras(line))
        active_sheet_res = sunrise_sheet_active_reservations(day_staylines, sunrise_sheet_reservations, summary.date)
        active_sheet_all = [
            res for res in sunrise_sheet_reservations
            if res.is_active_on(summary.date)
        ]
        from data_model import merge_cross_source_duplicates
        day_norm_res = merge_cross_source_duplicates(day_norm_res + active_sheet_all)

        entitlements = compute_meal_entitlements(day_norm_res, summary.date)
        dinner_summary = summarize_dinner(entitlements)
        breakfast_total = sum(e.count for e in entitlements if e.meal == "breakfast")
        lunch_total = sum(e.count for e in entitlements if e.meal == "lunch")
        dinner_total = dinner_summary["total"]
        by_house = dinner_summary["by_house"]
        house_breakdown = " | ".join(f"{h}: {n}" for h, n in sorted(by_house.items()) if n > 0)
        dinner_notice = dinner_preparation_notice(dinner_total)
        dinner_team_message = format_dinner_team_message(entitlements, day_norm_res)
        dinner_copy_html = ""
        if dinner_summary["audit"]:
            dinner_copy_html = f"""
            <div class="mt-3 rounded-xl border border-emerald-400/20 bg-emerald-400/5 p-3">
              <div class="mb-2 flex items-center justify-between gap-2">
                <span class="text-xs font-semibold uppercase text-emerald-200">Team Dinner List</span>
                <button class="copy-text-btn rounded bg-emerald-400 px-2.5 py-1 text-xs font-bold text-slate-950" data-text="{html.escape(dinner_team_message)}" type="button">Copy Dinner List</button>
              </div>
              <pre class="whitespace-pre-wrap font-mono text-sm text-slate-200">{html.escape(dinner_team_message)}</pre>
            </div>"""

        settlement_reminders = build_settlement_reminders(day_norm_res, summary.date)
        settlement_html = ""
        if settlement_reminders:
            payment_cards = []
            for item in settlement_reminders:
                when = "COLLECT TODAY" if item.timing == "today" else "PREPARE FOR TOMORROW"
                amount = (
                    f"Total €{item.total_amount:.2f} · recorded €{item.paid_amount:.2f} · remaining €{item.remaining_amount:.2f}"
                    if item.total_amount > 0 else "Reservation total needs confirmation"
                )
                extras = ", ".join(item.extra_checks) if item.extra_checks else "No recorded extras; check group messages"
                policy = (
                    f" · expected {item.expected_deposit_percent}% Surf Camp deposit"
                    if item.expected_deposit_percent is not None else ""
                )
                payment_cards.append(
                    f'<div class="rounded-lg border border-amber-400/20 bg-amber-400/5 p-3">'
                    f'<strong class="text-amber-200">{when} — {html.escape(item.guest_name)} ({html.escape(item.house)})</strong>'
                    f'<p class="mt-1 text-sm text-slate-200">{html.escape(amount + policy)}</p>'
                    f'<p class="text-sm text-slate-300">{html.escape(item.action)}</p>'
                    f'<p class="mt-1 text-xs text-slate-400">Check extras: {html.escape(extras)}</p></div>'
                )
            settlement_html = (
                '<div class="mt-4 rounded-2xl border border-amber-400/20 bg-slate-950/70 p-4">'
                '<h3 class="mb-3 text-sm font-semibold uppercase text-amber-200">Checkout Payments</h3>'
                '<div class="grid gap-2">' + "".join(payment_cards) + '</div></div>'
            )

        day_transfers = build_transfer_records(day_norm_res, summary.date, transfer_store)

        # ── Tomorrow transfers (prepare today) — only shown on today's view ──
        tomorrow_transfers: list = []
        if index == 0 and len(summaries) > 1:
            tomorrow_summary = summaries[1]
            tmrw_staylines = tomorrow_summary.in_house + tomorrow_summary.arrivals + tomorrow_summary.departures
            seen_tmrw: set = set()
            tmrw_norm_res = []
            for line in tmrw_staylines:
                if line.reservation_id not in seen_tmrw:
                    seen_tmrw.add(line.reservation_id)
                    tmrw_norm_res.append(normalized_from_stayline_with_extras(line))
            tomorrow_transfers = build_transfer_records(tmrw_norm_res, tomorrow_summary.date, transfer_store)

        total_transfer_count = len(day_transfers) + len(tomorrow_transfers)
        alert_badges = " ".join(
            [
                badge(f"Conflicts: {len(summary.room_conflicts)}", "conflict"),
                badge(f"Blocks: {len(summary.block_conflicts)}", "block"),
                badge(f"Bed requests: {len(summary.bed_requests)}", "neutral"),
                badge(f"Transfers: {total_transfer_count}", "meal" if total_transfer_count else "neutral"),
            ]
        )
        arrival_items = [short_guest_line(line) for line in summary.arrivals]
        arrival_items.extend(
            sunrise_sheet_reminder_lines(summary.arrivals, sunrise_sheet_reservations, summary.date, "arrival")
        )
        departure_items = [short_guest_line(line) for line in summary.departures]
        departure_items.extend(
            sunrise_sheet_reminder_lines(summary.departures, sunrise_sheet_reservations, summary.date, "departure")
        )
        in_house_items = [short_guest_line(line) for line in summary.in_house]
        in_house_items.extend(sunrise_sheet_guest_line(res) for res in active_sheet_res)
        room_items = [f"{room}: {count} guest(s)" for room, count in sorted(room_disposition(summary).items())]
        if sunrise_sheet_reservations:
            team_message = build_whatsapp_block_with_sunrise_sheet(summary, sunrise_sheet_reservations) + "\n\n————\n\n" + build_manager_block(summary)
        else:
            team_message = build_team_message(summary)

        # ── Helper: build a transfer card HTML ──────────────────────────────
        def _transfer_card(tr) -> str:
            direction_label = "ARRIVÉE" if tr.is_arrival else "DÉPART"
            dir_tone = "bg-emerald-500/20 text-emerald-300 ring-1 ring-emerald-500/30" if tr.is_arrival else "bg-indigo-500/20 text-indigo-300 ring-1 ring-indigo-500/30"
            if tr.status == "ready_to_send":
                status_badge_html = '<span class="rounded-full bg-emerald-400/10 px-2.5 py-0.5 text-xs font-semibold text-emerald-300 ring-1 ring-emerald-400/20">READY TO SEND</span>'
            elif tr.status == "sent":
                status_badge_html = '<span class="rounded-full bg-slate-800 px-2.5 py-0.5 text-xs font-semibold text-slate-300 ring-1 ring-white/10">SENT</span>'
            else:
                status_badge_html = '<span class="rounded-full bg-amber-400/10 px-2.5 py-0.5 text-xs font-semibold text-amber-300 ring-1 ring-amber-400/20">NEEDS INFO</span>'
            driver_msg = format_driver_message(tr)
            details_text = f"Flight: {tr.flight_number or '⚠️ missing'} · {tr.airport or 'Agadir'}" if tr.is_arrival else f"Time: {tr.pickup_time or '⚠️ missing'} · {tr.destination or 'Agadir'}"
            return f"""
                <div class="rounded-xl border border-white/10 bg-slate-900/60 p-4">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex items-center gap-2">
                      <span class="rounded px-2 py-0.5 text-xs font-bold {dir_tone}">{direction_label}</span>
                      <strong class="text-white text-base">{html.escape(tr.guest_name)}</strong>
                      <span class="text-xs text-slate-400">X{tr.passenger_count} ({html.escape(tr.house)})</span>
                    </div>
                    <div>{status_badge_html}</div>
                  </div>
                  <p class="mt-1 text-xs text-slate-400">{html.escape(details_text)}</p>
                  <div class="mt-3 rounded-lg border border-white/5 bg-slate-950/80 p-3">
                    <div class="flex items-center justify-between gap-2 mb-1.5">
                      <span class="text-xs font-semibold uppercase tracking-wide text-slate-400">Taxi Driver Message (French)</span>
                      <button class="copy-transfer-btn rounded bg-emerald-400/20 px-2 py-1 text-xs font-bold text-emerald-200 transition hover:bg-emerald-400/30" data-text="{html.escape(driver_msg)}" type="button">Copy Driver Text</button>
                    </div>
                    <pre class="font-mono text-xs text-slate-300 whitespace-pre-wrap">{html.escape(driver_msg)}</pre>
                  </div>
                </div>"""

        # Format Transfers HTML cards (today + tomorrow prepare section)
        today_part = ""
        if day_transfers:
            today_label = f'<p class="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">— Today {summary.date.strftime("%d %b")} —</p>' if tomorrow_transfers else ""
            today_part = today_label + '<div class="grid gap-3">' + "".join(_transfer_card(tr) for tr in day_transfers) + "</div>"
        else:
            today_part = '<p class="text-sm text-slate-500">No transfers today ✓</p>'

        tomorrow_part = ""
        if tomorrow_transfers:
            tmrw_date_str = summaries[1].date.strftime("%d %b") if len(summaries) > 1 else "tomorrow"
            tomorrow_part = f"""
            <div class="mt-4 rounded-xl border border-amber-400/20 bg-amber-400/5 p-4">
              <p class="text-xs font-semibold uppercase tracking-wide text-amber-300 mb-3">— Prepare for Tomorrow {tmrw_date_str} —</p>
              <div class="grid gap-3">{"".join(_transfer_card(tr) for tr in tomorrow_transfers)}</div>
            </div>"""

        if not day_transfers and not tomorrow_transfers:
            transfers_html = '<p class="text-sm text-slate-500">No transfers today or tomorrow ✓</p>'
        else:
            transfers_html = today_part + tomorrow_part

        # Format Dinner Notice Banner
        if dinner_notice:
            is_cap = "CAPACITY" in dinner_notice
            notice_banner = f"""
            <div class="mb-3 rounded-xl border {'border-rose-400/40 bg-rose-500/10 text-rose-200' if is_cap else 'border-amber-400/30 bg-amber-400/10 text-amber-200'} p-3 font-semibold text-sm">
              {html.escape(dinner_notice)}
            </div>
            """
        else:
            notice_banner = ""

        # Format Meals Section
        meals_section_html = f"""
        <div class="mt-4 rounded-2xl border border-white/10 bg-slate-950/70 p-4">
          <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
            <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">Meals & Dinner Preparation</h3>
            {f'<span class="rounded-lg bg-emerald-400/10 px-2.5 py-1 text-xs font-semibold text-emerald-300 ring-1 ring-emerald-400/20">Normal Setup (≤13)</span>' if not dinner_notice else ''}
          </div>
          {notice_banner}
          <div class="flex flex-wrap items-center gap-3">
            <div class="rounded-xl border border-white/10 bg-slate-900/60 px-4 py-2 text-center">
              <span class="block text-xs uppercase tracking-wide text-slate-400">Breakfast</span>
              <span class="text-xl font-bold text-white">{breakfast_total}</span>
            </div>
            <div class="rounded-xl border border-white/10 bg-slate-900/60 px-4 py-2 text-center">
              <span class="block text-xs uppercase tracking-wide text-slate-400">Lunch</span>
              <span class="text-xl font-bold text-white">{lunch_total}</span>
            </div>
            <div class="rounded-xl border border-white/10 bg-slate-900/60 px-4 py-2 text-center">
              <span class="block text-xs uppercase tracking-wide text-slate-400">Dinner</span>
              <span class="text-xl font-bold text-white">{dinner_total}</span>
            </div>
            {f'<div class="text-sm text-slate-400 pl-2">By House: <strong class="text-slate-200">{html.escape(house_breakdown)}</strong></div>' if house_breakdown else ''}
          </div>
          {dinner_copy_html}
          {f'''
          <details class="mt-3 rounded-xl border border-white/5 bg-slate-900/40 p-3">
            <summary class="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-400">Dinner Audit Trail ({len(dinner_summary['audit'])} guests)</summary>
            <div class="mt-2 grid gap-1 text-xs text-slate-300">
              {''.join(f'<div class="flex items-center justify-between border-b border-white/5 py-1"><span>{html.escape(item.guest_name)} ({html.escape(item.house)})</span><span class="text-slate-400">{item.count} cover(s) · {html.escape(item.reason_text)}</span></div>' for item in dinner_summary['audit'])}
            </div>
          </details>
          ''' if dinner_summary['audit'] else ''}
        </div>
        """

        day_sections.append(
            f"""
            <section id="{day_id}" class="day-panel {active}">
              <div class="mb-5 flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                <div>
                  <p class="text-sm uppercase tracking-wide text-emerald-300">{html.escape(summary.date.strftime('%A'))}</p>
                  <h2 class="mt-1 text-3xl font-bold text-white">{html.escape(summary.date.strftime('%d %B %Y'))}</h2>
                </div>
                <div class="flex flex-wrap gap-2">{alert_badges}</div>
              </div>

              <div class="grid gap-3 md:grid-cols-4">
                <div class="rounded-2xl border border-emerald-400/20 bg-emerald-400/10 p-4">
                  <p class="text-sm text-emerald-200">In-house</p>
                  <p class="mt-1 text-3xl font-bold text-white">{summary.guests}</p>
                  <p class="text-xs text-emerald-100/70">{summary.adults} adults, {summary.children} children</p>
                </div>
                <div class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><p class="text-sm text-slate-400">Arrivals</p><p class="mt-1 text-3xl font-bold text-white">{len(summary.arrivals)}</p></div>
                <div class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><p class="text-sm text-slate-400">Departures</p><p class="mt-1 text-3xl font-bold text-white">{len(summary.departures)}</p></div>
                <div class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><p class="text-sm text-slate-400">Occupied lines</p><p class="mt-1 text-3xl font-bold text-white">{len(summary.in_house)}</p></div>
              </div>

              {meals_section_html}
              {settlement_html}

              <div class="mt-4 rounded-2xl border border-white/10 bg-slate-950/70 p-4">
                <h3 class="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Transfers ({total_transfer_count}{"" if not tomorrow_transfers else f" — {len(day_transfers)} today · {len(tomorrow_transfers)} for tomorrow"})</h3>
                {transfers_html}
              </div>

              <div class="mt-4 rounded-2xl border border-white/10 bg-slate-950/80 p-4">
                <div class="mb-3 flex items-center justify-between gap-3">
                  <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">Team Message</h3>
                  <button class="copy-team-message rounded-full bg-emerald-400 px-3 py-1.5 text-xs font-bold text-slate-950 transition hover:bg-emerald-300" type="button">Copy Message</button>
                </div>
                <textarea class="team-message h-72 w-full resize-y rounded-xl border border-white/10 bg-slate-900 p-4 font-mono text-sm leading-6 text-slate-100 outline-none focus:border-emerald-300">{html.escape(team_message)}</textarea>
              </div>

              <div class="mt-4 grid gap-4 xl:grid-cols-2">
                <div class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><h3 class="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Arrivals</h3><div class="grid gap-2">{list_cards(arrival_items)}</div></div>
                <div class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><h3 class="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Departures</h3><div class="grid gap-2">{list_cards(departure_items)}</div></div>
              </div>

              <div class="mt-4 grid gap-3">
                <details class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><summary class="cursor-pointer text-sm font-semibold text-slate-100">In-house guests and beds</summary><div class="mt-3 grid gap-2">{list_cards(in_house_items)}</div></details>
                <details class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><summary class="cursor-pointer text-sm font-semibold text-slate-100">Room / bed disposition</summary><div class="mt-3 grid gap-2">{list_cards(room_items)}</div></details>
                <details class="rounded-2xl border border-white/10 bg-slate-950/70 p-4"><summary class="cursor-pointer text-sm font-semibold text-slate-100">Manual blocks</summary><div class="mt-3 grid gap-2">{list_cards(summary.blocks, 'block')}</div></details>
              </div>
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HotelRunner Daily Dashboard</title>
  <link rel="icon" type="image/png" href="olas-surf-camp.png">
  <link rel="apple-touch-icon" href="olas-surf-camp.png">
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-950 text-slate-100">
  <header class="border-b border-white/10 bg-slate-950/80 px-5 py-5 backdrop-blur">
    <div class="mx-auto max-w-7xl">
      <div class="flex items-center gap-4">
        <img src="olas-surf-camp.png" alt="Olas Surf Experience" class="h-20 w-20 shrink-0 rounded-lg bg-white object-contain p-1">
        <div>
          <p class="text-sm uppercase tracking-wide text-emerald-300">Olas Surf Experience · Operations</p>
          <h1 class="mt-1 text-3xl font-bold text-white">Daily In-House Dashboard</h1>
          <p class="mt-2 text-sm text-slate-400">Generated {html.escape(generated_at.strftime('%Y-%m-%d %H:%M'))}. Cache-backed with recent HotelRunner updates.</p>
        </div>
      </div>
      <section class="mt-4 rounded-2xl border {status_tone} p-4">
        <div class="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
          <div>
            <p class="text-xs font-bold uppercase tracking-wide">{status_badge}</p>
            <h2 class="mt-1 text-xl font-bold">{html.escape(run_status.title)}</h2>
            <p class="mt-1 text-sm opacity-90">{html.escape(run_status.message)}</p>
          </div>
          <ul class="grid gap-1 text-sm opacity-90 md:text-right">{status_details}</ul>
        </div>
      </section>
    </div>
  </header>

  <div class="mx-auto grid max-w-7xl gap-5 px-5 py-5 lg:grid-cols-[230px_1fr]">
    <nav class="h-fit rounded-2xl border border-white/10 bg-slate-900/70 p-3 lg:sticky lg:top-5">
      <p class="mb-3 px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Days</p>
      <div class="grid gap-2">{"".join(day_buttons)}</div>
    </nav>

    <main>
      {"".join(day_sections)}

      <section class="mt-6 rounded-2xl border border-white/10 bg-slate-900/70 p-5">
        <div class="mb-4 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <div>
            <h2 class="text-2xl font-bold text-white">Audit Results</h2>
            <p class="text-sm text-slate-400">Collapsed by default so the workspace stays clean until you need to review issues.</p>
          </div>
          <div class="flex flex-wrap gap-2">
            {badge(f"Rooms: {audit_counts.get('room', 0)}", "conflict")}
            {badge(f"Blocks: {audit_counts.get('block', 0)}", "block")}
            {badge(f"Beds: {audit_counts.get('bed', 0)}", "neutral")}
            {badge(f"Extras: {audit_counts.get('extra', 0)}", "meal")}
          </div>
        </div>
        <div class="grid gap-3">
          {audit_details("Room conflicts", all_room_conflicts, "conflict")}
          {audit_details("Block conflicts", all_block_conflicts, "block")}
          {audit_details("Duplicates / multi-bed reservations", all_duplicates, "neutral")}
          {audit_details("Bed requests", all_bed_requests, "neutral")}
          {audit_details("Extras / additional fees", all_extras, "meal")}
          {audit_details("Notes", all_notes, "neutral")}
        </div>
      </section>

      <section class="mt-6 rounded-2xl border border-white/10 bg-slate-900/70 p-5">
        <h2 class="text-xl font-bold text-white mb-2">System Health & Operations Engines</h2>
        <p class="text-xs text-slate-400 mb-4">Normalized operational status across all 3 houses (Olas, Tide, Sunrise).</p>
        <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 text-sm">
          <div class="rounded-xl border border-white/10 bg-slate-950/70 p-3">
            <span class="block text-xs uppercase text-slate-400">Last HotelRunner Fetch</span>
            <span class="font-semibold text-slate-200">{health_status.get('last_hotelrunner_fetch', {}).get('timestamp', 'Recent (Cache)') if health_status.get('last_hotelrunner_fetch') else 'Recent (Cache)'}</span>
          </div>
          <div class="rounded-xl border border-white/10 bg-slate-950/70 p-3">
            <span class="block text-xs uppercase text-slate-400">Tomorrow Transfers Jobs</span>
            <span class="font-semibold text-slate-200">17:00 & 20:00 (Morocco)</span>
          </div>
          <div class="rounded-xl border border-white/10 bg-slate-950/70 p-3">
            <span class="block text-xs uppercase text-slate-400">Conflict Engine</span>
            <span class="font-semibold text-emerald-400">Active (9300 Dorm Bed-Level)</span>
          </div>
          <div class="rounded-xl border border-white/10 bg-slate-950/70 p-3">
            <span class="block text-xs uppercase text-slate-400">Meal Engine</span>
            <span class="font-semibold text-emerald-400">Active (Auditable & Deduplicated)</span>
          </div>
        </div>
      </section>
    </main>
  </div>

  <script>
    document.querySelectorAll('.day-tab').forEach((button) => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('.day-tab').forEach((item) => {{
          item.classList.remove('border-emerald-300/50', 'bg-emerald-400/10', 'text-emerald-100');
          item.classList.add('border-white/10', 'bg-slate-950/60', 'text-slate-300');
        }});
        document.querySelectorAll('.day-panel').forEach((item) => {{
          item.classList.add('hidden');
          item.classList.remove('block');
        }});
        button.classList.add('border-emerald-300/50', 'bg-emerald-400/10', 'text-emerald-100');
        button.classList.remove('border-white/10', 'bg-slate-950/60', 'text-slate-300');
        const panel = document.getElementById(button.dataset.target);
        panel.classList.remove('hidden');
        panel.classList.add('block');
      }});
    }});

    document.querySelectorAll('.copy-team-message').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const textarea = button.closest('section').querySelector('.team-message');
        textarea.select();
        try {{
          await navigator.clipboard.writeText(textarea.value);
        }} catch (error) {{
          document.execCommand('copy');
        }}
        const original = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => button.textContent = original, 1400);
      }});
    }});

    document.querySelectorAll('.copy-transfer-btn').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const text = button.dataset.text;
        try {{
          await navigator.clipboard.writeText(text);
        }} catch (error) {{
          const temp = document.createElement('textarea');
          temp.value = text;
          document.body.appendChild(temp);
          temp.select();
          document.execCommand('copy');
          document.body.removeChild(temp);
        }}
        const original = button.textContent;
        button.textContent = 'Copied!';
        setTimeout(() => button.textContent = original, 1400);
      }});
    }});

    document.querySelectorAll('.copy-text-btn').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const text = button.dataset.text;
        try {{
          await navigator.clipboard.writeText(text);
        }} catch (error) {{
          const temp = document.createElement('textarea');
          temp.value = text;
          document.body.appendChild(temp);
          temp.select();
          document.execCommand('copy');
          document.body.removeChild(temp);
        }}
        const original = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => button.textContent = original, 1400);
      }});
    }});
  </script>
</body>
</html>
"""


def write_audit_file(reservations: list[dict[str, Any]], stay_lines: list[StayLine], summaries: list[DaySummary], output_path: Path, run_status: RunStatus | None = None) -> None:
    state_counts = Counter(str(first_value(item, ["state"], "unknown") or "unknown") for item in reservations)
    channel_counts = Counter(line.channel or "Unknown" for line in stay_lines)
    payload = {
        "run_status": {
            "safe_to_send": run_status.safe_to_send,
            "title": run_status.title,
            "message": run_status.message,
            "details": list(run_status.details),
        } if run_status else None,
        "fetched_reservations": len(reservations),
        "reservation_states": dict(sorted(state_counts.items())),
        "active_room_or_bed_lines": len(stay_lines),
        "channels": dict(sorted(channel_counts.items())),
        "report_days": [
            {
                "date": summary.date.isoformat(),
                "in_house_guests": summary.guests,
                "arrivals": len(summary.arrivals),
                "departures": len(summary.departures),
                "room_conflicts": summary.room_conflicts,
                "block_conflicts": summary.block_conflicts,
                "possible_duplicates": summary.possible_duplicates,
                "bed_requests": summary.bed_requests,
                "extras": summary.extras,
            }
            for summary in summaries
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a read-only HotelRunner daily operations report.")
    parser.add_argument("--start-date", help="Report start date in YYYY-MM-DD format. Defaults to today.")
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD, help="How many days after start date to include.")
    parser.add_argument("--output", default="hotelrunner_daily_summary.md", help="Markdown report file to create.")
    parser.add_argument("--dashboard-output", default="hotelrunner_dashboard.html", help="HTML dashboard file to create.")
    parser.add_argument("--no-dashboard", action="store_true", help="Only create the Markdown report.")
    parser.add_argument("--blocks-file", default="hotelrunner_blocks.json", help="Optional local room/house blocks JSON file.")
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_PATH, help="Local JSON cache used to avoid deep HotelRunner fetches.")
    parser.add_argument("--update-lookback-days", type=int, default=DEFAULT_UPDATE_LOOKBACK_DAYS, help="How far back to fetch recently updated HotelRunner reservations.")
    parser.add_argument("--lookback-days", type=int, help="Optional bootstrap/debug mode: fetch reservations by historical creation date instead of cache update mode.")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Maximum reservation pages to read from HotelRunner.")
    parser.add_argument("--page-delay", type=float, default=0.25, help="Seconds to wait between HotelRunner API pages.")
    parser.add_argument("--retries", type=int, default=3, help="How many times to retry a rate-limited HotelRunner page.")
    parser.add_argument("--retry-wait", type=float, default=30.0, help="Base seconds to wait after HotelRunner rate limits a page.")
    parser.add_argument("--debug-sample", action="store_true", help="Save a small JSON structure sample for field mapping.")
    parser.add_argument("--find-guest", help="Save a private debug file for matching guest name or reservation number.")
    parser.add_argument("--find-room", help="Save a private debug file for matching room/bed name or number.")
    args = parser.parse_args()

    load_dotenv(Path(".env"))

    token = get_required_env("HOTELRUNNER_TOKEN")
    hr_id = get_required_env("HOTELRUNNER_HR_ID")
    start = parse_date(args.start_date) if args.start_date else dt.date.today()
    if not start:
        raise SystemExit("Invalid --start-date. Use YYYY-MM-DD.")

    write_shortcut_scripts(Path("."))

    cache_path = Path(args.cache_file)
    if args.lookback_days is not None:
        print("Bootstrap/debug mode: fetching by historical date and refreshing the cache.")
        fetch_meta: dict[str, Any] = {}
        reservations = fetch_reservations(
            token=token,
            hr_id=hr_id,
            lookback_days=args.lookback_days,
            max_pages=args.max_pages,
            page_delay=args.page_delay,
            retries=args.retries,
            retry_wait=args.retry_wait,
            fetch_meta=fetch_meta,
        )
        active_reservations = [reservation for reservation in reservations if is_active_state(first_value(reservation, ["state"]))]
        save_reservation_cache(cache_path, active_reservations)
        cache_stats = {"loaded": 0, "updates": len(reservations), "added": len(active_reservations), "updated": 0, "removed": 0, "skipped": 0}
        write_last_fetch_debug(
            Path("hotelrunner_last_fetch.json"),
            mode="historical_bootstrap",
            stats=cache_stats,
            fetched=reservations,
            max_pages=args.max_pages,
            lookback_days=args.lookback_days,
            fetch_meta=fetch_meta,
        )
        reservations = active_reservations
        run_status = build_run_status(cache_stats, fetch_meta, mode="historical_bootstrap")
    else:
        fetch_meta = {}
        reservations, cache_stats, fetched_updates = load_update_and_save_cache(
            cache_path=cache_path,
            token=token,
            hr_id=hr_id,
            update_lookback_days=args.update_lookback_days,
            max_pages=args.max_pages,
            page_delay=args.page_delay,
            retries=args.retries,
            retry_wait=args.retry_wait,
            fetch_meta=fetch_meta,
        )
        write_last_fetch_debug(
            Path("hotelrunner_last_fetch.json"),
            mode="recent_updates",
            stats=cache_stats,
            fetched=fetched_updates,
            max_pages=args.max_pages,
            lookback_days=args.update_lookback_days,
            fetch_meta=fetch_meta,
        )
        run_status = build_run_status(cache_stats, fetch_meta, mode="recent_updates")
    print(
        "Cache: "
        f"{cache_stats['loaded']} loaded, {cache_stats['updates']} updates, "
        f"{cache_stats['added']} added, {cache_stats['updated']} updated, "
        f"{cache_stats['removed']} removed, {len(reservations)} total."
    )
    if args.debug_sample:
        write_debug_sample(reservations, Path("hotelrunner_debug_sample.json"))
    if args.find_guest:
        count = write_guest_debug(reservations, args.find_guest, Path("hotelrunner_guest_debug.json"))
        print(f"Saved {count} matching reservation(s) to hotelrunner_guest_debug.json")
        return

    stay_lines = active_stay_lines(reservations)
    if args.find_room:
        count = write_room_debug(
            stay_lines,
            args.find_room,
            Path("hotelrunner_room_debug.json"),
            metadata={
                "lookback_days": args.lookback_days,
                "update_lookback_days": args.update_lookback_days,
                "max_pages": args.max_pages,
                "page_delay": args.page_delay,
            },
        )
        print(f"Saved {count} matching room/bed line(s) to hotelrunner_room_debug.json")
        return

    blocks = load_room_blocks(Path(args.blocks_file))
    summaries = build_day_summaries(stay_lines, start=start, days_ahead=args.days_ahead, blocks=blocks)
    report = build_report(summaries, run_status=run_status)
    write_audit_file(reservations, stay_lines, summaries, Path("hotelrunner_audit.json"), run_status=run_status)

    output_path = Path(args.output)
    output_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nSaved report to: {output_path.resolve()}")

    if not args.no_dashboard:
        dashboard_path = Path(args.dashboard_output)
        dashboard = build_dashboard_html(summaries, generated_at=dt.datetime.now(), run_status=run_status)
        dashboard_path.write_text(dashboard, encoding="utf-8")
        print(f"Saved dashboard to: {dashboard_path.resolve()}")


# Sample GitHub Actions workflow template for `.github/workflows/daily_run.yml`:
#
# name: Daily HotelRunner Summary
# on:
#   schedule:
#     - cron: "0 6 * * *"
#   workflow_dispatch:
# jobs:
#   daily-run:
#     runs-on: ubuntu-latest
#     steps:
#       - uses: actions/checkout@v4
#       - uses: actions/setup-python@v5
#         with:
#           python-version: "3.x"
#       - name: Run HotelRunner daily summary
#         env:
#           HOTELRUNNER_TOKEN: ${{ secrets.HOTELRUNNER_TOKEN }}
#           HOTELRUNNER_HR_ID: ${{ secrets.HOTELRUNNER_HR_ID }}
#         run: python hotelrunner_daily_summary.py --max-pages 15 --page-delay 1


def write_shortcut_scripts(folder: Path) -> None:
    """Write daily-run shortcuts beside this script. Existing files are refreshed intentionally."""
    run_bat = folder / "run.bat"
    run_sh = folder / "run.sh"
    run_bat.write_text("python hotelrunner_daily_summary.py --max-pages 15 --page-delay 1\r\n", encoding="utf-8")
    run_sh.write_text("#!/bin/bash\npython hotelrunner_daily_summary.py --max-pages 15 --page-delay 1\n", encoding="utf-8")
    try:
        run_sh.chmod(0o755)
    except OSError:
        pass


if __name__ == "__main__":
    main()
