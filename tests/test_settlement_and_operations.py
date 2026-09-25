import datetime as dt
import json
from urllib.error import HTTPError

from data_model import NormalizedExtra, NormalizedReservation, merge_cross_source_duplicates
from meal_engine import compute_meal_entitlements, format_dinner_team_message
from settlement_engine import build_settlement_reminders


TODAY = dt.date(2026, 9, 25)


def reservation(**overrides):
    values = dict(
        reservation_id="r-1",
        source="hotelrunner",
        guest_name="Test Guest",
        adults=1,
        house="Olas",
        room="RDC1",
        arrival_date=dt.date(2026, 9, 20),
        departure_date=dt.date(2026, 9, 26),
        channel="Online",
        total_amount=600,
        paid_amount=300,
        payment_record_count=1,
    )
    values.update(overrides)
    return NormalizedReservation(**values)


def test_partial_payment_is_reminded_one_day_before_checkout():
    reminders = build_settlement_reminders([reservation()], TODAY)
    assert len(reminders) == 1
    assert reminders[0].remaining_amount == 300
    assert reminders[0].timing == "tomorrow"
    assert reminders[0].payment_status == "partial"


def test_no_online_payment_record_requires_paypal_confirmation():
    item = build_settlement_reminders([
        reservation(paid_amount=0, payment_record_count=0)
    ], TODAY)[0]
    assert item.payment_status == "confirm_external"
    assert item.remaining_amount == 600
    assert "PayPal" in item.action


def test_surf_camp_policy_uses_booking_completion_date():
    old = reservation(
        reservation_id="old",
        channel="Surf Camp",
        booking_date=dt.date(2026, 8, 31),
        paid_amount=0,
        payment_record_count=0,
    )
    new = reservation(
        reservation_id="new",
        channel="Surf Camp",
        booking_date=dt.date(2026, 9, 1),
        paid_amount=0,
        payment_record_count=0,
    )
    reminders = build_settlement_reminders([old, new], TODAY)
    assert reminders[0].expected_deposit_percent == 50
    assert reminders[1].expected_deposit_percent == 20


def test_settlement_lists_operational_extras_to_verify():
    res = reservation(extras=[
        NormalizedExtra("Board rental", "surf_equipment", 20),
        NormalizedExtra("Half Board formula", "meal", 120),
        NormalizedExtra("Airport Transfer", "transfer", 65),
    ])
    item = build_settlement_reminders([res], TODAY)[0]
    assert item.extra_checks == ["meals", "board rental", "transfer"]


def test_split_room_booking_has_one_payment_reminder():
    first = reservation(reservation_id="group-1", room="RDC1")
    second = reservation(reservation_id="group-1", room="RDC2")
    assert len(build_settlement_reminders([first, second], TODAY)) == 1


def test_consecutive_same_guest_records_are_combined_at_final_checkout():
    hostelworld = reservation(
        reservation_id="ruby-hostelworld",
        guest_name="Ruby Smith",
        channel="HostelWorld",
        arrival_date=dt.date(2026, 9, 18),
        departure_date=dt.date(2026, 9, 23),
        total_amount=720,
        paid_amount=0,
    )
    extension = reservation(
        reservation_id="ruby-olas",
        guest_name=" Ruby   Smith ",
        channel="Online",
        arrival_date=dt.date(2026, 9, 23),
        departure_date=TODAY,
        total_amount=28,
        paid_amount=0,
    )

    reminders = build_settlement_reminders([hostelworld, extension], TODAY)

    assert len(reminders) == 1
    assert reminders[0].remaining_amount == 748
    assert [part.channel for part in reminders[0].components] == ["HostelWorld", "Online"]
    assert "combined balance of €748.00" in reminders[0].action


def test_non_consecutive_same_guest_records_are_not_combined():
    older = reservation(
        reservation_id="old-stay",
        guest_name="Returning Guest",
        departure_date=dt.date(2026, 9, 10),
        total_amount=100,
        paid_amount=0,
    )
    current = reservation(
        reservation_id="current-stay",
        guest_name="Returning Guest",
        arrival_date=dt.date(2026, 9, 20),
        departure_date=TODAY,
        total_amount=50,
        paid_amount=0,
    )

    reminder = build_settlement_reminders([older, current], TODAY)[0]

    assert reminder.remaining_amount == 50
    assert len(reminder.components) == 1


def test_hostelworld_note_balance_and_mixed_currency_stay_separate():
    hostelworld = reservation(
        reservation_id="ruby-hostelworld",
        guest_name="Ruby Smith",
        channel="HostelWorld",
        arrival_date=dt.date(2026, 9, 18),
        departure_date=dt.date(2026, 9, 23),
        total_amount=720,
        paid_amount=0,
        currency="MAD",
        notes=["paid:108.00 - due:612.00 - OTAcommission:108.00 - OTAdue:0.00 - paymenttype:Deposit"],
    )
    extension = reservation(
        reservation_id="ruby-direct",
        guest_name="Ruby Smith",
        channel="Online",
        arrival_date=dt.date(2026, 9, 23),
        departure_date=TODAY,
        total_amount=28,
        paid_amount=0,
        currency="EUR",
    )

    reminder = build_settlement_reminders([hostelworld, extension], TODAY)[0]

    assert reminder.currency_balances == {
        "MAD": (720.0, 108.0, 612.0),
        "EUR": (28.0, 0.0, 28.0),
    }
    assert "each balance shown" in reminder.action


def test_sheet_guest_matching_hotelrunner_is_merged_not_double_counted():
    hr = reservation(guest_name="Same Person", house="Sunrise", room="Sunrise 4")
    sheet = reservation(
        reservation_id="sheet-1",
        source="google_sheet",
        guest_name="Same Person",
        house="Sunrise",
        room="Sunrise 4",
        meal_plan="Half Board",
        surf_level="Beginner",
        total_amount=0,
        paid_amount=0,
        payment_record_count=0,
    )
    merged = merge_cross_source_duplicates([hr, sheet])
    assert len(merged) == 1
    assert merged[0].meal_plan == "Half Board"
    assert merged[0].surf_level == "Beginner"


def test_dinner_team_message_groups_houses_in_operational_order():
    guests = [
        reservation(reservation_id="t", guest_name="Tide Guest", house="Tide", meal_plan="Half Board"),
        reservation(reservation_id="o", guest_name="Olas Guest", house="Olas", meal_plan="Half Board"),
        reservation(reservation_id="s", guest_name="Sunrise Guest", house="Sunrise", meal_plan="Half Board", diet="Veggie; fish is okay"),
    ]
    message = format_dinner_team_message(compute_meal_entitlements(guests, TODAY), guests)
    assert message.index("Sunrise x1") < message.index("Olas x1") < message.index("Tide x1")
    assert "veggie; fish is okay" in message.lower()


def test_gemini_404_retries_with_fallback_model(monkeypatch):
    import gemini_client

    calls = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "All clear"}]}}]}).encode()

    def fake_open(request, timeout=30):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return Response()

    monkeypatch.setattr(gemini_client.urllib.request, "urlopen", fake_open)
    answer = gemini_client.generate_content("key", "prompt", models=["old-model", "fallback-model"])
    assert answer == "All clear"
    assert "old-model" in calls[0]
    assert "fallback-model" in calls[1]


def test_bed_request_only_appears_on_arrival_day():
    from hotelrunner_daily_summary import StayLine, build_day_summaries

    line = StayLine(
        reservation_id="bed-1",
        hr_number="HR1",
        guest_name="Bed Guest",
        channel="Online",
        room_name="Bay",
        bed_number="6500",
        arrival=TODAY,
        departure=TODAY + dt.timedelta(days=3),
        adults=2,
        children=0,
        meal_plan="Bed And Breakfast",
        bed_request="separate beds",
    )
    summaries = build_day_summaries([line], TODAY, 2)
    assert summaries[0].bed_requests
    assert summaries[1].bed_requests == []
    assert summaries[2].bed_requests == []


def test_dashboard_contains_copyable_dinner_and_payment_panels(monkeypatch):
    import google_sheets
    from hotelrunner_daily_summary import (
        StayLine,
        build_day_summaries,
        build_dashboard_html,
    )

    monkeypatch.setattr(google_sheets, "load_sunrise_reservations", lambda: [])
    line = StayLine(
        reservation_id="pay-1",
        hr_number="HR2",
        guest_name="Checkout Guest",
        channel="Online",
        room_name="RDC1",
        bed_number="110",
        arrival=TODAY - dt.timedelta(days=2),
        departure=TODAY + dt.timedelta(days=1),
        adults=1,
        children=0,
        meal_plan="Half Board",
        total_amount=600,
        paid_amount=300,
        payment_record_count=1,
        booking_date=TODAY - dt.timedelta(days=20),
    )
    html = build_dashboard_html(
        build_day_summaries([line], TODAY, 1),
        generated_at=dt.datetime(2026, 9, 25, 7, 0),
    )
    assert "Copy Dinner List" in html
    assert "Checkout Payments" in html
    assert "remaining €300.00" in html
    assert '<link rel="icon" type="image/png" href="olas-surf-camp.png">' in html
    assert 'alt="Olas Surf Experience"' in html


def test_dashboard_combines_consecutive_booking_records_at_final_checkout(monkeypatch):
    import google_sheets
    from hotelrunner_daily_summary import (
        StayLine,
        build_day_summaries,
        build_dashboard_html,
        normalized_from_stayline_with_extras,
    )

    monkeypatch.setattr(google_sheets, "load_sunrise_reservations", lambda: [])
    earlier = StayLine(
        reservation_id="ruby-hostelworld",
        hr_number="HR-OLD",
        guest_name="Ruby Smith",
        channel="HostelWorld",
        room_name="dorm",
        bed_number="10",
        arrival=TODAY - dt.timedelta(days=7),
        departure=TODAY - dt.timedelta(days=2),
        adults=1,
        children=0,
        meal_plan="Bed And Breakfast",
        total_amount=720,
    )
    extension = StayLine(
        reservation_id="ruby-direct",
        hr_number="HR-NEW",
        guest_name="Ruby Smith",
        channel="Online",
        room_name="dorm",
        bed_number="10",
        arrival=TODAY - dt.timedelta(days=2),
        departure=TODAY,
        adults=1,
        children=0,
        meal_plan="Bed And Breakfast",
        total_amount=28,
    )
    settlement_reservations = [
        normalized_from_stayline_with_extras(earlier),
        normalized_from_stayline_with_extras(extension),
    ]

    html = build_dashboard_html(
        build_day_summaries([earlier, extension], TODAY, 0),
        generated_at=dt.datetime(2026, 9, 25, 7, 0),
        settlement_reservations=settlement_reservations,
    )

    assert "Total €748.00" in html
    assert "Linked reservation records" in html
    assert "HostelWorld" in html
    assert "Online" in html
