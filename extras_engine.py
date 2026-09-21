"""
extras_engine.py
────────────────
Classifies HotelRunner Additional Fees / Extras into typed operational objects.

Each extra becomes a NormalizedExtra with:
  - category: transfer | surf_equipment | surf_lesson | meal | tax | unknown
  - operational_note: what the manager needs to DO about it

The raw extras are already extracted by extract_extras() in hotelrunner_daily_summary.py
as formatted strings like "Surfboards Rental x14 140".

This engine works on the raw reservation dict to get full detail including dates,
and also accepts the already-formatted string as fallback.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from data_model import NormalizedExtra, NormalizedReservation, load_config


# ── Keyword matching ──────────────────────────────────────────────────────────

def _category_from_label(label: str) -> str:
    cfg = load_config()
    extras_cfg = cfg.get("extras", {})
    label_lower = label.casefold()

    if any(k in label_lower for k in extras_cfg.get("tax", [])):
        return "tax"
    if any(k in label_lower for k in extras_cfg.get("transfer", [])):
        return "transfer"
    if any(k in label_lower for k in extras_cfg.get("surf_lesson", [])):
        return "surf_lesson"
    if any(k in label_lower for k in extras_cfg.get("surf_equipment", [])):
        return "surf_equipment"
    if any(k in label_lower for k in extras_cfg.get("meal_extras", [])):
        return "meal"
    return "unknown"


def _operational_note(category: str, label: str, quantity: int | None, amount: float | None, dates: list[dt.date]) -> str | None:
    """Generate a human-readable action note for the manager."""
    qty_str = f" x{quantity}" if quantity else ""
    amt_str = f" — €{amount:.0f}" if amount else ""
    date_str = ""
    if dates:
        if len(dates) == 1:
            date_str = f" on {dates[0].strftime('%d %b')}"
        elif len(dates) == 2:
            date_str = f" on {dates[0].strftime('%d %b')} and {dates[1].strftime('%d %b')}"
        else:
            date_str = f" from {dates[0].strftime('%d %b')} to {dates[-1].strftime('%d %b')}"

    if category == "transfer":
        return f"Airport transfer{qty_str}{amt_str}{date_str} — confirm pickup arranged"
    if category == "surf_equipment":
        return f"Surf equipment{qty_str}{amt_str} — verify boards/wetsuits prepared"
    if category == "surf_lesson":
        return f"Surf lesson{qty_str}{amt_str} — include in surf headcount"
    if category == "meal":
        return f"Meal extra{qty_str}{amt_str} — add to meal counts"
    if category == "unknown":
        return f"Unknown extra: {label}{qty_str}{amt_str} — check what needs to be prepared"
    return None


# ── Date parsing from HotelRunner extra "days" field ─────────────────────────

def _parse_extra_dates(days_value: Any) -> list[dt.date]:
    """
    Parse HotelRunner extra 'days' field into a list of specific dates.
    Format examples:
      - "Sep 21 x 1, Sep 29 x 1"  → [2026-09-21, 2026-09-29]
      - "Sun, Mon, Tue, Wed, Thu, Fri, Sat"  → [] (weekly pattern, no specific dates)
      - ["2026-09-21", "2026-09-22"]  → [2026-09-21, 2026-09-22]
    """
    if not days_value:
        return []

    if isinstance(days_value, list):
        dates = []
        for item in days_value:
            d = _try_parse_date(str(item))
            if d:
                dates.append(d)
        return sorted(set(dates))

    text = str(days_value)

    # Pattern: "Sep 21 x 1, Sep 29 x 1"
    matches = re.findall(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})", text, re.I)
    if matches:
        year = dt.date.today().year
        dates = []
        for month_str, day_str in matches:
            try:
                d = dt.datetime.strptime(f"{month_str} {day_str} {year}", "%b %d %Y").date()
                # Adjust year if date is in the past by more than 6 months
                if (dt.date.today() - d).days > 180:
                    d = d.replace(year=year + 1)
                dates.append(d)
            except ValueError:
                continue
        return sorted(set(dates))

    # Pattern: ISO dates in list form
    iso_matches = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if iso_matches:
        dates = []
        for s in iso_matches:
            d = _try_parse_date(s)
            if d:
                dates.append(d)
        return sorted(set(dates))

    return []


def _try_parse_date(value: str) -> dt.date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(value.strip()[:20], fmt).date()
        except ValueError:
            continue
    return None


# ── Main extra classifier ─────────────────────────────────────────────────────

def classify_raw_extra(extra: dict[str, Any]) -> NormalizedExtra | None:
    """
    Classify a single raw HotelRunner extra dict into a NormalizedExtra.
    Returns None for extras that should be silently ignored (e.g. city tax).
    """
    label = str(
        extra.get("name") or extra.get("title") or extra.get("description") or extra.get("label") or ""
    ).strip()
    if not label:
        return None

    category = _category_from_label(label)

    # Silently skip city tax
    if category == "tax":
        return None

    # Quantity
    qty_raw = extra.get("quantity") or extra.get("qty") or extra.get("count")
    try:
        quantity = int(qty_raw) if qty_raw not in (None, "") else None
    except (TypeError, ValueError):
        quantity = None

    # Amount
    amount_raw = extra.get("total") or extra.get("price") or extra.get("amount") or extra.get("base_price")
    try:
        amount = float(str(amount_raw).replace(",", ".")) if amount_raw not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        amount = None

    # Dates
    dates = _parse_extra_dates(extra.get("days") or extra.get("dates") or extra.get("selected_days"))

    operational_note = _operational_note(category, label, quantity, amount, dates)

    return NormalizedExtra(
        raw_label=label,
        category=category,
        quantity=quantity,
        amount=amount,
        dates=dates,
        operational_note=operational_note,
    )


def classify_extras_from_reservation(reservation: dict[str, Any]) -> list[NormalizedExtra]:
    """
    Extract and classify all extras from a raw HotelRunner reservation dict.
    Combines extras at reservation level and room level.
    """
    raw_extras: list[Any] = []
    raw_extras.extend(reservation.get("extras") or [])
    for key in ("extra_adjustments_details", "adjustment_details", "price_adjustments_details"):
        raw_extras.extend(reservation.get(key) or [])
    for room in reservation.get("rooms") or []:
        if isinstance(room, dict):
            raw_extras.extend(room.get("extras") or [])

    results: list[NormalizedExtra] = []
    seen: set[str] = set()
    for raw in raw_extras:
        if not isinstance(raw, dict):
            continue
        ne = classify_raw_extra(raw)
        if ne and ne.raw_label.casefold() not in seen:
            seen.add(ne.raw_label.casefold())
            results.append(ne)
    return results


def enrich_reservation_extras(res: NormalizedReservation, raw_reservation: dict[str, Any]) -> None:
    """Add classified extras to an existing NormalizedReservation in-place."""
    res.extras = classify_extras_from_reservation(raw_reservation)


# ── Transfer date detection ───────────────────────────────────────────────────

def get_transfer_dates_for_today(reservations: list[NormalizedReservation], date: dt.date) -> list[dict]:
    """
    Return all airport transfers happening on the given date.
    Combines: extras with specific dates + arrival/departure transfers from Google Sheet.
    """
    transfers = []

    for res in reservations:
        # From HotelRunner extras with specific dates
        for extra in res.transfer_extras:
            if date in extra.dates:
                direction = "IN" if res.is_arriving_on(date) else "OUT" if res.is_departing_on(date) else "?"
                transfers.append({
                    "guest": res.guest_name,
                    "direction": direction,
                    "detail": extra.raw_label,
                    "amount": extra.amount,
                    "source": "hotelrunner",
                    "reservation_id": res.reservation_id,
                })

        # From Google Sheet arrival/departure info
        if res.source == "google_sheet":
            if res.is_arriving_on(date) and res.arrival_transfer:
                transfers.append({
                    "guest": res.guest_name,
                    "direction": "IN",
                    "detail": res.arrival_transfer,
                    "amount": None,
                    "source": "google_sheet",
                    "reservation_id": res.reservation_id,
                })
            if res.is_departing_on(date) and res.departure_transfer:
                # Only flag if it looks like a real transfer (has time or flight number)
                dep = res.departure_transfer
                if re.search(r"\d{2}[h:]\d{2}|\b[A-Z]{2}\d{3,4}\b", dep):
                    transfers.append({
                        "guest": res.guest_name,
                        "direction": "OUT",
                        "detail": dep,
                        "amount": None,
                        "source": "google_sheet",
                        "reservation_id": res.reservation_id,
                    })

    return transfers


def get_missing_transfer_info(reservations: list[NormalizedReservation], date: dt.date) -> list[dict]:
    """
    Flag Sunrise guests arriving today/tomorrow whose transfer info is missing or says 'no answer'.
    """
    missing = []
    tomorrow = date + dt.timedelta(days=1)

    for res in reservations:
        if res.source != "google_sheet":
            continue
        if not (res.is_arriving_on(date) or res.is_arriving_on(tomorrow)):
            continue
        arr = res.arrival_transfer or ""
        if not arr or "no answer" in arr.casefold():
            missing.append({
                "guest": res.guest_name,
                "arrival_date": res.arrival_date,
                "detail": arr or "not provided",
            })
    return missing
