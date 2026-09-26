import datetime as dt
import json
from pathlib import Path

from hotelrunner_daily_summary import DaySummary, RunStatus, StayLine, build_dashboard_html
from operations_qa import validate_outputs


TODAY = dt.date(2026, 9, 26)


def _checkout() -> StayLine:
    return StayLine(
        reservation_id="checkout-1",
        hr_number="HR-QA",
        guest_name="Example Checkout",
        channel="Online",
        room_name="Bay",
        bed_number="6500",
        arrival=TODAY - dt.timedelta(days=3),
        departure=TODAY,
        adults=2,
        children=0,
        meal_plan="Bed And Breakfast",
    )


def _write_cache(path: Path) -> None:
    line = _checkout()
    reservation = {
        "reservation_id": line.reservation_id,
        "hr_number": line.hr_number,
        "guest": line.guest_name,
        "channel": "online",
        "state": "confirmed",
        "checkin_date": line.arrival.isoformat(),
        "checkout_date": line.departure.isoformat(),
        "total_adult": 2,
        "rooms": [{"name": "Bay", "room_id": "6500", "total_adult": 2}],
    }
    path.write_text(json.dumps({"updated_at": f"{TODAY}T07:00:00", "reservations": [reservation]}), encoding="utf-8")


def test_qa_passes_when_today_checkout_is_in_dashboard(tmp_path, monkeypatch):
    import google_sheets

    monkeypatch.setattr(google_sheets, "load_sunrise_reservations", lambda: [])
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "reservations_cache.json"
    dashboard = tmp_path / "hotelrunner_dashboard.html"
    _write_cache(cache)
    dashboard.write_text(
        build_dashboard_html(
            [DaySummary(date=TODAY, departures=[_checkout()])],
            generated_at=dt.datetime(2026, 9, 26, 7, 0),
            run_status=RunStatus(True, "Ready", "Ready", ()),
        ),
        encoding="utf-8",
    )

    assert validate_outputs(cache, dashboard, TODAY) == []


def test_qa_fails_when_today_checkout_is_missing_from_dashboard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "reservations_cache.json"
    dashboard = tmp_path / "hotelrunner_dashboard.html"
    _write_cache(cache)
    dashboard.write_text('<section id="day-0">No departures</section>', encoding="utf-8")

    errors = validate_outputs(cache, dashboard, TODAY)

    assert any("dashboard omitted checkout" in error for error in errors)
