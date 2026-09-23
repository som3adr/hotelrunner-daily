# Data Model

## NormalizedReservation

The central data structure. All sources (HotelRunner, Google Sheet) are
normalized into this before any business logic runs.

```python
@dataclass
class NormalizedReservation:
    # Identity
    reservation_id: str
    source: str             # "hotelrunner" | "google_sheet" | "direct"
    hr_number: str = ""

    # Guest
    guest_name: str = ""
    adults: int = 0
    children: int = 0

    # Location
    house: str = ""         # "Olas" | "Tide" | "Sunrise"
    room: str = ""
    bed: str = ""

    # Dates
    arrival_date: dt.date | None = None
    departure_date: dt.date | None = None

    # Meal & booking
    meal_plan: str = ""
    status: str = ""        # "confirmed" | "reserved" | "cancelled"
    channel: str = ""

    # Extras
    extras: list[NormalizedExtra] = [...]
    notes: list[str] = [...]
    bed_request: str = ""

    # Transfers (parsed from sheet or extras)
    arrival_transfer: str | None = None    # e.g. "13:05 - Agadir Airport"
    departure_transfer: str | None = None  # e.g. "FR9347" or "05h45 from the house"

    # Surf (Google Sheet only)
    surf_package: str | None = None
    surf_level: str | None = None          # "Beginner" | "Intermediate" | "Advanced"
    surf_goals: str | None = None

    # Health (Google Sheet only)
    diet: str | None = None
    allergies: str | None = None
    medical_notes: str | None = None

    # Payment (HotelRunner only)
    total_amount: float = 0.0
    paid_amount: float = 0.0

    # Group booking
    is_group_booking: bool = False
    group_leader: str = ""
```

### Key Properties

| Property | Type | Meaning |
|----------|------|---------|
| `guest_count` | int | `adults + children` |
| `nights` | int | `(departure_date - arrival_date).days` |
| `unpaid_amount` | float | `max(0, total - paid)` |
| `is_active_on(date)` | bool | `arrival <= date < departure` |
| `is_arriving_on(date)` | bool | `arrival_date == date` |
| `is_departing_on(date)` | bool | `departure_date == date` |
| `has_transfer_in` | bool | `arrival_transfer is not None` |
| `has_transfer_out` | bool | `departure_transfer is not None` |
| `transfer_extras` | list | extras where `category == "transfer"` |
| `meal_extras` | list | extras where `category == "meal"` |
| `unknown_extras` | list | extras where `category == "unknown"` |

### Critical: is_active_on vs is_departing_on

`is_active_on(date)` uses **exclusive departure** (`arrival <= date < departure`).

This means a guest departing September 24 is:
- `is_active_on(Sep 23)` → **True** (their last night)
- `is_active_on(Sep 24)` → **False**
- `is_departing_on(Sep 24)` → **True**

For **transfer purposes**, always use `is_departing_on(date)` — NOT `is_active_on`.

---

## NormalizedExtra

```python
@dataclass
class NormalizedExtra:
    raw_label: str
    category: str       # "transfer" | "surf_equipment" | "surf_lesson"
                        # | "meal" | "tax" | "unknown"
    quantity: int | None = None
    amount: float | None = None
    dates: list[dt.date] = []
    operational_note: str | None = None
```

Created by `extras_engine.classify_extras_from_reservation(raw_dict)`.

---

## MealEntitlement

Auditable record of one guest's meal entitlement for a specific meal and date.

```python
@dataclass
class MealEntitlement:
    reservation_id: str
    guest_name: str
    house: str
    date: dt.date
    meal: str          # "breakfast" | "lunch" | "dinner"
    count: int         # number of covers (= guest_count, usually 1 per adult)
    source: str        # "meal_plan" | "extra" | "note"
    reason_text: str   # e.g. "Half Board" or "All Inclusive + extra"
```

Created by `meal_engine.compute_meal_entitlements(reservations, date)`.

### Deduplication Rule

Each guest is counted **ONCE** per meal regardless of how many sources confirm it.
- Half Board + dinner note + dinner extra → **1 dinner entitlement**

---

## TransferRecord

```python
@dataclass
class TransferRecord:
    reservation_id: str
    guest_name: str
    house: str
    direction: str          # "arrival" | "departure"
    date: dt.date
    passenger_count: int = 1
    flight_number: str = "" # arrivals
    airport: str = ""       # arrivals (default: "Agadir airport")
    pickup_time: str = ""   # departures, format "HH:MM"
    destination: str = ""   # departures (default: "Agadir airport")
    raw_source: str = ""    # "all_inclusive" | "note" | "extra"
    status: str = "needs_info"  # "needs_info" | "ready_to_send" | "sent"
```

Created by `transfer_engine.build_transfer_records(reservations, date, store)`.

### Status Classification

| Status | Condition |
|--------|-----------|
| `sent` | `TransferStateStore.is_sent(id, direction, date)` returns True |
| `ready_to_send` | Arrival: `flight_number AND airport` populated. Departure: `pickup_time AND destination` populated |
| `needs_info` | Any required field missing |

---

## AttentionItem

Operational item requiring manager attention — not a hard conflict.

```python
@dataclass
class AttentionItem:
    category: str    # "meal" | "transfer" | "extra" | "room" | "surf"
    severity: str    # "info" | "check" | "action_required"
    title: str
    description: str
    reservation_id: str = ""
```

---

## Source Adapters

| Source | Function | Notes |
|--------|----------|-------|
| HotelRunner StayLine | `stayline_to_normalized(line, raw)` | Pass raw dict for payment + extras |
| Google Sheet row | `sheet_row_to_normalized(row, row_index)` | Returns None for empty/invalid rows |

---

## House Detection Rules

`detect_house_from_stayline(room_name, channel)`:

1. If `room_name` OR `channel` starts with `9200|9300|9400|9500|9600` → **Sunrise**
2. If `room_name room_name channel` contains `tidehunter|bay|slab|cathedral|reef` → **Tide**
3. Everything else → **Olas**

Sunrise prefixes are configurable in `config.json` > `sunrise_hotelrunner.all_sunrise_prefixes`.

---

## Config.json Key Sections

```json
{
  "meals": {
    "plans": {
      "all inclusive": {"breakfast": true, "lunch": true, "dinner": true},
      "full board":    {"breakfast": true, "lunch": true, "dinner": true},
      "half board":    {"breakfast": true, "lunch": false, "dinner": true},
      "bed and breakfast": {"breakfast": true, "lunch": false, "dinner": false}
    },
    "dinner_thresholds": {
      "extra_tables_above": 13,
      "capacity_warning_above": 24
    }
  },
  "sunrise_hotelrunner": {
    "all_sunrise_prefixes": ["9200", "9300", "9400", "9500", "9600"],
    "dorm_room_prefix": "9300",
    "dorm_max_beds": 5
  },
  "scheduled_jobs": {
    "tomorrow_transfer_times_local": ["17:00", "20:00"]
  }
}
```
