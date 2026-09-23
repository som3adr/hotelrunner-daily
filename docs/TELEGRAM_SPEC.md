# Telegram Specification

## Two Systems — Distinct Purposes

See `docs/ARCHITECTURE.md#two-telegram-systems` for full context.

| System | Script | Trigger | Always sends? |
|--------|--------|---------|---------------|
| Change Watcher | `telegram_send.py --mode check` | Hourly | No (silent if no changes) |
| Tomorrow Preparation | `tomorrow_transfers.py --send` | 17:00 + 20:00 Morocco | Yes |
| Daily Briefing | `telegram_send.py --mode full` | 07:00 Morocco | Yes |
| Manager Report | `manager_report.py --send` | 07:00 Morocco | Yes |

---

## Message Formats

### Change Watcher Alert

Triggered when reservation data changes since last snapshot.

```
HOTELRUNNER UPDATE · 23 Sep · 14:30

➕ NEW: Guest Name x2 · Room · 2026-09-25 → 2026-09-28 · Half Board
✏️ CHANGED: Guest Name · Room · meal_plan: Room Only → Half Board
❌ CANCELLED: Guest Name · Room · 2026-09-25 → 2026-09-28
```

### Tomorrow Transfers

Always sends — even "no transfers" is an operational confirmation.

With transfers:
```
TOMORROW — TRANSFERS
24 September 2026

READY TO SEND

Departure — Olas
Etienne Aleveque X1
Time: 08:15
Agadir Airport

Driver message:
Depart *Olas*
24 septembre 2026
*Etienne Aleveque* X1
Time: 08:15
Agadir airport
------------------------------

NEEDS INFORMATION

Departure — Olas
Guest Name X2
⚠️ Pickup time missing
------------------------------

SENT
✓ Departure — Olas — Guest Name
```

Without transfers:
```
TOMORROW — TRANSFERS
24 September 2026

No transfers tomorrow — all clear ✓
```

---

## Driver Message Format

**This format is established. Do NOT change without explicit approval.**

**Arrival:**
```
Arrivée *{house}*
{day} {month_french} {year}
*{guest_name}* X{passenger_count}
Flight number: {flight_number}
{airport}
```

**Departure:**
```
Depart *{house}*
{day} {month_french} {year}
*{guest_name}* X{passenger_count}
Time: {pickup_time}
{destination}
```

French month names:
`janvier, février, mars, avril, mai, juin,
juillet, août, septembre, octobre, novembre, décembre`

If flight/time is missing, shows `(missing)` placeholder — not omitted.

---

## Scheduled Run Times

| UTC Time | Morocco Time | Action |
|----------|-------------|--------|
| 06:00 | 07:00 | Full daily briefing + manager report |
| 07:00–22:00 | 08:00–23:00 | Change watcher (hourly) |
| 16:00 | 17:00 | Tomorrow transfers |
| 19:00 | 20:00 | Tomorrow transfers |
| 00:01–05:59 | 01:01–06:59 | Quiet hours — no notifications |

Morocco is UTC+1 (Africa/Casablanca). During daylight saving it may vary.

---

## Telegram Reliability Contract

Safe sequence — must be followed in this order:

1. FETCH FRESH DATA (from HotelRunner cache or API)
2. NORMALIZE to NormalizedReservation
3. CALCULATE operational state (meals, transfers, conflicts)
4. COMPARE snapshots (if change-watcher mode)
5. SEND TELEGRAM message
6. CONFIRM send success (OK response from Telegram API)
7. ONLY THEN: save snapshot / mark as processed

**If step 5 or 6 fails**:
- Do NOT advance the snapshot
- Do NOT mark as sent
- The next run will retry automatically

**If step 6 confirms success**:
- Save snapshot
- Log run to `telegram_run_log.json`

This is implemented in `telegram_send.py` as of 2026-09-23 (previously had a bug
where snapshot was saved BEFORE the send).

---

## Quiet Hours Logic

```python
morocco_hour = (datetime.now(timezone.utc).hour + 1) % 24
if 1 <= morocco_hour <= 6:
    print("Quiet hours. No notifications until 07:00.")
    return
```

Implemented in `telegram_send.py` check mode only.
`tomorrow_transfers.py` is protected by the GitHub Actions schedule
(only runs at 16:00 and 19:00 UTC).

---

## Run Logging

Every run appends to `telegram_run_log.json`:

```json
{
  "timestamp": "2026-09-23T17:00:05",
  "mode": "check",
  "fetch_success": true,
  "records_processed": 14,
  "alert_count": 1,
  "telegram_result": true,
  "error": ""
}
```

Last 100 entries kept. Use this to diagnose missed notifications.
