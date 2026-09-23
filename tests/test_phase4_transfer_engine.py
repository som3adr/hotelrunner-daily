"""
Phase 4 tests — Transfer engine: detection, status, and driver message format.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_model import NormalizedReservation, NormalizedExtra, TransferRecord
from transfer_engine import (
    detect_transfer_entitlement, build_transfer_records,
    format_driver_message, _parse_flight_from_text, _parse_time_from_text,
)
from transfer_state import TransferStateStore, TransferSentRecord


TOMORROW = dt.date(2026, 9, 24)


def _empty_store():
    store = TransferStateStore.__new__(TransferStateStore)
    store._records = {}
    store.path = Path("nonexistent_test_transfer.json")
    return store


def _make_res(rid, meal_plan, house="Olas", adults=2,
              arrival=TOMORROW, departure=None,
              notes=None, extras=None,
              arrival_transfer=None, departure_transfer=None):
    if departure is None:
        departure = TOMORROW + dt.timedelta(days=3)
    return NormalizedReservation(
        reservation_id=rid,
        source="hotelrunner",
        guest_name=f"Guest {rid}",
        adults=adults,
        house=house,
        room="RDC1",
        arrival_date=arrival,
        departure_date=departure,
        meal_plan=meal_plan,
        notes=notes or [],
        extras=extras or [],
        arrival_transfer=arrival_transfer,
        departure_transfer=departure_transfer,
    )


class TestTransferEngine(unittest.TestCase):

    def test_all_inclusive_has_transfer(self):
        res = _make_res("r1", "All Inclusive")
        self.assertTrue(detect_transfer_entitlement(res))

    def test_room_only_no_transfer(self):
        res = _make_res("r2", "Room Only")
        self.assertFalse(detect_transfer_entitlement(res))

    def test_half_board_no_transfer_unless_extra_or_note(self):
        res = _make_res("r3", "Half Board")
        self.assertFalse(detect_transfer_entitlement(res))

    def test_transfer_extra_detected(self):
        extra = NormalizedExtra(raw_label="Airport Transfer", category="transfer")
        res = _make_res("r4", "Bed And Breakfast", extras=[extra])
        self.assertTrue(detect_transfer_entitlement(res))

    def test_transfer_note_detected(self):
        res = _make_res("r5", "Bed And Breakfast", notes=["Please arrange airport transfer"])
        self.assertTrue(detect_transfer_entitlement(res))

    def test_arrival_complete_details_ready_to_send(self):
        """All Inclusive arrival with flight + airport → ready_to_send."""
        res = _make_res("r6", "All Inclusive", notes=["Flight HV6491, arrives Agadir airport"])
        records = build_transfer_records([res], TOMORROW, _empty_store())
        arrivals = [r for r in records if r.direction == "arrival"]
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0].status, "ready_to_send")
        self.assertEqual(arrivals[0].flight_number, "HV6491")

    def test_arrival_missing_flight_needs_info(self):
        """All Inclusive arrival with no flight details → needs_info."""
        res = _make_res("r7", "All Inclusive")
        records = build_transfer_records([res], TOMORROW, _empty_store())
        arrivals = [r for r in records if r.direction == "arrival"]
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0].status, "needs_info")

    def test_departure_missing_pickup_time_needs_info(self):
        """Departure with no pickup time → needs_info."""
        dep_date = TOMORROW
        res = _make_res(
            "r8", "All Inclusive",
            arrival=dep_date - dt.timedelta(days=3),
            departure=dep_date,
        )
        records = build_transfer_records([res], dep_date, _empty_store())
        departures = [r for r in records if r.direction == "departure"]
        self.assertEqual(len(departures), 1)
        self.assertEqual(departures[0].status, "needs_info")

    def test_sent_status_respected(self):
        """Record marked as sent in store → status is 'sent'."""
        store = _empty_store()
        key = f"r9:arrival:{TOMORROW.isoformat()}"
        store._records[key] = TransferSentRecord(key=key, marked_sent_at="2026-09-23T17:00:00")
        res = _make_res("r9", "All Inclusive", notes=["Flight AT425 Agadir"])
        records = build_transfer_records([res], TOMORROW, store)
        arrivals = [r for r in records if r.direction == "arrival"]
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0].status, "sent")

    def test_format_arrival_message(self):
        rec = TransferRecord(
            reservation_id="r10",
            guest_name="Alice Dupont",
            house="Olas",
            direction="arrival",
            date=dt.date(2026, 9, 20),
            passenger_count=1,
            flight_number="HV6491",
            airport="Agadir airport",
            status="ready_to_send",
        )
        msg = format_driver_message(rec)
        self.assertIn("Arrivée *Olas*", msg)
        self.assertIn("20 septembre 2026", msg)
        self.assertIn("*Alice Dupont* X1", msg)
        self.assertIn("Flight number: HV6491", msg)
        self.assertIn("Agadir airport", msg)

    def test_format_departure_message(self):
        rec = TransferRecord(
            reservation_id="r11",
            guest_name="Bob Martin",
            house="Tide",
            direction="departure",
            date=dt.date(2026, 9, 18),
            passenger_count=2,
            pickup_time="15:30",
            destination="Agadir airport",
            status="ready_to_send",
        )
        msg = format_driver_message(rec)
        self.assertIn("Depart *Tide*", msg)
        self.assertIn("18 septembre 2026", msg)
        self.assertIn("*Bob Martin* X2", msg)
        self.assertIn("Time: 15:30", msg)
        self.assertIn("Agadir airport", msg)

    def test_parse_flight_number(self):
        self.assertEqual(_parse_flight_from_text("Flight HV6491"), "HV6491")
        self.assertEqual(_parse_flight_from_text("AT425 Agadir"), "AT425")
        self.assertEqual(_parse_flight_from_text("no flight"), "")

    def test_parse_time(self):
        self.assertEqual(_parse_time_from_text("pickup at 15h30"), "15:30")
        self.assertEqual(_parse_time_from_text("at 09:00"), "09:00")
        self.assertEqual(_parse_time_from_text("no time here"), "")

    def test_no_transfer_no_records(self):
        """Room Only guest → no transfer records."""
        res = _make_res("r12", "Room Only")
        records = build_transfer_records([res], TOMORROW, _empty_store())
        self.assertEqual(len(records), 0)


if __name__ == "__main__":
    unittest.main()
