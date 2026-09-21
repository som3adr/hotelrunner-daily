"""
Comprehensive test suite for Olas Surf Camp Operations Assistant.
Tests:
1. Same room, overlapping dates -> conflict
2. Same room, checkout and check-in same day -> no overnight conflict
3. Dorm has 6 beds and 6 guests -> OK
4. Dorm has 6 beds and 7 guests -> capacity warning
5. Surf at 14:00 + lunch at 14:30 -> surf/lunch conflict
6. Surf at 06:30 -> breakfast timing notice
7. HotelRunner transfer extra -> transfer action
8. Unknown HotelRunner extra -> UNKNOWN EXTRA check
9. Regression: existing build_whatsapp_block output remains identical
"""
import datetime as dt
import unittest
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_model import NormalizedReservation, NormalizedExtra
from conflict_engine import detect_conflicts, _overlaps
from extras_engine import classify_raw_extra
from surf_schedule import detect_surf_meal_conflicts
from hotelrunner_daily_summary import (
    StayLine, DaySummary, build_whatsapp_block,
)


class TestOperationsAssistant(unittest.TestCase):

    def test_overlaps_logic(self):
        # Case 1: Checkout Sep 25, Check-in Sep 25 -> NO overnight overlap
        a_arr = dt.date(2026, 9, 20)
        a_dep = dt.date(2026, 9, 25)
        b_arr = dt.date(2026, 9, 25)
        b_dep = dt.date(2026, 9, 30)
        self.assertFalse(_overlaps(a_arr, a_dep, b_arr, b_dep))

        # Case 2: Guest A departs Sep 26, Guest B arrives Sep 25 -> Overlap!
        a_dep_overlap = dt.date(2026, 9, 26)
        self.assertTrue(_overlaps(a_arr, a_dep_overlap, b_arr, b_dep))

    def test_private_room_conflict(self):
        # Two different guests in RDC1 with overlapping dates
        res_a = NormalizedReservation(
            reservation_id="res-1",
            source="hotelrunner",
            guest_name="Guest Alpha",
            adults=2,
            house="Olas",
            room="RDC1",
            bed="110",
            arrival_date=dt.date(2026, 9, 21),
            departure_date=dt.date(2026, 9, 25),
        )
        res_b = NormalizedReservation(
            reservation_id="res-2",
            source="hotelrunner",
            guest_name="Guest Beta",
            adults=2,
            house="Olas",
            room="RDC1",
            bed="110",
            arrival_date=dt.date(2026, 9, 23),
            departure_date=dt.date(2026, 9, 28),
        )
        conflicts, warnings = detect_conflicts([res_a, res_b], check_date=dt.date(2026, 9, 21))
        self.assertTrue(len(conflicts) >= 1)
        self.assertEqual(conflicts[0].house, "Olas")
        self.assertEqual(conflicts[0].room, "RDC1")

    def test_dorm_capacity_ok_and_exceeded(self):
        # Dorm 03 capacity is 6
        today = dt.date(2026, 9, 21)
        dep = dt.date(2026, 9, 25)

        # 6 guests -> OK
        guests_6 = [
            NormalizedReservation(
                reservation_id=f"dorm-g{i}",
                source="hotelrunner",
                guest_name=f"Dorm Guest {i}",
                adults=1,
                house="Olas",
                room="dorm 03",
                bed="03",
                arrival_date=today,
                departure_date=dep,
            )
            for i in range(6)
        ]
        conflicts, warnings = detect_conflicts(guests_6, check_date=today)
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(len(warnings), 0)

        # 7th guest added -> Capacity Warning!
        guest_7 = NormalizedReservation(
            reservation_id="dorm-g7",
            source="hotelrunner",
            guest_name="Dorm Guest 7",
            adults=1,
            house="Olas",
            room="dorm 03",
            bed="03",
            arrival_date=today,
            departure_date=dep,
        )
        conflicts, warnings = detect_conflicts(guests_6 + [guest_7], check_date=today)
        self.assertEqual(len(conflicts), 0)
        self.assertTrue(len(warnings) >= 1)
        self.assertEqual(warnings[0].booked, 7)
        self.assertEqual(warnings[0].capacity, 6)
        self.assertEqual(warnings[0].overflow, 1)

    def test_surf_meal_conflict(self):
        # Surf at 14:00 (ends ~16:30) while lunch is 14:30
        today = dt.date(2026, 9, 21)
        sessions = [
            {
                "time": "14:00",
                "level": "Beginner",
                "spot": "Bay",
                "guest_count": 8,
            }
        ]
        # Guest with All Inclusive (includes lunch)
        res_lunch = NormalizedReservation(
            reservation_id="res-lunch",
            source="hotelrunner",
            guest_name="Surfer With Lunch",
            adults=8,
            house="Olas",
            room="dorm 04",
            arrival_date=today,
            departure_date=today + dt.timedelta(days=3),
            meal_plan="All Inclusive",
        )
        conflicts = detect_surf_meal_conflicts(sessions, [res_lunch], today)
        self.assertTrue(any(c["category"] == "SURF_LUNCH_CONFLICT" for c in conflicts))

    def test_early_surf_breakfast_notice(self):
        # Surf at 06:30 before breakfast (08:30)
        today = dt.date(2026, 9, 21)
        sessions = [
            {
                "time": "06:30",
                "level": "Intermediate",
                "spot": "Cathedral",
                "guest_count": 5,
            }
        ]
        res_bb = NormalizedReservation(
            reservation_id="res-bb",
            source="hotelrunner",
            guest_name="Early Surfer",
            adults=5,
            house="Olas",
            room="balcony",
            arrival_date=today,
            departure_date=today + dt.timedelta(days=3),
            meal_plan="Bed and breakfast",
        )
        conflicts = detect_surf_meal_conflicts(sessions, [res_bb], today)
        self.assertTrue(any(c["category"] == "EARLY_SURF_BREAKFAST" for c in conflicts))

    def test_extras_classification(self):
        # Airport transfer extra
        extra_transfer = {
            "name": "Airport Transfer (Essa / Agad)",
            "quantity": 2,
            "total": 130,
            "days": "Sep 21 x 1, Sep 29 x 1",
        }
        ne_transfer = classify_raw_extra(extra_transfer)
        self.assertIsNotNone(ne_transfer)
        self.assertEqual(ne_transfer.category, "transfer")
        self.assertEqual(len(ne_transfer.dates), 2)
        self.assertEqual(ne_transfer.dates[0].day, 21)

        # Unknown paid extra
        extra_unknown = {
            "name": "Custom VIP Quad Bike Tour",
            "quantity": 1,
            "total": 85,
        }
        ne_unknown = classify_raw_extra(extra_unknown)
        self.assertIsNotNone(ne_unknown)
        self.assertEqual(ne_unknown.category, "unknown")
        self.assertIn("Custom VIP Quad Bike Tour", ne_unknown.operational_note)

    def test_team_report_regression(self):
        # Verify build_whatsapp_block outputs expected format
        day = dt.date(2026, 9, 21)
        arr = StayLine(
            reservation_id="r1",
            hr_number="HR1",
            guest_name="Test Guest",
            channel="Direct",
            room_name="balcony",
            bed_number="113",
            arrival=day,
            departure=day + dt.timedelta(days=2),
            adults=2,
            children=0,
            meal_plan="Bed and breakfast",
        )
        dep = StayLine(
            reservation_id="r2",
            hr_number="HR2",
            guest_name="Departing Guest",
            channel="Direct",
            room_name="dorm",
            bed_number="04",
            arrival=day - dt.timedelta(days=3),
            departure=day,
            adults=1,
            children=0,
            meal_plan="Room only",
        )
        summary = DaySummary(
            date=day,
            arrivals=[arr],
            departures=[dep],
        )
        msg = build_whatsapp_block(summary)
        self.assertIn("🏁 CHECK-OUTS · 21 September", msg)
        self.assertIn("🏨 CHECK-INS · 21 September", msg)
        self.assertIn("Test Guest x2 -> balcony", msg)
        self.assertIn("Departing Guest x1 -> dorm", msg)


if __name__ == "__main__":
    unittest.main()
