import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_model import NormalizedExtra, NormalizedReservation
from hotelrunner_daily_summary import (
    DaySummary,
    StayLine,
    build_dashboard_html,
    build_whatsapp_block,
    build_whatsapp_block_with_sunrise_sheet,
    team_house,
)
from transfer_engine import (
    build_relevant_transfer_records,
    build_transfer_records,
    find_transfer_confirmation_items,
)
from transfer_state import TransferStateStore


TODAY = dt.date(2026, 9, 24)
TOMORROW = TODAY + dt.timedelta(days=1)


def _store():
    store = TransferStateStore.__new__(TransferStateStore)
    store._records = {}
    store.path = Path("nonexistent_test_transfer.json")
    return store


def _res(rid, arrival, departure, *, meal_plan="All Inclusive", extras=None):
    return NormalizedReservation(
        reservation_id=rid,
        source="hotelrunner",
        guest_name=f"Guest {rid}",
        adults=1,
        house="Olas",
        room="RDC1",
        arrival_date=arrival,
        departure_date=departure,
        meal_plan=meal_plan,
        extras=extras or [],
    )


def test_mid_stay_old_transfer_extra_is_not_actionable_today():
    old_extra = NormalizedExtra(
        raw_label="Airport Transfer",
        category="transfer",
        dates=[dt.date(2026, 9, 21)],
    )
    guest = _res("midstay-old", dt.date(2026, 9, 20), dt.date(2026, 9, 29), extras=[old_extra])

    records = build_relevant_transfer_records([guest], TODAY, _store())
    confirmations = find_transfer_confirmation_items([guest], TODAY)

    assert records == []
    assert confirmations == []


def test_tomorrow_departure_transfer_is_relevant_today():
    guest = _res("dep-tomorrow", dt.date(2026, 9, 20), TOMORROW)

    records = build_relevant_transfer_records([guest], TODAY, _store())

    assert len(records) == 1
    assert records[0].direction == "departure"
    assert records[0].date == TOMORROW


def test_today_departure_transfer_is_relevant_today():
    guest = _res("dep-today", dt.date(2026, 9, 20), TODAY)

    records = build_relevant_transfer_records([guest], TODAY, _store())

    assert len(records) == 1
    assert records[0].direction == "departure"
    assert records[0].date == TODAY


def test_unknown_mid_stay_transfer_date_requires_confirmation():
    dated_extra = NormalizedExtra(
        raw_label="Airport Transfer",
        category="transfer",
        dates=[TODAY],
    )
    guest = _res("unknown-date", dt.date(2026, 9, 20), dt.date(2026, 9, 29), extras=[dated_extra])

    records = build_transfer_records([guest], TODAY, _store())
    confirmations = find_transfer_confirmation_items([guest], TODAY)

    assert records == []
    assert len(confirmations) == 1
    assert "does not match arrival or departure" in confirmations[0].description


def test_transfer_records_deduplicate_same_reservation_stay_lines():
    first_line = _res("split-res", dt.date(2026, 9, 20), TODAY)
    second_line = _res("split-res", dt.date(2026, 9, 20), TODAY)
    second_line.room = "RDC2"

    records = build_transfer_records([first_line, second_line], TODAY, _store())

    assert len(records) == 1
    assert records[0].reservation_id == "split-res"
    assert records[0].direction == "departure"


def test_transfer_records_deduplicate_same_guest_split_room_lines():
    first_line = _res("split-a", dt.date(2026, 9, 20), TODAY)
    second_line = _res("split-b", dt.date(2026, 9, 20), TODAY)
    first_line.guest_name = "Split Guest"
    second_line.guest_name = "  Split   Guest  "
    second_line.room = "RDC2"

    records = build_transfer_records([first_line, second_line], TODAY, _store())

    assert len(records) == 1
    assert records[0].guest_name == "Split Guest"
    assert records[0].direction == "departure"


def test_run_morning_refreshes_dashboard_and_sends_single_manager_flow():
    content = Path("run_morning.bat").read_text(encoding="utf-8")

    assert "--no-dashboard" not in content
    assert "hotelrunner_daily_summary.py --max-pages 15 --page-delay 1" in content
    assert "telegram_send.py --mode full --team-only" in content
    assert "manager_report.py --send" in content


def test_telegram_full_team_only_dry_run_suppresses_legacy_manager_block(monkeypatch, capsys):
    import telegram_send

    monkeypatch.setattr(sys, "argv", ["telegram_send.py", "--mode", "full", "--team-only", "--dry-run"])
    monkeypatch.setattr(telegram_send, "load_dotenv", lambda path: None)
    monkeypatch.setattr(telegram_send, "get_env", lambda key: "dummy")
    monkeypatch.setattr(telegram_send, "build_messages_from_cache", lambda cache_path, target_date: ("TEAM REPORT", "LEGACY MANAGER"))
    monkeypatch.setattr(Path, "exists", lambda self: True)

    telegram_send.main()

    out = capsys.readouterr().out
    assert "TEAM REPORT" in out
    assert "LEGACY MANAGER" not in out


def test_sunrise_hotelrunner_room_groups_under_sunrise():
    line = StayLine(
        reservation_id="hr-sunrise",
        hr_number="HR1",
        guest_name="HR Sunrise Guest",
        channel="HotelRunner",
        room_name="SunRise 2 #9300-1",
        bed_number="9300-1",
        arrival=TODAY,
        departure=TOMORROW,
        adults=1,
        children=0,
        meal_plan="Bed And Breakfast",
    )

    assert team_house(line) == "Sunrise"


def test_sunrise_sheet_only_checkin_gets_check_add_hr_reminder():
    sheet_guest = NormalizedReservation(
        reservation_id="sheet-alicja",
        source="google_sheet",
        guest_name="Alicja Okuniewicz",
        adults=1,
        house="Sunrise",
        room="Sunrise 5",
        arrival_date=TODAY,
        departure_date=TOMORROW,
        meal_plan="Half Board",
        surf_level="Beginner+",
    )
    summary = DaySummary(date=TODAY)

    msg = build_whatsapp_block_with_sunrise_sheet(summary, [sheet_guest])

    assert "🌅 Sunrise" in msg
    assert "Alicja Okuniewicz x1 -> Sunrise 5 · Beginner+ · Half Board · ⚠️ check/add HR" in msg


def test_sunrise_sheet_guest_matching_hotelrunner_is_not_duplicated():
    hr_guest = StayLine(
        reservation_id="hr-alicja",
        hr_number="HR2",
        guest_name="Alicja Okuniewicz",
        channel="HotelRunner",
        room_name="SunRise 5 #9500",
        bed_number="9500",
        arrival=TODAY,
        departure=TOMORROW,
        adults=1,
        children=0,
        meal_plan="Half Board",
    )
    sheet_guest = NormalizedReservation(
        reservation_id="sheet-alicja",
        source="google_sheet",
        guest_name="Alicja Okuniewicz",
        adults=1,
        house="Sunrise",
        room="Sunrise 5",
        arrival_date=TODAY,
        departure_date=TOMORROW,
        meal_plan="Half Board",
        surf_level="Beginner+",
    )
    summary = DaySummary(date=TODAY, arrivals=[hr_guest])

    msg = build_whatsapp_block_with_sunrise_sheet(summary, [sheet_guest])

    assert msg.count("Alicja Okuniewicz") == 1
    assert "⚠️ check/add HR" not in msg


def test_checkins_show_bed_and_breakfast_meal_plan():
    arrival = StayLine(
        reservation_id="bb-arrival",
        hr_number="HR-BB",
        guest_name="Breakfast Guest",
        channel="Direct",
        room_name="Mixed dorm",
        bed_number="03",
        arrival=TODAY,
        departure=TOMORROW,
        adults=1,
        children=0,
        meal_plan="Bed And Breakfast",
    )

    msg = build_whatsapp_block(DaySummary(date=TODAY, arrivals=[arrival]))

    assert "Breakfast Guest x1 -> dorm (breakfast)" in msg


def test_dashboard_html_includes_sunrise_sheet_check_add_hr(monkeypatch):
    import google_sheets

    sheet_guest = NormalizedReservation(
        reservation_id="sheet-alicja",
        source="google_sheet",
        guest_name="Alicja Okuniewicz",
        adults=1,
        house="Sunrise",
        room="Sunrise 5",
        arrival_date=TODAY,
        departure_date=TOMORROW,
        meal_plan="Half Board",
        surf_level="Beginner+",
    )
    monkeypatch.setattr(google_sheets, "load_sunrise_reservations", lambda: [sheet_guest])

    html = build_dashboard_html([DaySummary(date=TODAY)], generated_at=dt.datetime(2026, 9, 24, 9, 0))

    assert "Alicja Okuniewicz x1 -&gt; Sunrise 5" in html
    assert "Beginner+" in html
    assert "Half Board" in html
    assert "⚠️ check/add HR" in html


def test_dashboard_meals_include_hotelrunner_half_board_formula_extra(monkeypatch):
    import google_sheets

    monkeypatch.setattr(google_sheets, "load_sunrise_reservations", lambda: [])
    guest = StayLine(
        reservation_id="paid-dinner",
        hr_number="HR3",
        guest_name="Paid Dinner Guest",
        channel="Online",
        room_name="Room 7",
        bed_number="07",
        arrival=TODAY,
        departure=TOMORROW,
        adults=1,
        children=0,
        meal_plan="Bed And Breakfast",
        extras=("Half Board formula x1 120.0",),
    )
    summary = DaySummary(date=TODAY, in_house=[guest])

    html = build_dashboard_html([summary], generated_at=dt.datetime(2026, 9, 24, 9, 0))

    assert "Dinner</span>\n              <span class=\"text-xl font-bold text-white\">1</span>" in html
    assert "Paid Dinner Guest" in html
    assert "Half Board formula" in html


def test_dashboard_meals_include_active_sunrise_sheet_guests(monkeypatch):
    import google_sheets

    sheet_guest = NormalizedReservation(
        reservation_id="sheet-sunrise-dinner",
        source="google_sheet",
        guest_name="Sunrise Dinner Guest",
        adults=1,
        house="Sunrise",
        room="Sunrise 4",
        arrival_date=TODAY - dt.timedelta(days=1),
        departure_date=TOMORROW,
        meal_plan="Half Board",
        surf_level="Beginner",
    )
    monkeypatch.setattr(google_sheets, "load_sunrise_reservations", lambda: [sheet_guest])

    html = build_dashboard_html([DaySummary(date=TODAY)], generated_at=dt.datetime(2026, 9, 24, 9, 0))

    assert "Dinner</span>\n              <span class=\"text-xl font-bold text-white\">1</span>" in html
    assert "Sunrise Dinner Guest" in html
    assert "Sunrise: 1" in html
    assert "⚠️ check/add HR" in html

