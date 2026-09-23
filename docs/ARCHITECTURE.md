# Architecture — HotelRunner Daily Summary

## Design Principle: Single Source of Truth

All operational information (meals, transfers, conflicts) flows through
normalized domain engines. No feature should re-implement business logic.

```
HotelRunner API
Google Sheet (Sunrise)
  ↓
NORMALIZATION LAYER
  data_model.py → NormalizedReservation[]
  extras_engine.py → NormalizedExtra classification
  ↓
DOMAIN ENGINES
  meal_engine.py      → MealEntitlement[]
  transfer_engine.py  → TransferRecord[]
  conflict_engine.py  → RoomConflict[], CapacityWarning[]
  surf_schedule.py    → surf session data
  ↓
OUTPUT SURFACES
  dashboard   → hotelrunner_daily_summary.py (build_dashboard_html)
  telegram    → telegram_send.py (change watcher + daily briefing)
  preparation → tomorrow_transfers.py
  reports     → manager_report.py
```

---

## Module Reference

### Core Data Layer

**`data_model.py`**
- `NormalizedReservation` — unified reservation from any source
- `NormalizedExtra` — classified additional fees/extras
- `MealEntitlement` — auditable meal entitlement record
- `TransferRecord` — transfer with status classification
- `AttentionItem` — operational attention item (non-hard-conflict)
- `stayline_to_normalized(line, raw)` — HotelRunner StayLine → NormalizedReservation
- `sheet_row_to_normalized(row, index)` — Google Sheet row → NormalizedReservation
- `detect_house_from_stayline(room, channel)` → "Olas" | "Tide" | "Sunrise"
- `meals_for_reservation(res, date)` — legacy meal counter (prefer meal_engine)
- `dinner_setup_rule(count)` → "normal" | "extra_tables" | "capacity_warning"

**`config.json`**
- Houses, rooms, capacities
- Meal plans and thresholds (dinner_thresholds: 13/24)
- Sunrise HotelRunner room prefixes (9200–9600)
- Scheduled job times
- Google Sheets column mappings

---

### Domain Engines

**`meal_engine.py`** ← USE THIS for meal counts
- `compute_meal_entitlements(reservations, date)` → list[MealEntitlement]
- `summarize_dinner(entitlements)` → {total, by_house, audit, setup, notice}
- `dinner_preparation_notice(count)` → warning string or None
- `flag_ambiguous_meals(reservations, date)` → list[AttentionItem]

**`transfer_engine.py`** ← USE THIS for transfer detection
- `detect_transfer_entitlement(reservation)` → bool
- `build_transfer_records(reservations, date, store)` → list[TransferRecord]
- `format_driver_message(record)` → French-format driver message string

**`conflict_engine.py`** ← USE THIS for room/bed conflicts
- `detect_conflicts(reservations, check_date)` → (RoomConflict[], CapacityWarning[])
- Internal: `_check_9300_dorm_capacity()` — Sunrise dorm capacity
- Internal: `_is_sunrise_9300_bed()` — classifies 9300-x beds individually

**`extras_engine.py`** — HotelRunner extra classification
- `classify_extras_from_reservation(raw_dict)` → list[NormalizedExtra]
- `get_transfer_dates_for_today()` — **LEGACY** — used by manager_report only
  - ⚠️ Known issue: manager_report._build_transfers() uses this instead of transfer_engine

---

### State Modules

**`alert_state.py`**
- `AlertStateStore` — prevents re-alerting same conflict every hour
- Persists to `alert_state.json`

**`transfer_state.py`**
- `TransferStateStore` — marks which transfers have been communicated to driver
- Persists to `transfer_state.json`
- User manually marks as sent; system reads this to show status badges

**`system_health.py`**
- `SystemHealthStore` with `record_fetch()`, `record_checker_run()`, `record_telegram_send()`
- `get_health_status()` → health dict with timestamps + stale-data warnings

---

### HotelRunner Data Layer

**`hotelrunner_daily_summary.py`** (≈2256 lines — **do not refactor without review**)
- `load_reservation_cache(path)` → list[dict] — raw HotelRunner JSON
- `active_stay_lines(reservations)` → list[StayLine]
- `StayLine` — frozen dataclass for one HotelRunner stay period
- `build_day_summaries(stay_lines, ...)` → list[DaySummary]
- `build_whatsapp_block(day_summary)` → **DO NOT MODIFY** (team-facing format)
- `build_manager_block(day_summary)` — manager summary block
- `build_dashboard_html(...)` → HTML string

**`google_sheets.py`** — Sunrise Google Sheet integration
- `load_sunrise_reservations()` → list[NormalizedReservation]
- `load_surf_schedule()` → list[dict] surf session data

---

### Output Surfaces

**`telegram_send.py`** — Two modes:

| Mode | Trigger | Behavior |
|------|---------|----------|
| `--mode full` | 06:00 UTC daily | Sends 2 messages: WhatsApp block + manager block |
| `--mode check` | Every hour | Compares snapshot vs current; silent if no changes |

Critical functions:
- `build_snapshot()` — captures today/tomorrow reservations
- `build_change_alert(old, new)` — diffs snapshots, returns alert string or None
- `send_with_retry()` — 3 retries, 5s backoff
- `log_run()` → `telegram_run_log.json`
- **CRITICAL**: snapshot saved ONLY AFTER confirmed successful send

**`tomorrow_transfers.py`** — Tomorrow preparation job

- `build_tomorrow_transfer_message(cache_path, today)` → message string
- Always sends (even "No transfers" = confirmation job ran)
- Scheduled: 17:00 + 20:00 Morocco time (16:00 + 19:00 UTC)
- Calls `transfer_engine.build_transfer_records(reservations, TOMORROW)`

**`manager_report.py`** — Morning manager report sections:
- Occupancy, Meals (via meal_engine), Surf, Transfers (via extras_engine — legacy), Conflicts

---

## Two Telegram Systems

```
A: Change Watcher  (telegram_send.py --mode check)
   ├── Runs: every hour (GitHub Actions cron)
   ├── Compares: current snapshot vs last saved snapshot
   ├── Sends: alert ONLY if reservation data changed
   ├── Silent: if nothing changed
   └── Purpose: notify about new bookings, changes, cancellations

B: Tomorrow Preparation  (tomorrow_transfers.py)
   ├── Runs: 17:00 + 20:00 Morocco local time
   ├── Scans: ALL reservations → finds departing/arriving TOMORROW
   ├── Sends: always (empty = "No transfers scheduled")
   └── Purpose: prepare drivers/logistics for the next day
```

### Why They Must Be Separate

A guest departing **tomorrow** with a transfer may have had their
reservation entered **3 days ago** with **no changes today**.

System A (Change Watcher) would be **silent** — nothing changed.
System B (Tomorrow Preparation) **MUST** still surface that transfer.

This is why `tomorrow_transfers.py` does **NOT** check for reservation
changes. It simply asks: "What do we need for tomorrow?"

---

## Known Architectural Issues

| Issue | Location | Priority |
|-------|----------|----------|
| `manager_report._build_transfers()` uses legacy `extras_engine.get_transfer_dates_for_today()` instead of `transfer_engine.build_transfer_records()` | `manager_report.py` L217 | Medium |
| Clement Arnoux appears twice in transfer records (likely 2 StayLines for same group booking) | `transfer_engine.py` / data | Low |
| `tomorrow_transfers._load_reservations()` duplicates `manager_report.load_all_reservations()` logic | `tomorrow_transfers.py` L50 | Low |

---

## File Map

```
HotelRunner Daily Summary/
├── AGENTS.md                          ← READ FIRST
├── config.json                        ← business rules configuration
├── data_model.py                      ← normalization layer (NormalizedReservation)
├── extras_engine.py                   ← HotelRunner extra classification
├── conflict_engine.py                 ← room/bed conflict detection
├── meal_engine.py                     ← meal entitlement engine
├── transfer_engine.py                 ← transfer detection + driver messages
├── transfer_state.py                  ← transfer sent/unsent state persistence
├── system_health.py                   ← automation health tracking
├── alert_state.py                     ← conflict alert dedup state
├── manager_report.py                  ← morning manager Telegram report
├── telegram_send.py                   ← change watcher + daily briefing
├── tomorrow_transfers.py              ← tomorrow preparation job
├── hotelrunner_daily_summary.py       ← HotelRunner API + HTML dashboard (≈2256 lines)
├── google_sheets.py                   ← Sunrise Google Sheet integration
├── surf_schedule.py                   ← surf conditions + session schedule
├── .github/
│   └── workflows/
│       └── daily_summary.yml          ← GitHub Actions automation
├── docs/
│   ├── ARCHITECTURE.md               ← this file
│   ├── OPERATIONS_RULES.md
│   ├── DATA_MODEL.md
│   ├── TELEGRAM_SPEC.md
│   ├── DASHBOARD_SPEC.md
│   ├── TEST_SCENARIOS.md
│   └── CHANGELOG.md
└── tests/
    ├── test_manager_assistant.py      ← 8 original regression tests
    ├── test_phase1_data_contract.py   ← 11 data model tests
    ├── test_phase2_meal_engine.py     ← 17 meal engine tests
    ├── test_phase4_transfer_engine.py ← 13 transfer engine tests
    ├── test_phase9_conflicts.py       ← 10 conflict/9300 dorm tests
    └── test_tomorrow_transfers.py     ← Etienne regression test
```
