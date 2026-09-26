"""Fail fast when the generated daily outputs omit known operational records."""
from __future__ import annotations

import datetime as dt
import html
import json
import sys
from pathlib import Path

from hotelrunner_daily_summary import (
    active_stay_lines,
    build_day_summaries,
    build_team_message,
    load_reservation_cache,
    load_room_blocks,
)


def validate_outputs(
    cache_path: Path,
    dashboard_path: Path,
    today: dt.date,
) -> list[str]:
    errors: list[str] = []
    reservations = load_reservation_cache(cache_path)
    lines = active_stay_lines(reservations)
    summaries = build_day_summaries(
        lines,
        start=today,
        days_ahead=1,
        blocks=load_room_blocks(Path("hotelrunner_blocks.json")),
    )
    if not summaries or summaries[0].date != today:
        return ["The generated report does not start with today's date."]

    dashboard = dashboard_path.read_text(encoding="utf-8")
    today_panel = dashboard.split('id="day-0"', 1)[-1].split('id="day-1"', 1)[0]
    team_message = build_team_message(summaries[0])

    for departure in summaries[0].departures:
        name = departure.guest_name.strip()
        if name and name not in team_message:
            errors.append(f"Today's team checkout report omitted reservation {departure.reservation_id}.")
        if name and html.escape(name) not in today_panel:
            errors.append(f"Today's dashboard omitted checkout reservation {departure.reservation_id}.")

    collect_at = today_panel.find("COLLECT TODAY")
    prepare_at = today_panel.find("PREPARE FOR TOMORROW")
    if collect_at >= 0 and prepare_at >= 0 and collect_at > prepare_at:
        errors.append("Checkout payments are not ordered with Today before Prepare for tomorrow.")

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        updated_at = payload.get("updated_at") if isinstance(payload, dict) else None
        if updated_at and dt.datetime.fromisoformat(updated_at).date() != today:
            errors.append("Reservation cache was not refreshed today.")
    except (json.JSONDecodeError, ValueError, TypeError):
        errors.append("Reservation cache freshness could not be verified.")

    return errors


def main() -> None:
    errors = validate_outputs(
        Path("reservations_cache.json"),
        Path("hotelrunner_dashboard.html"),
        dt.date.today(),
    )
    if errors:
        for error in errors:
            print(f"[operations_qa] FAIL: {error}")
        sys.exit(1)
    print("[operations_qa] PASS: today's departures, payment order, and cache freshness are consistent.")


if __name__ == "__main__":
    main()
