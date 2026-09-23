"""
Phase 9 tests — Sunrise 9300 dorm conflict detection and house mapping.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_model import NormalizedReservation, detect_house_from_stayline
from conflict_engine import detect_conflicts


TODAY = dt.date(2026, 9, 23)
DEP = TODAY + dt.timedelta(days=5)


def _make_sunrise_9300(rid, bed, arrival=TODAY, departure=DEP):
    return NormalizedReservation(
        reservation_id=rid,
        source="hotelrunner",
        guest_name=f"Sunrise Guest {rid}",
        adults=1,
        house="Sunrise",
        room="9300",
        bed=bed,
        arrival_date=arrival,
        departure_date=departure,
        meal_plan="Half Board",
    )


class TestSunrise9300Conflicts(unittest.TestCase):

    def test_same_9300_1_bed_is_conflict(self):
        """Two guests in 9300-1 overlapping → bed conflict."""
        res_a = _make_sunrise_9300("a", "9300-1")
        res_b = _make_sunrise_9300("b", "9300-1",
                                    arrival=TODAY + dt.timedelta(days=1),
                                    departure=TODAY + dt.timedelta(days=4))
        conflicts, warnings = detect_conflicts([res_a, res_b], check_date=TODAY)
        self.assertTrue(len(conflicts) >= 1)

    def test_different_9300_beds_no_conflict(self):
        """9300-1 and 9300-2 overlapping → NO bed conflict."""
        res_a = _make_sunrise_9300("c", "9300-1")
        res_b = _make_sunrise_9300("d", "9300-2")
        conflicts, warnings = detect_conflicts([res_a, res_b], check_date=TODAY)
        # No room conflict — different beds
        bed_conflicts = [c for c in conflicts if "9300" in (c.room or "")]
        self.assertEqual(len(bed_conflicts), 0)

    def test_9300_dorm_5_beds_ok(self):
        """5 different 9300 beds occupied → no capacity warning."""
        guests = [_make_sunrise_9300(f"g{i}", f"9300-{i+1}") for i in range(5)]
        conflicts, warnings = detect_conflicts(guests, check_date=TODAY)
        dorm_warnings = [w for w in warnings if "9300" in (w.room or "")]
        self.assertEqual(len(dorm_warnings), 0)

    def test_9300_dorm_6_beds_capacity_warning(self):
        """6 distinct beds in 9300 dorm (capacity 5) → capacity warning."""
        guests = [_make_sunrise_9300(f"h{i}", f"9300-{i+1}") for i in range(6)]
        conflicts, warnings = detect_conflicts(guests, check_date=TODAY)
        dorm_warnings = [w for w in warnings if "9300" in (w.room or "")]
        self.assertTrue(len(dorm_warnings) >= 1)

    def test_same_day_checkin_checkout_no_conflict(self):
        """Regression: same-day checkout/checkin on same 9300-1 bed = NOT a conflict."""
        res_a = _make_sunrise_9300("e", "9300-1",
                                    arrival=TODAY,
                                    departure=TODAY + dt.timedelta(days=3))
        res_b = _make_sunrise_9300("f", "9300-1",
                                    arrival=TODAY + dt.timedelta(days=3),
                                    departure=TODAY + dt.timedelta(days=6))
        conflicts, warnings = detect_conflicts([res_a, res_b], check_date=TODAY)
        bed1_conflicts = [
            c for c in conflicts
            if all((r.bed or "") == "9300-1" for r in c.reservations)
        ]
        self.assertEqual(len(bed1_conflicts), 0)

    def test_detect_house_9300_is_sunrise(self):
        """HotelRunner bed 9300-1 must map to Sunrise house."""
        self.assertEqual(detect_house_from_stayline("9300-1", "Direct"), "Sunrise")
        self.assertEqual(detect_house_from_stayline("9300-2", "Booking.com"), "Sunrise")

    def test_detect_house_9200_is_sunrise(self):
        self.assertEqual(detect_house_from_stayline("9200", "Direct"), "Sunrise")

    def test_detect_house_9500_is_sunrise(self):
        self.assertEqual(detect_house_from_stayline("9500", "Airbnb"), "Sunrise")

    def test_detect_house_bay_still_tide(self):
        """Regression: bay still maps to Tide."""
        self.assertEqual(detect_house_from_stayline("bay view room", "Direct"), "Tide")

    def test_detect_house_dorm_still_olas(self):
        """Regression: regular rooms still map to Olas."""
        self.assertEqual(detect_house_from_stayline("dorm 03", "Hostelworld"), "Olas")
        self.assertEqual(detect_house_from_stayline("RDC1", "Direct"), "Olas")


if __name__ == "__main__":
    unittest.main()
