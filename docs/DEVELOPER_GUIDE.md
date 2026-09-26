# HotelRunner Daily Operations Assistant

## Developer Guide

**Project:** Olas Surf Experience daily operations assistant  
**Location:** Imsouane, Morocco  
**Properties:** Olas, Tide, and Sunrise/SandyCamp  
**Primary interfaces:** HTML dashboard and Telegram  
**Production platform:** GitHub Actions and GitHub Pages

## 1. Purpose

This application turns reservation data into a daily operational briefing for
a small hospitality and surf-camp team. Its job is not to replace
HotelRunner. It combines HotelRunner with the Sunrise Google Sheet and makes
time-sensitive work difficult to miss.

The system helps the team answer:

- Who checks in and out today?
- Who is staying in each house and room?
- How many breakfasts, lunches, and dinners are required?
- Which guests need transfers today or tomorrow?
- Which checkout payments and extras need to be verified?
- Are any rooms over capacity or double booked?
- Which Sunrise guests still need to be added to HotelRunner?
- Has a booking changed since the previous check?

The application is read-only with respect to HotelRunner and Google Sheets.
It never creates, edits, confirms, or cancels a reservation.

## 2. Operational Context

The business operates three houses.

### Olas

The main surf camp. It contains private rooms and two six-bed dormitories.

- RDC1: two guests, double or twin
- RDC2: two guests, double or twin
- Balcony: two guests, double or twin
- RDC3: up to three guests, private bathroom
- Two dormitories: six beds each

### Tide

Four private rooms with private bathrooms.

- Bay: two guests
- Cathedral: up to four guests
- Slab: up to three guests; private bathroom outside the room
- Reef: up to three guests

### Sunrise / SandyCamp

The third house is managed partly by an external partner. Reservations can
exist in HotelRunner, the Sunrise Google Sheet, or both.

- Sunrise 1: three guests
- Sunrise 2: five-bed dormitory
- Sunrise 3: four guests
- Sunrise 4: three guests
- Sunrise 5: three single beds

When the same guest and stay dates exist in both sources, the application
merges the records for operational calculations. The Google Sheet entry is
not deleted because it remains useful to the external partner.

## 3. System Overview

The project follows a normalization-and-domain-engine design:

```text
HotelRunner API             Sunrise Google Sheet
       |                              |
       +--------------+---------------+
                      |
              Normalization layer
                 data_model.py
                      |
       +--------------+----------------+----------------+
       |              |                |                |
   meal_engine   transfer_engine  settlement_engine  conflict_engine
       |              |                |                |
       +--------------+----------------+----------------+
                      |
       +--------------+----------------+----------------+
       |              |                |                |
 HTML dashboard  Team Telegram  Manager Telegram  Telegram Q&A
```

Business rules belong in domain engines. Output modules should consume those
engines instead of recalculating meals, transfers, payments, or conflicts.

## 4. Data Sources

### HotelRunner

HotelRunner is the primary reservation source. The application fetches active
and recently updated reservations and stores a local cache for reliability.

Important fields include:

- Reservation and HotelRunner identifiers
- Guest name and guest count
- Check-in and check-out dates
- House, room, and bed identifier
- Booking channel
- Meal plan
- Notes and bed requests
- Additional fees and extras
- Reservation total, paid amount, and payment records

### Sunrise Google Sheet

The Sheet supplies Sunrise information that may not yet exist in HotelRunner:

- Guest name and dates
- Sunrise room
- Surf package and level
- Meal plan information
- Dietary, allergy, and medical notes
- Estimated arrival and departure details

Google Sheets access is read-only. In GitHub Actions, credentials are supplied
through the `GOOGLE_CREDENTIALS_JSON` secret.

### External Conditions

The surf scheduling module can read marine and weather conditions. These are
supporting operational signals and do not alter reservations.

## 5. Normalized Data Model

`data_model.py` converts every source into `NormalizedReservation`.
Important normalized fields are:

- Identity: reservation ID, HotelRunner number, source
- Guest: name, adults, children
- Location: house, room, bed
- Stay: arrival and departure dates
- Booking: meal plan, status, channel, booking date
- Extras: classified additional fees
- Transfers: arrival and departure details
- Surf and health information
- Payment: total, paid amount, and payment-record count

Departure is exclusive for occupancy:

```text
arrival_date <= active night < departure_date
```

A guest checking out today is not in-house tonight, but is still a departure
for payment and transfer calculations.

## 6. Domain Engines

### Meal Engine

`meal_engine.py` determines meal entitlement from:

1. HotelRunner meal plan
2. Additional fees such as Half Board or Full Board
3. Explicit meal notes
4. Sunrise package information

Meal meanings:

- Bed and Breakfast: breakfast
- Half Board: breakfast and dinner
- Full Board: breakfast, lunch, and dinner
- All Inclusive: breakfast, lunch, and dinner

One guest is counted once per meal even when several sources confirm the same
entitlement. The dashboard retains an audit trail explaining why each guest
was counted.

The dashboard also provides a copyable dinner message grouped in this order:

1. Sunrise/SandyCamp
2. Olas
3. Tide

Dietary details are included when useful. The detailed audit remains visible
separately.

Dinner preparation thresholds:

- 0-13 covers: normal setup
- 14-24 covers: extra tables
- More than 24 covers: capacity warning

### Transfer Engine

`transfer_engine.py` is the single source of truth for transfer relevance.

A transfer may be detected from:

- An All Inclusive package
- Reservation notes
- A HotelRunner transfer extra
- Explicit Sheet transfer information

Action windows:

- Arrival transfer: one day before arrival and on arrival day
- Departure transfer: one day before departure and on departure day
- Mid-stay or old transfer extras: suppressed
- Unknown direction or date: requires confirmation

The application prepares a French driver message but does not automatically
send it to a driver.

### Settlement Engine

`settlement_engine.py` creates final-payment reminders for Online,
Hostelworld, and Surf Camp bookings.

Reminders appear:

- One day before checkout
- On checkout day

The remaining accommodation amount is:

```text
reservation total - recorded paid amount
```

Payment behavior:

- Recorded partial payment: show the recorded balance
- Fully paid accommodation: still verify extras
- No payment record: collect the full amount or confirm PayPal
- Unknown reservation total: request manual confirmation

Surf Camp deposit policy:

- Booking completed before 1 September 2026: expected 50% deposit
- Booking completed on or after 1 September 2026: expected 20% deposit
- Actual recorded payment always takes priority

The closing checklist can include meals, surf lessons, Timlalin, board rental,
and transfers. It also reminds the manager to check group messages for extras
that were never entered into HotelRunner.

### Conflict Engine

`conflict_engine.py` detects:

- Overlapping bookings in private rooms
- A reservation exceeding physical room capacity
- Dorm occupancy exceeding bed capacity
- Two reservations assigned to the same Sunrise dorm bed

Sunrise HotelRunner identifiers `9300-1` through `9300-5` represent five
beds in one physical dorm, not five separate rooms.

### Extras Engine

`extras_engine.py` classifies additional fees as:

- Meal
- Transfer
- Surf lesson
- Surf equipment
- Tax
- Unknown

Unknown operational extras remain visible for confirmation rather than being
silently ignored.

## 7. Output Surfaces

### HTML Dashboard

`hotelrunner_daily_summary.py` generates
`hotelrunner_dashboard.html`.

The dashboard includes:

- Olas branding and browser favicon
- Today plus the next seven days
- Arrivals, departures, and in-house totals
- Meals and dinner preparation
- Copyable team dinner list
- Dinner audit trail
- Transfers and copyable driver messages
- Checkout payment reminders
- Copyable team report
- Room disposition and conflict warnings
- System-health information

The generated HTML contains private guest data and is intentionally excluded
from Git history. GitHub Actions generates it at runtime and deploys it to
GitHub Pages.

### Team Telegram Report

`telegram_send.py --mode full --team-only` sends the concise operational
check-in/check-out report. This format is relied on by the team and should not
be changed casually.

### Manager Report

`manager_report.py --send` sends a separate report containing occupancy,
surf, meals, diet and health notes, transfers, checkout payments, conflicts,
and important extras.

### Change Watcher

`telegram_send.py --mode check` compares the current reservation snapshot
with the last successfully delivered snapshot.

It is silent when nothing changed. The snapshot advances only after Telegram
confirms successful delivery, allowing failed sends to retry.

### Tomorrow Transfer Preparation

`tomorrow_transfers.py --send` scans all reservations for tomorrow's
transfers. It runs independently of the change watcher because a transfer can
become relevant tomorrow even when the reservation did not change today.

### Telegram Q&A

`telegram_bot.py --poll` processes questions from the configured Telegram
chat only. It builds a current operational context and sends it to CodeCraft.

`codecraft_client.py` supports:

- Optional `CODECRAFT_MODEL` configuration
- Live model discovery for the configured API key
- Fallback to another compatible chat model
- Standard browser-like request headers required by CodeCraft

The bot is scheduled every five minutes outside quiet hours. It is not a
continuously running webhook service, so responses can take several minutes.

### HotelRunner GRM Conversation Boundary

The reservation API can expose notes and special-request fields, and those are
parsed for operational requests. The separate HotelRunner GRM conversation
inbox is not available through the documented custom-app reservation API and
is not currently ingested. The dashboard must not interpret an empty request
field as proof that the guest sent no message. Until HotelRunner provides a
supported conversation endpoint, important GRM requests need a reservation
note or another structured operational source.

## 8. Automation

Production automation is defined in:

```text
.github/workflows/daily_summary.yml
```

The workflow:

1. Checks out the repository
2. Installs Python dependencies
3. Restores cache and state files
4. Fetches fresh HotelRunner data
5. Regenerates the dashboard
6. Selects full, check, or transfers mode
7. Sends the relevant Telegram reports
8. Saves cache and state
9. Publishes the HTML dashboard and logo to GitHub Pages
10. Runs the optional CodeCraft operations monitor after deployment

Telegram Q&A polling runs in the separate `telegram_qa.yml` workflow every
five minutes outside quiet hours.

Modes:

- `full`: team report and manager report
- `check`: change watcher; silent when there is no change
- `transfers`: tomorrow-transfer preparation

The manual `full` workflow sends both reports regardless of the minute when
it is started.

## 9. Configuration And Secrets

### Local `.env`

Required values depend on the feature being run:

```text
HOTELRUNNER_TOKEN
HOTELRUNNER_HR_ID
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
CODECRAFT_API_KEY
CODECRAFT_MODEL              # optional
GOOGLE_CREDENTIALS_JSON      # or a local credentials file
```

### GitHub Actions Secrets

Production requires:

- `HOTELRUNNER_TOKEN`
- `HOTELRUNNER_HR_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `CODECRAFT_API_KEY`
- `GOOGLE_CREDENTIALS_JSON`
- `CODECRAFT_MODEL` when an explicit model is preferred

Never commit real tokens, Telegram identifiers, Google credentials, raw
reservation caches, generated dashboards, or guest debug files.

## 10. Local Development

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Generate the report and dashboard:

```powershell
python hotelrunner_daily_summary.py --max-pages 15 --page-delay 1
```

Preview the team report without sending:

```powershell
python telegram_send.py --mode full --team-only --dry-run
```

Preview the manager report:

```powershell
python manager_report.py --dry-run
```

Preview tomorrow's transfers:

```powershell
python tomorrow_transfers.py --dry-run
```

Process Telegram questions:

```powershell
python telegram_bot.py --poll
```

Windows shortcuts:

- `run_morning.bat`: refresh dashboard, send team report, send manager report
- `run_check.bat`: check for reservation changes

## 11. Testing

Run the complete suite:

```powershell
python -m pytest tests/ -v
```

The suite covers:

- Source normalization and house detection
- Meal plans, meal extras, and dinner deduplication
- Transfer relevance and missing information
- Mid-stay suppression of old transfer extras
- Today and tomorrow departure transfers
- Room conflicts and Sunrise dorm-bed behavior
- Google Sheet and HotelRunner cross-source merging
- Payment reminders and Surf Camp deposit policies
- Guest-request suppression after check-in
- Copyable dinner and payment dashboard panels
- Gemini 404 fallback behavior
- Morning dashboard generation

For a behavior change, add a regression test before modifying the engine.

## 12. State And Generated Files

Runtime state is intentionally not committed:

- `reservations_cache.json`
- `hotelrunner_snapshot.json`
- `hotelrunner_latest_updates.json`
- `alert_state.json`
- `transfer_state.json`
- `telegram_bot_state.json`
- `operations_monitor_state.json`
- `telegram_run_log.json`
- `hotelrunner_dashboard.html`
- Audit and guest debug files

GitHub Actions persists required state through the Actions cache.

## 13. Reliability And Privacy

Important guarantees:

- Reservation sources are read-only
- Telegram output is restricted to the configured chat
- Change state is saved only after successful Telegram delivery
- Unknown information is marked for confirmation instead of guessed
- Generated guest data is excluded from Git
- Secrets are supplied only by local environment or GitHub Secrets
- Dashboard deployment is runtime-only

The current GitHub Pages dashboard contains operational guest information.
Repository visibility and Pages access should therefore be reviewed whenever
the team changes its privacy requirements.

## 14. Development Rules

Before changing the application:

1. Read `AGENTS.md`
2. Read the relevant files in `docs/`
3. Add a regression test
4. Extend the existing engine instead of duplicating business logic
5. Run the complete test suite
6. Generate and inspect the real HTML
7. Run a production acceptance test for Telegram or deployment changes

Do not:

- Recalculate meals outside `meal_engine.py`
- Recalculate transfer relevance outside `transfer_engine.py`
- Recalculate checkout settlement outside `settlement_engine.py`
- Change the stable team message format without approval
- Commit secrets or guest data
- Treat a missing payment record as proof that no external payment occurred

## 15. Extension Guide

For a new HotelRunner field:

1. Capture it in `StayLine` if required by the dashboard
2. Add it to `NormalizedReservation`
3. Normalize it in `data_model.py`
4. Add engine-level behavior and tests
5. Expose the engine result to each required output surface

For a new extra:

1. Add classification keywords to `config.json`
2. Classify it in `extras_engine.py`
3. Decide whether it affects meals, transfers, settlement, or only attention
4. Add a regression test using a synthetic guest

For a new output:

Consume normalized reservations and domain-engine results. Do not parse raw
HotelRunner data again in the output module.

## 16. Known Limitations And Acceptance Checks

- Telegram Q&A is polling-based, not immediate.
- External PayPal payments may require manual confirmation.
- Extras mentioned only in group chat cannot be calculated automatically.
- Google Sheet data is unavailable locally without Google credentials.
- GitHub cron execution time can drift by several minutes.
- The HTML dashboard is read-only.
- Transfer sent status is manual.

After deployment, verify:

1. HotelRunner fetch succeeds
2. Sunrise Sheet data loads
3. Team Telegram sends
4. Manager Telegram sends
5. Telegram Q&A answers a real question
6. Tomorrow transfers send at the expected times
7. GitHub Pages shows the current generation timestamp and Olas logo

## 17. Documentation Map

- `README.md`: quick start
- `AGENTS.md`: mandatory coding rules
- `docs/DEVELOPER_GUIDE.md`: complete product and engineering overview
- `docs/ARCHITECTURE.md`: module-level architecture
- `docs/OPERATIONS_RULES.md`: business rules
- `docs/DATA_MODEL.md`: normalized types
- `docs/TELEGRAM_SPEC.md`: Telegram behavior
- `docs/DASHBOARD_SPEC.md`: dashboard structure
- `docs/TEST_SCENARIOS.md`: acceptance scenarios
- `docs/CHANGELOG.md`: implementation history

