# Changelog

---

## 2026-09-23 — Phases 1–9 Implementation

### Added

**New modules:**
- `meal_engine.py` — deduplicated, auditable meal entitlement engine
- `transfer_engine.py` — transfer detection + French driver message formatting
- `transfer_state.py` — persistent transfer sent/unsent state (→ `transfer_state.json`)
- `tomorrow_transfers.py` — independent daily tomorrow-transfer preparation job
- `system_health.py` — automation health tracking store

**New documentation:**
- `AGENTS.md` — agent onboarding, golden rules, dev workflow
- `docs/ARCHITECTURE.md` — module map, data flow, two-telegram-systems distinction
- `docs/OPERATIONS_RULES.md` — rooms, meals, transfers, verbatim from Olas ops
- `docs/DATA_MODEL.md` — all dataclasses with fields and key properties
- `docs/TELEGRAM_SPEC.md` — message formats, driver message, reliability contract
- `docs/DASHBOARD_SPEC.md` — dashboard layout, data sources
- `docs/TEST_SCENARIOS.md` — acceptance scenarios including Etienne regression
- `docs/CHANGELOG.md` — this file

**New tests:**
- `tests/test_phase1_data_contract.py` — 11 tests for dataclasses and house detection
- `tests/test_phase2_meal_engine.py` — 17 tests for meal engine + dinner setup rule
- `tests/test_phase4_transfer_engine.py` — 13 tests for transfer detection and message format
- `tests/test_phase9_conflicts.py` — 10 tests for 9300 bed conflicts and capacity
- `tests/test_tomorrow_transfers.py` — Etienne scenario regression tests

### Fixed

- **CRITICAL BUG** (`telegram_send.py`): snapshot was previously saved BEFORE
  confirming successful Telegram send. Fixed: snapshot now saved ONLY AFTER
  confirmed delivery. Previously a failed send would permanently lose the alert.
- `conflict_engine.py`: `_room_key()` now handles `None` bed gracefully
- `conflict_engine.py`: 9300 beds now tracked per-individual-bed (not as shared-room),
  allowing 9300-1 + 9300-2 simultaneously without conflict

### Changed

- `data_model.py`:
  - Added `MealEntitlement`, `TransferRecord`, `AttentionItem` dataclasses
  - Added `dinner_setup_rule()` function
  - `detect_house_from_stayline()` now checks 9200/9300/9400/9500/9600 → Sunrise FIRST
- `manager_report.py`:
  - `_build_meals()` now uses `meal_engine` for deduplicated, auditable counts with per-house breakdown
- `telegram_send.py`:
  - `send_text()` now returns `bool` (was void)
  - Added `send_with_retry()` (3 retries, 5s backoff)
  - Added `log_run()` → `telegram_run_log.json`
- `config.json`:
  - Added `meals.dinner_thresholds` (13/24 guests)
  - Added `sunrise_hotelrunner` section (9200–9600 prefixes, 9300 dorm config)
  - Added `scheduled_jobs.tomorrow_transfer_times_local` (17:00, 20:00)
- `hotelrunner_daily_summary.py`:
  - `build_dashboard_html()` now uses `meal_engine`, `transfer_engine`, `transfer_state`, `system_health`
  - Dashboard shows: normalized meal totals + per-house breakdown + dinner prep notice + audit trail
  - Dashboard shows: transfer cards with status badges + French driver message + Copy button
  - Dashboard footer: System Health section
- `.github/workflows/daily_summary.yml`:
  - Added `transfers` option to manual dispatch
  - Added `Send Tomorrow Transfers` step at 16:00 + 19:00 UTC

### Test Count

**60/60 tests pass** as of end of session.

---

### Known Issues (open as of 2026-09-23)

1. **manager_report._build_transfers() uses legacy `extras_engine.get_transfer_dates_for_today()`**
   instead of `transfer_engine.build_transfer_records()`. The manager report's
   transfer section does not use the same engine as the dashboard and tomorrow preparation.
   **Impact**: Transfer detection in the morning manager report may differ from dashboard.
   **Priority**: Medium — fix when manager report transfers section is next revisited.

2. **Clement Arnoux appears twice in transfer records** — likely two separate `StayLine`
   records for the same group booking in HotelRunner. The transfer engine builds one
   record per StayLine, causing duplicate entries.
   **Fix needed**: Deduplicate by `(reservation_id, direction, date)` in `build_transfer_records()`.
   **Priority**: Low — cosmetic duplication, not a data loss issue.

3. **`tomorrow_transfers._load_reservations()` duplicates loading logic** from
   `manager_report.load_all_reservations()`. Both functions do the same thing:
   load HotelRunner cache + Sunrise sheet + attach extras.
   **Fix**: Extract a shared `load_all_normalized_reservations(cache_path)` utility.
   **Priority**: Low — not causing bugs, just code duplication.

---

## Pre-2026-09-23 — Baseline

- HotelRunner API integration, StayLine parsing, room block detection
- WhatsApp team message generation (`build_whatsapp_block()`)
- HTML dashboard with Tailwind CSS, dark theme, multi-day navigation
- GitHub Actions deployment to GitHub Pages
- Change watcher with snapshot diff (`telegram_send.py --mode check`)
- Room conflict engine (`conflict_engine.py`)
- Extras classification (`extras_engine.py`)
- Surf schedule + conditions (`surf_schedule.py`)
- Google Sheets integration for Sunrise property (`google_sheets.py`)
- Alert state deduplication (`alert_state.py`)
- 8 baseline regression tests (`tests/test_manager_assistant.py`)
