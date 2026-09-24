"""
manager_report.py
─────────────────
Builds the 🧠 MANAGER REPORT Telegram message.

Data flow:
  HotelRunner cache  ──┐
  Google Sheet       ──┼──→ NormalizedReservation[] ──→ Rules Engine ──→ Telegram Report
  Surf Schedule      ──┘

This module NEVER modifies the team report (build_whatsapp_block).
It generates a SEPARATE manager-only Telegram message.

Usage:
  python manager_report.py [--date YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

# ── imports ───────────────────────────────────────────────────────────────────

def _setup_path():
    script_dir = Path(__file__).parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

_setup_path()

from data_model import (
    NormalizedReservation, load_config, stayline_to_normalized,
    meals_for_reservation,
)
from extras_engine import (
    classify_extras_from_reservation, get_missing_transfer_info,
)
from conflict_engine import detect_conflicts, RoomConflict, CapacityWarning
from transfer_engine import build_relevant_transfer_records, find_transfer_confirmation_items
from transfer_state import TransferStateStore
from surf_schedule import (
    fetch_conditions, detect_surf_meal_conflicts,
    format_conditions_summary, session_end_time, parse_time, spot_assessment,
)
from google_sheets import load_sunrise_reservations, load_surf_schedule
from alert_state import AlertStateStore

try:
    from hotelrunner_daily_summary import (
        load_reservation_cache,
        active_stay_lines,
    )
except ImportError as exc:
    sys.exit(f"[manager_report] Cannot import hotelrunner_daily_summary: {exc}")


# ── Constants ─────────────────────────────────────────────────────────────────

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_all_reservations(cache_path: Path, date: dt.date) -> list[NormalizedReservation]:
    """Load and normalize reservations from all sources."""
    # HotelRunner
    raw_reservations = load_reservation_cache(cache_path)
    raw_by_id = {str(r.get("reservation_id") or ""): r for r in raw_reservations}
    stay_lines = active_stay_lines(raw_reservations)

    hr_reservations: list[NormalizedReservation] = []
    for line in stay_lines:
        raw = raw_by_id.get(line.reservation_id)
        res = stayline_to_normalized(line, raw)
        # Attach classified extras from raw data
        if raw:
            res.extras = classify_extras_from_reservation(raw)
        hr_reservations.append(res)

    # Google Sheet (Sunrise)
    try:
        sunrise_reservations = load_sunrise_reservations()
    except Exception as exc:
        print(f"[manager_report] Sunrise data unavailable: {exc}")
        sunrise_reservations = []

    all_reservations = hr_reservations + sunrise_reservations
    return all_reservations


# ── Occupancy section ─────────────────────────────────────────────────────────

def _build_occupancy(reservations: list[NormalizedReservation], date: dt.date) -> str:
    cfg = load_config()
    houses_cfg = cfg.get("houses", {})

    lines = ["🏠 OCCUPANCY"]
    total_guests = 0

    for house_name, house_cfg in houses_cfg.items():
        capacity = house_cfg.get("total_capacity", "?")
        in_house = [r for r in reservations if r.house == house_name and r.is_active_on(date)]
        guest_count = sum(r.guest_count for r in in_house)
        total_guests += guest_count
        emoji = house_cfg.get("emoji", "")
        lines.append(f"{emoji} {house_name}: {guest_count} / {capacity}")

    lines.append(f"Total: {total_guests} guests")
    return "\n".join(lines)


# ── Meals section ─────────────────────────────────────────────────────────────

def _build_meals(reservations: list[NormalizedReservation], date: dt.date) -> str:
    from meal_engine import compute_meal_entitlements, summarize_dinner, dinner_preparation_notice

    entitlements = compute_meal_entitlements(reservations, date)

    # Totals by meal type
    totals = {"breakfast": 0, "lunch": 0, "dinner": 0}
    for e in entitlements:
        if e.meal in totals:
            totals[e.meal] += e.count

    # Dinner detail with per-house breakdown
    dinner_summary = summarize_dinner(entitlements)
    dinner_total = dinner_summary["total"]
    by_house = dinner_summary["by_house"]
    house_parts = " | ".join(f"{h}: {n}" for h, n in sorted(by_house.items()) if n > 0)

    lines = ["🍽️ MEALS"]
    lines.append(f"Breakfast: {totals['breakfast']}")
    lines.append(f"Lunch:     {totals['lunch']}")

    dinner_line = f"Dinner:    {dinner_total}"
    if house_parts:
        dinner_line += f"  ({house_parts})"
    lines.append(dinner_line)

    # Preparation notice (auto-appears when >13 guests)
    notice = dinner_preparation_notice(dinner_total)
    if notice:
        lines.append(notice)

    return "\n".join(lines)


# ── Diet/Allergy section (Sunrise only) ──────────────────────────────────────

def _build_diet_notes(reservations: list[NormalizedReservation], date: dt.date) -> str | None:
    notes = []
    for res in reservations:
        if not res.is_active_on(date):
            continue
        parts = []
        if res.diet:
            parts.append(f"Diet: {res.diet}")
        if res.allergies:
            parts.append(f"Allergies: {res.allergies}")
        if res.medical_notes:
            parts.append(f"Note: {res.medical_notes}")
        if parts:
            notes.append(f"{res.guest_name} ({res.house}): " + " | ".join(parts))
    if not notes:
        return None
    lines = ["🥗 DIETARY / HEALTH NOTES"]
    lines.extend(f"• {n}" for n in notes)
    return "\n".join(lines)


# ── Surf section ──────────────────────────────────────────────────────────────

def _build_surf(sessions: list[dict], conditions: dict, surf_conflicts: list[dict]) -> str:
    cfg = load_config()
    lines = ["🌊 SURF"]

    if not sessions:
        lines.append("No surf sessions scheduled for today.")
        lines.append("(Add sessions to 'Surf Schedule' tab in Google Sheet)")
    else:
        for s in sessions:
            start = s.get("time", "?")
            level = s.get("level", "")
            spot = s.get("spot", "")
            count = s.get("guest_count", 0)
            end = parse_time(start)
            if end:
                end_time = session_end_time(end, cfg)
                end_str = f" → ~{end_time.strftime('%H:%M')}"
            else:
                end_str = ""
            guest_str = f" · {count} guests" if count else ""
            spot_str = f" · {spot}" if spot else ""
            lines.append(f"{start}{end_str} — {level}{spot_str}{guest_str}")

    # Conditions summary
    if conditions.get("available"):
        lines.append("")
        lines.append(f"📡 {format_conditions_summary(conditions)}")
        # Spot assessment (info only)
        spots_used = {s.get("spot") for s in sessions if s.get("spot")}
        for spot in sorted(spots_used):
            lines.append(f"  {spot_assessment(spot, conditions)}")

    # Surf/meal conflicts
    if surf_conflicts:
        lines.append("")
        for conflict in surf_conflicts:
            lines.append(f"⚠️ {conflict['title']}")

    return "\n".join(lines)


# ── Transfers section ─────────────────────────────────────────────────────────

def _build_transfers(reservations: list[NormalizedReservation], date: dt.date) -> str:
    store = TransferStateStore()
    transfers = build_relevant_transfer_records(reservations, date, store)
    missing = get_missing_transfer_info(reservations, date)
    confirmations = find_transfer_confirmation_items(reservations, date)

    lines = ["🚐 TRANSFERS"]

    if not transfers and not missing and not confirmations:
        lines.append("No transfers today or tomorrow.")
        return "\n".join(lines)

    today_transfers = [t for t in transfers if t.date == date]
    tomorrow_transfers = [t for t in transfers if t.date == date + dt.timedelta(days=1)]

    def append_records(label: str, records) -> None:
        if not records:
            return
        lines.append(label)
        for record in records:
            arrow = "→ IN" if record.is_arrival else "← OUT"
            status = "sent" if record.status == "sent" else "ready" if record.status == "ready_to_send" else "needs info"
            detail = (
                f"Flight {record.flight_number or 'missing'} · {record.airport or 'Agadir airport'}"
                if record.is_arrival
                else f"Pickup {record.pickup_time or 'missing'} · {record.destination or 'Agadir airport'}"
            )
            lines.append(f"  {arrow}: {record.guest_name} · {status} · {detail}")

    append_records("TODAY:", today_transfers)
    append_records("TOMORROW:", tomorrow_transfers)

    if missing:
        lines.append("")
        for m in missing:
            arr = m["arrival_date"].strftime("%d %b") if m.get("arrival_date") else ""
            lines.append(f"🟠 {m['guest']} (arriving {arr}): transfer info missing")

    if confirmations:
        lines.append("")
        for item in confirmations:
            lines.append(f"🟠 {item.description}")

    return "\n".join(lines)


# ── Arrivals/Departures summary ───────────────────────────────────────────────

def _build_movements(reservations: list[NormalizedReservation], date: dt.date) -> str:
    tomorrow = date + dt.timedelta(days=1)
    arr_today = [r for r in reservations if r.is_arriving_on(date)]
    dep_today = [r for r in reservations if r.is_departing_on(date)]
    arr_tomorrow = [r for r in reservations if r.is_arriving_on(tomorrow)]
    dep_tomorrow = [r for r in reservations if r.is_departing_on(tomorrow)]

    lines = ["🟢 INFO"]

    def fmt_list(label: str, items: list[NormalizedReservation]) -> None:
        if items:
            names = ", ".join(r.guest_name for r in items[:5])
            extra = f" +{len(items)-5} more" if len(items) > 5 else ""
            lines.append(f"{label}: {len(items)} ({names}{extra})")

    fmt_list("Arrivals today", arr_today)
    fmt_list("Departures today", dep_today)
    fmt_list("Arrivals tomorrow", arr_tomorrow)
    fmt_list("Departures tomorrow", dep_tomorrow)

    # Unpaid balances
    unpaid = [r for r in reservations
              if r.has_unpaid_balance and (r.is_arriving_on(date) or r.is_arriving_on(tomorrow))]
    if unpaid:
        lines.append("")
        lines.append("💳 UNPAID BALANCES (arriving soon):")
        for r in unpaid:
            lines.append(f"  🟠 {r.guest_name}: owes €{r.unpaid_amount:.0f}")

    # Sunrise guests today
    sunrise_today = [r for r in reservations
                     if r.house == "Sunrise" and r.is_active_on(date)]
    if sunrise_today:
        lines.append("")
        lines.append("🌅 SUNRISE GUESTS TODAY:")
        for r in sunrise_today:
            parts = [r.guest_name, r.room]
            if r.surf_level:
                parts.append(r.surf_level)
            if r.meal_plan:
                parts.append(r.meal_plan)
            lines.append("  • " + " · ".join(p for p in parts if p))

    return "\n".join(lines)


# ── Issues: conflicts + extras ────────────────────────────────────────────────

SEVERITY_EMOJI = {
    "urgent": "🔴",
    "action_required": "🔴",
    "check": "🟠",
    "info": "🟢",
}


def _build_conflicts_section(
    room_conflicts: list[RoomConflict],
    capacity_warnings: list[CapacityWarning],
    alert_store: AlertStateStore,
) -> tuple[list[str], list[str], list[str]]:
    """Returns (urgent_lines, check_lines, info_lines)."""
    urgent: list[str] = []
    check: list[str] = []
    info: list[str] = []

    for conflict in room_conflicts:
        sev = conflict.escalated_severity
        if not alert_store.should_notify(conflict.alert_id, sev, conflict.room, "ROOM_CONFLICT"):
            continue
        emoji = SEVERITY_EMOJI.get(sev, "🟠")
        days = conflict.days_until_arrival
        days_str = f"{days} days until arrival" if days > 0 else "ARRIVAL TODAY"
        block = (
            f"{emoji} ROOM CONFLICT — {conflict.room} ({conflict.house})\n"
            f"   {conflict.conflict_start.strftime('%d %b')} → {conflict.conflict_end.strftime('%d %b')}\n"
            f"   {conflict.description}\n"
            f"   {days_str}\n"
            f"   → {conflict.recommended_action}"
        )
        if sev in ("urgent", "action_required"):
            urgent.append(block)
        else:
            check.append(block)

    for warning in capacity_warnings:
        block = (
            f"🔴 CAPACITY EXCEEDED — {warning.room} ({warning.house})\n"
            f"   {warning.date.strftime('%d %b')}: {warning.booked} guests / {warning.capacity} beds\n"
            f"   Overflow: {warning.overflow} guest(s)"
        )
        urgent.append(block)

    return urgent, check, info


def _build_extras_section(reservations: list[NormalizedReservation], date: dt.date) -> tuple[list[str], list[str]]:
    """Returns (check_items, info_items) for extras."""
    check_items: list[str] = []
    info_items: list[str] = []

    processed: set[str] = set()
    for res in reservations:
        if not res.is_active_on(date) and not res.is_arriving_on(date):
            continue
        for extra in res.extras:
            key = f"{res.reservation_id}:{extra.raw_label}"
            if key in processed:
                continue
            processed.add(key)
            if extra.category == "unknown":
                check_items.append(
                    f"🟠 UNKNOWN EXTRA — CHECK\n"
                    f"   Guest: {res.guest_name}\n"
                    f"   Extra: {extra.raw_label}"
                    + (f"\n   Amount: €{extra.amount:.0f}" if extra.amount else "")
                    + "\n   → Verify what needs to be prepared"
                )
            elif extra.category in ("surf_equipment", "surf_lesson") and extra.operational_note:
                note = extra.operational_note
                info_items.append(f"🟢 {extra.raw_label} ({res.guest_name})\n   → {note}")

    return check_items, info_items


# ── Full Manager Report ───────────────────────────────────────────────────────

def build_manager_report(
    cache_path: Path,
    date: dt.date,
    alert_store: AlertStateStore | None = None,
) -> str:
    if alert_store is None:
        alert_store = AlertStateStore()

    # Load all data
    reservations = load_all_reservations(cache_path, date)
    sessions = load_surf_schedule(date)
    conditions = fetch_conditions(date)

    # Run engines
    room_conflicts, capacity_warnings = detect_conflicts(reservations, check_date=date, lookahead_days=30)
    surf_conflicts = detect_surf_meal_conflicts(sessions, reservations, date)

    # Track which alerts are still active
    active_ids = {c.alert_id for c in room_conflicts}
    newly_resolved = alert_store.resolve_missing(active_ids)

    # Build sections
    day_name = date.strftime("%A %d %B")
    sections = [f"🧠 MANAGER REPORT\n📅 {day_name}"]

    sections.append(DIVIDER + "\n" + _build_occupancy(reservations, date))
    sections.append(DIVIDER + "\n" + _build_surf(sessions, conditions, surf_conflicts))
    sections.append(DIVIDER + "\n" + _build_meals(reservations, date))

    diet = _build_diet_notes(reservations, date)
    if diet:
        sections.append(DIVIDER + "\n" + diet)

    sections.append(DIVIDER + "\n" + _build_transfers(reservations, date))

    # Conflicts
    urgent_items, check_items_conflict, _ = _build_conflicts_section(
        room_conflicts, capacity_warnings, alert_store
    )
    extra_checks, extra_info = _build_extras_section(reservations, date)
    check_items = check_items_conflict + extra_checks

    # Surf/meal conflicts
    for sc in surf_conflicts:
        sev = sc.get("severity", "check")
        emoji = SEVERITY_EMOJI.get(sev, "🟠")
        block = (
            f"{emoji} {sc['title']}\n"
            f"   {sc['description']}\n"
            f"   → {sc['recommended_action']}"
        )
        if sev == "action_required":
            urgent_items.append(block)
        else:
            check_items.append(block)

    if urgent_items:
        sections.append(DIVIDER + "\n🔴 ACTION REQUIRED\n\n" + "\n\n".join(urgent_items))
    if check_items:
        sections.append(DIVIDER + "\n🟠 CHECK\n\n" + "\n".join(check_items))

    info_section = _build_movements(reservations, date)
    if extra_info:
        info_section += "\n\n" + "\n".join(extra_info)
    if newly_resolved:
        info_section += "\n\n✅ RESOLVED:\n" + "\n".join(f"• {r}" for r in newly_resolved)
    sections.append(DIVIDER + "\n" + info_section)

    # Save alert state
    alert_store.save()

    return "\n\n".join(sections)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Build and optionally send the Manager Report")
    parser.add_argument("--date", help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--cache-file", default="reservations_cache.json")
    parser.add_argument("--dry-run", action="store_true", help="Print without sending")
    parser.add_argument("--send", action="store_true", help="Send via Telegram")
    args = parser.parse_args()

    target_date = dt.date.today()
    if args.date:
        try:
            target_date = dt.date.fromisoformat(args.date)
        except ValueError:
            sys.exit("Invalid --date. Use YYYY-MM-DD.")

    cache_path = Path(args.cache_file)
    if not cache_path.exists():
        sys.exit(f"Cache not found: {cache_path}. Run hotelrunner_daily_summary.py first.")

    # Load .env
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        from hotelrunner_daily_summary import load_dotenv
        load_dotenv(env_path)

    alert_store = AlertStateStore()
    report = build_manager_report(cache_path, target_date, alert_store)

    if args.dry_run or not args.send:
        out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
        out.write(report.encode("utf-8"))
        out.write(b"\n")
        out.flush()
        return

    if args.send:
        import os
        import json as _json
        import urllib.request as _req
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required for --send")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = _json.dumps({"chat_id": chat_id, "text": report}).encode("utf-8")
        req = _req.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with _req.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        if result.get("ok"):
            print(f"[manager_report] Sent for {target_date}")
        else:
            print(f"[manager_report] Telegram error: {result}")


if __name__ == "__main__":
    main()
