# Test Scenarios — Acceptance Criteria

These are the real-world operational scenarios that MUST pass.
**No task is considered complete until its relevant scenarios pass.**

---

## The Etienne Scenario (Critical Regression Test)

**Status**: Verified working as of 2026-09-23

**This scenario defines the fundamental correctness of the Tomorrow Preparation system.**

```
Scenario: Tomorrow transfers surface correctly without same-day reservation changes

Given:
  Today = September 23, 2026
  Etienne Aleveque has departure_date = September 24, 2026
  Etienne's meal_plan = "All Inclusive" (transfer included)
  Etienne's reservation was entered days ago — NOT changed today

Expected:
  Running: python tomorrow_transfers.py --dry-run (on Sep 23)
  → Message contains "Etienne Aleveque"
  → Shows "NEEDS INFORMATION" (pickup time missing)
  → Does NOT require any change to the reservation today

Verified:
  detect_transfer_entitlement(etienne_res) → True ✓
  build_transfer_records(norm, date=Sep24) → includes Etienne ✓
  build_tomorrow_transfer_message(cache, today=Sep23) → includes Etienne ✓
```

**Root Cause (for reference)**:
The concern was that the tomorrow preparation might silently depend on the
change watcher having detected a change. It does not — `tomorrow_transfers.py`
independently scans ALL reservations for tomorrow using:
```python
records = build_transfer_records(reservations, tomorrow, store)
```
This is purely date-based, not change-based.

**Regression Test**: `tests/test_tomorrow_transfers.py` — see below.

---

## Transfer Engine Scenarios (T-series)

### T1: All Inclusive → Transfer Detected
- **Given**: guest with `meal_plan="All Inclusive"`
- **Expected**: `detect_transfer_entitlement(res)` → `True`

### T2: Half Board → No Transfer (unless extra or note)
- **Given**: guest with `meal_plan="Half Board"`, no extras, no transfer notes
- **Expected**: `detect_transfer_entitlement(res)` → `False`

### T3: Transfer keyword in notes
- **Given**: guest with note containing "airport transfer"
- **Expected**: `detect_transfer_entitlement(res)` → `True`

### T4: Transfer extra in HotelRunner fees
- **Given**: guest with `NormalizedExtra(category="transfer")`
- **Expected**: `detect_transfer_entitlement(res)` → `True`

### T5: Arrival with flight → ready_to_send
- **Given**: arrival record with `flight_number` AND `airport` populated
- **Expected**: `status == "ready_to_send"`

### T6: Departure missing pickup time → needs_info
- **Given**: departure record with no `pickup_time`
- **Expected**: `status == "needs_info"`

### T7: Sent flag respected
- **Given**: transfer manually marked as sent in `TransferStateStore`
- **Expected**: `status == "sent"` (overrides ready check)

### T8: Arrival record default airport
- **Given**: arrival with no airport information in notes or extras
- **Expected**: `airport == "Agadir airport"` (Imsouane default)

### T9: tomorrow_transfers includes all AI guests
- **Given**: 3 All Inclusive guests departing tomorrow
- **Expected**: `build_transfer_records(reservations, tomorrow)` → 3 records

---

## Meal Engine Scenarios (M-series)

### M1: Half Board → 1 dinner
- **Given**: 1 adult, Half Board meal plan
- **Expected**: 1 dinner entitlement (`count=1`)

### M2: All Inclusive → 1 dinner
- **Given**: 1 adult, All Inclusive
- **Expected**: 1 dinner entitlement via meal_plan

### M3: No double-counting
- **Given**: 1 adult, Half Board + "dinner" note + dinner extra
- **Expected**: **1** dinner entitlement total (not 3)

### M4: Room Only → 0 dinners
- **Given**: guest with Room Only or Bed And Breakfast
- **Expected**: 0 dinner entitlements

### M5: Guest not active on date → 0
- **Given**: guest's stay does not include the target date
- **Expected**: 0 entitlements for that date

### M6: Dinner thresholds
| Guest Count | Expected Rule | Expected Notice |
|-------------|--------------|-----------------|
| 0–13 | `"normal"` | `None` |
| 14–24 | `"extra_tables"` | Contains "Extra tables" |
| 25+ | `"capacity_warning"` | Contains "CAPACITY" |

### M7: Ambiguous meal note → AttentionItem
- **Given**: note contains "repas" (French for meal, unclear which)
- **Expected**: `flag_ambiguous_meals()` returns 1 AttentionItem
- **Expected**: NOT added to dinner count

---

## 9300 Dorm Conflict Scenarios (C-series)

### C1: Same bed, overlapping dates → Conflict
- **Given**: Guest A on `9300-1` Sep 1–5, Guest B on `9300-1` Sep 3–7
- **Expected**: `RoomConflict` returned

### C2: Different beds → No conflict
- **Given**: Guest A on `9300-1`, Guest B on `9300-2`, overlapping dates
- **Expected**: No conflict

### C3: 5 beds occupied → No capacity warning
- **Given**: Guests on `9300-1` through `9300-5`
- **Expected**: No `CapacityWarning`

### C4: 6 distinct beds → Capacity Warning
- **Given**: 6 reservations on `9300-x` same date (hypothetical)
- **Expected**: `CapacityWarning` for "9300 Dorm"

### C5: Same-day checkout/checkin → No conflict
- **Given**: Guest A departs Sep 24, Guest B arrives Sep 24 on same bed
- **Expected**: No conflict (departure exclusive)

---

## House Detection Scenarios (H-series)

| Input | Expected House |
|-------|---------------|
| room_name=`"9300-1"` | `"Sunrise"` |
| room_name=`"9200"` | `"Sunrise"` |
| room_name=`"9500"` | `"Sunrise"` |
| room_name containing `"bay"` | `"Tide"` |
| room_name containing `"tidehunter"` | `"Tide"` |
| room_name=`"dorm 03"` | `"Olas"` |
| room_name=`"room 5"` | `"Olas"` |

---

## Original Regression Tests (must always pass)

All 8 tests in `tests/test_manager_assistant.py` must always pass:

1. Private room double-booking → conflict detected
2. Same-day checkout/checkin → no conflict
3. Dorm capacity within limit → no warning
4. Dorm capacity exceeded → warning
5. Surf/meal time conflict detection
6. Early surf breakfast notice
7. Extras classification
8. Team report output format unchanged (regression)

---

## Pending Scenarios (not yet automated)

### Tomorrow Scenario Formal Test

Test exists at: `tests/test_tomorrow_transfers.py`

```python
def test_etienne_scenario():
    """
    A guest departing tomorrow with All Inclusive must appear in
    tomorrow's transfer list even when reservation hasn't changed today.
    """
```

### Late-Transfer Change Watcher Scenario

When a transfer note/extra is added to a reservation at 21:30:
- **Expected**: Change Watcher (running hourly) detects reservation change
- **Expected**: Alert sent via Telegram with the change details
- **Not yet automated** — requires integration test with fake HotelRunner cache
- **Verify manually** by: adding a note to a test reservation in HotelRunner cache
  and running `python telegram_send.py --mode check --dry-run`

### Clement Arnoux Duplicate Scenario

Currently Clement Arnoux appears twice in transfer records.
Likely cause: two `StayLine` records for the same group booking.
- **Expected fix**: `build_transfer_records()` deduplicates by `(reservation_id, direction, date)`
- **Must not affect**: different guests or different dates
