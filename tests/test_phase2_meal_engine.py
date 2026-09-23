"""
Phase 2 & 3 tests — Meal engine: deduplication, audit trail, and dinner prep rule.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_model import NormalizedReservation, NormalizedExtra
from meal_engine import (
    compute_meal_entitlements, summarize_dinner,
    dinner_preparation_notice, flag_ambiguous_meals, parse_note_for_meal,
)


TODAY = dt.date(2026, 9, 23)


def _make_res(reservation_id, meal_plan, house="Olas", adults=2,
              notes=None, extras=None):
    return NormalizedReservation(
        reservation_id=reservation_id,
        source="hotelrunner",
        guest_name=f"Guest {reservation_id}",
        adults=adults,
        house=house,
        room="RDC1",
        arrival_date=TODAY,
        departure_date=TODAY + dt.timedelta(days=3),
        meal_plan=meal_plan,
        notes=notes or [],
        extras=extras or [],
    )


class TestMealEngine(unittest.TestCase):

    def test_half_board_one_dinner(self):
        res = _make_res("r1", "Half Board", adults=1)
        entitlements = compute_meal_entitlements([res], TODAY)
        dinners = [e for e in entitlements if e.meal == "dinner"]
        self.assertEqual(len(dinners), 1)
        self.assertEqual(dinners[0].count, 1)
        self.assertIn("Half Board", dinners[0].reason_text)

    def test_full_board_one_dinner(self):
        res = _make_res("r2", "Full Board", adults=1)
        entitlements = compute_meal_entitlements([res], TODAY)
        dinners = [e for e in entitlements if e.meal == "dinner"]
        self.assertEqual(len(dinners), 1)
        self.assertEqual(dinners[0].count, 1)

    def test_all_inclusive_one_dinner(self):
        res = _make_res("r3", "All Inclusive", adults=2)
        entitlements = compute_meal_entitlements([res], TODAY)
        dinners = [e for e in entitlements if e.meal == "dinner"]
        self.assertEqual(len(dinners), 1)
        self.assertEqual(dinners[0].count, 2)

    def test_bb_with_paid_dinner_extra(self):
        """B&B guest with a paid dinner extra → counted for dinner."""
        dinner_extra = NormalizedExtra(
            raw_label="Dinner",
            category="meal",
            quantity=1,
        )
        res = _make_res("r4", "Bed And Breakfast", adults=1, extras=[dinner_extra])
        entitlements = compute_meal_entitlements([res], TODAY)
        dinners = [e for e in entitlements if e.meal == "dinner"]
        self.assertEqual(len(dinners), 1)

    def test_half_board_plus_duplicate_dinner_not_double_counted(self):
        """Half Board + dinner note = still exactly 1 dinner entitlement record."""
        res = _make_res(
            "r5", "Half Board", adults=2,
            notes=["Guest has dinner tonight (half board)"],
        )
        entitlements = compute_meal_entitlements([res], TODAY)
        dinners = [e for e in entitlements if e.meal == "dinner"]
        # Must be exactly 1 MealEntitlement record (count=2 for 2 adults)
        self.assertEqual(len(dinners), 1)
        self.assertEqual(dinners[0].count, 2)

    def test_room_only_no_dinner(self):
        res = _make_res("r6", "Room Only", adults=2)
        entitlements = compute_meal_entitlements([res], TODAY)
        dinners = [e for e in entitlements if e.meal == "dinner"]
        self.assertEqual(len(dinners), 0)

    def test_google_sheet_guest_same_logic(self):
        """Sunrise guest with Half Board → exactly 1 dinner."""
        res = NormalizedReservation(
            reservation_id="sheet-1",
            source="google_sheet",
            guest_name="Sunrise Guest",
            adults=1,
            house="Sunrise",
            room="Sunrise 2",
            arrival_date=TODAY,
            departure_date=TODAY + dt.timedelta(days=3),
            meal_plan="Half Board",
        )
        entitlements = compute_meal_entitlements([res], TODAY)
        dinners = [e for e in entitlements if e.meal == "dinner"]
        self.assertEqual(len(dinners), 1)

    def test_not_active_not_counted(self):
        """Guest who has departed is not counted."""
        res = _make_res("r7", "Half Board", adults=1)
        res.departure_date = TODAY  # departed today = not active (arrival <= date < departure)
        entitlements = compute_meal_entitlements([res], TODAY)
        self.assertEqual(len(entitlements), 0)

    def test_summarize_dinner_by_house(self):
        olas_res = _make_res("olas1", "Half Board", house="Olas", adults=3)
        tide_res = _make_res("tide1", "All Inclusive", house="Tide", adults=2)
        entitlements = compute_meal_entitlements([olas_res, tide_res], TODAY)
        summary = summarize_dinner(entitlements)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["by_house"]["Olas"], 3)
        self.assertEqual(summary["by_house"]["Tide"], 2)

    def test_ambiguous_note_flagged(self):
        """A note that mentions 'repas' without being parseable → AttentionItem."""
        res = _make_res(
            "r8", "Room Only", adults=1,
            notes=["guest will join for a repas maybe"],
        )
        items = flag_ambiguous_meals([res], TODAY)
        self.assertTrue(len(items) >= 1)
        self.assertEqual(items[0].category, "meal")
        self.assertEqual(items[0].severity, "check")

    def test_parse_note_dinner(self):
        self.assertEqual(parse_note_for_meal("Guest has dîner tonight"), "dinner")
        self.assertEqual(parse_note_for_meal("demi-pension inclus"), "dinner")
        self.assertEqual(parse_note_for_meal("half board"), "dinner")

    def test_parse_note_none_for_unrelated(self):
        self.assertIsNone(parse_note_for_meal("Late checkout requested"))
        self.assertIsNone(parse_note_for_meal("Please prepare extra towels"))


class TestDinnerPrepRule(unittest.TestCase):
    """Phase 3 tests — dinner preparation notice."""

    def test_13_normal(self):
        self.assertIsNone(dinner_preparation_notice(13))

    def test_14_extra_tables(self):
        notice = dinner_preparation_notice(14)
        self.assertIsNotNone(notice)
        self.assertIn("extra tables", notice.lower())

    def test_24_extra_tables_ok(self):
        notice = dinner_preparation_notice(24)
        self.assertIsNotNone(notice)
        self.assertIn("extra tables", notice.lower())
        self.assertNotIn("CAPACITY", notice)

    def test_25_capacity_warning(self):
        notice = dinner_preparation_notice(25)
        self.assertIsNotNone(notice)
        self.assertIn("CAPACITY", notice)

    def test_0_normal(self):
        self.assertIsNone(dinner_preparation_notice(0))


if __name__ == "__main__":
    unittest.main()
