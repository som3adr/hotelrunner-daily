# AGENTS.md — Coding Agent Instructions

This file MUST be read by any AI coding agent before modifying this project.
Do not skip this step. Do not assume. Investigate first.

---

## Start Here

Before ANY change, read these files in order:

1. `docs/ARCHITECTURE.md` — module map, data flow, what does what
2. `docs/OPERATIONS_RULES.md` — Olas business rules (rooms, meals, transfers)
3. `docs/DATA_MODEL.md` — NormalizedReservation and all dataclasses
4. `docs/TEST_SCENARIOS.md` — acceptance scenarios, including the Etienne test
5. `docs/CHANGELOG.md` — recent changes, known issues

---

## Golden Rules

### DO NOT invent business rules.
If code behavior conflicts with documented rules in `docs/OPERATIONS_RULES.md`,
STOP and raise a question. Do not silently choose one interpretation.

### DO NOT calculate the same information in two places.
All transfer detection MUST go through `transfer_engine.py`.
All meal entitlements MUST go through `meal_engine.py`.
All normalization MUST go through `data_model.py`.
See `docs/ARCHITECTURE.md` for the full dependency map.

### DO NOT rewrite working modules.
Extend them. If you need to change behavior, write a test first.

### DO NOT mark a task complete until acceptance scenarios pass.
See `docs/TEST_SCENARIOS.md` for required scenarios per feature.

### DO NOT combine Change Watcher and Tomorrow Preparation.
They are architecturally separate. See `docs/ARCHITECTURE.md#two-telegram-systems`.

### DO NOT touch `build_whatsapp_block()` in hotelrunner_daily_summary.py.
That is the live team-facing message format. It has been stable. Do not modify it.

### DO NOT remove emojis from user-facing Telegram messages without discussion.
The formatting is intentional. Change readability only with explicit approval.

---

## Development Workflow

For every significant change:

1. **READ** — understand the existing implementation
2. **PLAN** — explain what changes and why; stop for review on complex changes
3. **TEST FIRST** — add regression/acceptance test before implementing
4. **IMPLEMENT** — make the smallest change necessary
5. **VERIFY** — run `python -m pytest tests/ -v`
6. **REAL-WORLD CHECK** — verify against the actual Olas operational scenario
7. **REPORT** — what changed, what files, tests added, tests passed, assumptions made

---

## Quick Reference

| Concern | Module |
|---------|--------|
| Normalization | `data_model.py` |
| Transfer detection | `transfer_engine.py` |
| Meal entitlements | `meal_engine.py` |
| Room conflicts | `conflict_engine.py` |
| Extra classification | `extras_engine.py` |
| Telegram change watcher | `telegram_send.py` |
| Tomorrow preparation | `tomorrow_transfers.py` |
| Morning manager report | `manager_report.py` |
| HTML dashboard | `hotelrunner_daily_summary.py` (`build_dashboard_html`) |
| Transfer sent state | `transfer_state.py` |
| System health | `system_health.py` |
| Tests | `tests/` |

---

## Current Test Suite

```
python -m pytest tests/ -v
```

Expected: 60 tests, all passing.

Key test files:
- `tests/test_manager_assistant.py` — 8 original regression tests
- `tests/test_phase1_data_contract.py` — 11 data model tests
- `tests/test_phase2_meal_engine.py` — 17 meal engine tests
- `tests/test_phase4_transfer_engine.py` — 13 transfer engine tests
- `tests/test_phase9_conflicts.py` — 10 conflict/9300 dorm tests
- `tests/test_tomorrow_transfers.py` — Etienne scenario regression tests

---

## Known Issues / Open Items

- `manager_report.py` `_build_transfers()` still uses legacy
  `extras_engine.get_transfer_dates_for_today()` instead of
  `transfer_engine.build_transfer_records()`. This is a known
  architectural inconsistency to be resolved.
- Clement Arnoux appears twice in transfer records — likely two StayLines
  for the same group booking. Needs deduplication by reservation_id in
  `build_transfer_records()`.
- `tomorrow_transfers._load_reservations()` duplicates loading logic from
  `manager_report.load_all_reservations()`. Could be consolidated.
