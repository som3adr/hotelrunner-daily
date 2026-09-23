"""
transfer_engine.py
──────────────────
Transfer detection, status classification and driver message generation.

How a transfer is detected (exactly per master plan):
  1. All Inclusive meal plan → transfer included
  2. Notes → if transfer request written in note
  3. Additional Fees / Extras → explicit transfer extra
  Do NOT infer transfer solely from arrival/departure.

Status flow: needs_info → ready_to_send → sent
  Arrival ready:   flight_number AND airport populated
  Departure ready: pickup_time AND destination populated
  Sent: marked manually in TransferStateStore
"""
from __future__ import annotations

import datetime as dt
import re

from data_model import NormalizedReservation, TransferRecord
from transfer_state import TransferStateStore


# ── Transfer detection helpers ────────────────────────────────────────────────

_FLIGHT_RE = re.compile(r"\b([A-Z]{2}\d{3,4}|[A-Z]\d{4})\b")
_TIME_RE = re.compile(r"\b(\d{1,2})[h:](\d{2})\b")
_TRANSFER_NOTE_KEYWORDS = [
    "transfer", "taxi", "pickup", "pick up", "pick-up",
    "shuttle", "navette", "aéroport", "aeroporto", "airport",
    "arrivée", "arrivee", "départ", "depart",
]


def _note_mentions_transfer(note: str) -> bool:
    t = note.casefold()
    return any(kw in t for kw in _TRANSFER_NOTE_KEYWORDS)


def _is_all_inclusive(res: NormalizedReservation) -> bool:
    plan = res.meal_plan.casefold()
    return "all inclusive" in plan or "all-inclusive" in plan


def detect_transfer_entitlement(res: NormalizedReservation) -> bool:
    """True if this reservation includes a transfer service."""
    # 1. All Inclusive
    if _is_all_inclusive(res):
        return True
    # 2. Transfer extra
    if res.transfer_extras:
        return True
    # 3. Note mentions transfer
    if any(_note_mentions_transfer(n) for n in res.notes):
        return True
    return False


def _parse_flight_from_text(text: str) -> str:
    """Extract flight number like HV6491 or AT425 from text."""
    m = _FLIGHT_RE.search(text.upper())
    return m.group(1) if m else ""


def _parse_time_from_text(text: str) -> str:
    """Extract HH:MM pickup time from text."""
    m = _TIME_RE.search(text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def _parse_airport_from_text(text: str) -> str:
    """Extract airport name from text."""
    t = text.casefold()
    if "agadir" in t or "essa" in t or " aga" in t:
        return "Agadir airport"
    if "marrakech" in t or "marrakesh" in t or " rak" in t:
        return "Marrakech airport"
    if "casablanca" in t or " cmn" in t:
        return "Casablanca airport"
    return ""


def _classify_status(
    record: TransferRecord,
    store: TransferStateStore,
) -> str:
    """Determine needs_info | ready_to_send | sent."""
    if store.is_sent(record.reservation_id, record.direction, record.date):
        return "sent"
    if record.is_arrival:
        if record.flight_number and record.airport:
            return "ready_to_send"
        return "needs_info"
    else:  # departure
        if record.pickup_time and record.destination:
            return "ready_to_send"
        return "needs_info"


# ── Build transfer records ────────────────────────────────────────────────────

def build_transfer_records(
    reservations: list[NormalizedReservation],
    date: dt.date,
    store: TransferStateStore | None = None,
) -> list[TransferRecord]:
    """
    Build TransferRecord objects for all guests arriving or departing on `date`.
    Only guests with a transfer entitlement are included.
    """
    if store is None:
        store = TransferStateStore()

    records: list[TransferRecord] = []

    for res in reservations:
        if not detect_transfer_entitlement(res):
            continue

        # Arrivals on this date
        if res.is_arriving_on(date):
            record = _build_arrival_record(res, date)
            record.status = _classify_status(record, store)
            records.append(record)

        # Departures on this date
        if res.is_departing_on(date):
            record = _build_departure_record(res, date)
            record.status = _classify_status(record, store)
            records.append(record)

    return records


def _build_arrival_record(res: NormalizedReservation, date: dt.date) -> TransferRecord:
    """Build arrival TransferRecord — extract flight/airport from all sources."""
    flight = ""
    airport = ""

    # Check arrival_transfer field (Google Sheet / notes)
    if res.arrival_transfer:
        flight = flight or _parse_flight_from_text(res.arrival_transfer)
        airport = airport or _parse_airport_from_text(res.arrival_transfer)

    # Check notes
    for note in res.notes:
        flight = flight or _parse_flight_from_text(note)
        airport = airport or _parse_airport_from_text(note)

    # Check transfer extras
    for extra in res.transfer_extras:
        flight = flight or _parse_flight_from_text(extra.raw_label)
        airport = airport or _parse_airport_from_text(extra.raw_label)
        if extra.operational_note:
            airport = airport or _parse_airport_from_text(extra.operational_note)

    # Determine raw source
    raw_source = "extra" if res.transfer_extras else (
        "all_inclusive" if _is_all_inclusive(res) else "note"
    )

    return TransferRecord(
        reservation_id=res.reservation_id,
        guest_name=res.guest_name,
        house=res.house,
        direction="arrival",
        date=date,
        passenger_count=res.guest_count or 1,
        flight_number=flight,
        airport=airport or "Agadir airport",  # default for Imsouane ops
        raw_source=raw_source,
    )


def _build_departure_record(res: NormalizedReservation, date: dt.date) -> TransferRecord:
    """Build departure TransferRecord — extract pickup time/destination."""
    pickup_time = ""
    destination = ""

    # Check departure_transfer field (Google Sheet)
    if res.departure_transfer:
        pickup_time = pickup_time or _parse_time_from_text(res.departure_transfer)
        destination = destination or _parse_airport_from_text(res.departure_transfer)

    # Check notes
    for note in res.notes:
        pickup_time = pickup_time or _parse_time_from_text(note)
        destination = destination or _parse_airport_from_text(note)

    # Check transfer extras
    for extra in res.transfer_extras:
        pickup_time = pickup_time or _parse_time_from_text(extra.raw_label)
        destination = destination or _parse_airport_from_text(extra.raw_label)

    raw_source = "extra" if res.transfer_extras else (
        "all_inclusive" if _is_all_inclusive(res) else "note"
    )

    return TransferRecord(
        reservation_id=res.reservation_id,
        guest_name=res.guest_name,
        house=res.house,
        direction="departure",
        date=date,
        passenger_count=res.guest_count or 1,
        pickup_time=pickup_time,
        destination=destination or "Agadir airport",
        raw_source=raw_source,
    )


# ── Driver message formatting ─────────────────────────────────────────────────

_MONTH_FR = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre"
]


def format_driver_message(record: TransferRecord) -> str:
    """
    Generate taxi driver message in the established format.

    Arrival:
      Arrivée *Olas*
      20 septembre 2026
      *Guest Name* X1
      Flight number: HV6491
      Agadir airport

    Departure:
      Depart *Tide*
      18 septembre 2026
      *Guest Name* X2
      Time: 15:30
      Agadir airport
    """
    date_str = f"{record.date.day} {_MONTH_FR[record.date.month]} {record.date.year}"
    pax = f"X{record.passenger_count}"
    guest_bold = f"*{record.guest_name}*"

    if record.is_arrival:
        lines = [
            f"Arrivée *{record.house}*",
            date_str,
            f"{guest_bold} {pax}",
        ]
        if record.flight_number:
            lines.append(f"Flight number: {record.flight_number}")
        else:
            lines.append("Flight number: (missing)")
        lines.append(record.airport or "Agadir airport")
    else:
        lines = [
            f"Depart *{record.house}*",
            date_str,
            f"{guest_bold} {pax}",
        ]
        if record.pickup_time:
            lines.append(f"Time: {record.pickup_time}")
        else:
            lines.append("Time: (missing)")
        lines.append(record.destination or "Agadir airport")

    return "\n".join(lines)
