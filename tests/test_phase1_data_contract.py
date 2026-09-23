"""
Phase 1 tests — Normalized data contract.
Tests MealEntitlement, TransferRecord, AttentionItem, dinner_setup_rule,
and the fixed detect_house_from_stayline (Sunrise 9300-x support).
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_model import (
    MealEntitlement, TransferRecord, AttentionItem,
    dinner_setup_rule, detect_house_from_stayline,
)


class TestDataContract(unittest.TestCase):

    def test_meal_entitlement_fields(self):
        me = MealEntitlement(
            reservation_id="r1",
            guest_name="Alice",
            house="Olas",
            date=dt.date(2026, 9, 23),
            meal="dinner",
            count=2,
            source="meal_plan",
            reason_text="Half Board",
        )
        self.assertEqual(me.meal, "dinner")
        self.assertEqual(me.count, 2)
        self.assertEqual(me.source, "meal_plan")

    def test_transfer_record_defaults(self):
        tr = TransferRecord(
            reservation_id="r2",
            guest_name="Bob",
            house="Tide",
            direction="arrival",
            date=dt.date(2026, 9, 24),
        )
        self.assertEqual(tr.status, "needs_info")
        self.assertTrue(tr.is_arrival)
        self.assertFalse(tr.is_departure)

    def test_transfer_record_departure(self):
        tr = TransferRecord(
            reservation_id="r3",
            guest_name="Carol",
            house="Olas",
            direction="departure",
            date=dt.date(2026, 9, 25),
            pickup_time="15:30",
        )
        self.assertFalse(tr.is_arrival)
        self.assertTrue(tr.is_departure)

    def test_attention_item_fields(self):
        ai = AttentionItem(
            category="meal",
            severity="check",
            title="Ambiguous dinner note",
            description="Could not parse dinner wording",
            reservation_id="r4",
        )
        self.assertEqual(ai.category, "meal")
        self.assertEqual(ai.severity, "check")

    def test_dinner_setup_rule_normal(self):
        self.assertEqual(dinner_setup_rule(0), "normal")
        self.assertEqual(dinner_setup_rule(13), "normal")

    def test_dinner_setup_rule_extra_tables(self):
        self.assertEqual(dinner_setup_rule(14), "extra_tables")
        self.assertEqual(dinner_setup_rule(24), "extra_tables")

    def test_dinner_setup_rule_capacity_warning(self):
        self.assertEqual(dinner_setup_rule(25), "capacity_warning")
        self.assertEqual(dinner_setup_rule(100), "capacity_warning")

    def test_detect_house_9300_is_sunrise(self):
        """HotelRunner bed 9300-1 must map to Sunrise."""
        self.assertEqual(detect_house_from_stayline("9300-1", "Direct"), "Sunrise")
        self.assertEqual(detect_house_from_stayline("9300-2", "Booking.com"), "Sunrise")

    def test_detect_house_9200_is_sunrise(self):
        self.assertEqual(detect_house_from_stayline("9200", "Direct"), "Sunrise")

    def test_detect_house_bay_still_tide(self):
        """Regression: Tide keywords still work."""
        self.assertEqual(detect_house_from_stayline("bay view room", "Direct"), "Tide")
        self.assertEqual(detect_house_from_stayline("cathedral", "Booking.com"), "Tide")

    def test_detect_house_dorm_still_olas(self):
        """Regression: regular rooms still map to Olas."""
        self.assertEqual(detect_house_from_stayline("dorm 03", "Hostelworld"), "Olas")
        self.assertEqual(detect_house_from_stayline("RDC1", "Direct"), "Olas")


if __name__ == "__main__":
    unittest.main()
