# Operations Rules — Olas Surf Camp, Imsouane

This document defines the **operational business rules** for the property.
Do NOT invent rules. If behavior is unclear, mark it as UNCONFIRMED and ask.

---

## Properties

Three houses managed together:

| House | Description |
|-------|-------------|
| **Olas** | Main surf camp (hostel-style, dorms and private rooms) |
| **Tide** | Secondary property |
| **Sunrise** | Third property — guests come from HotelRunner AND Google Sheet |

---

## Sunrise Room IDs (HotelRunner)

Sunrise guests booked via HotelRunner use room/bed IDs starting with:

| ID Prefix | Type | Notes |
|-----------|------|-------|
| 9200 | Private room | — |
| 9300 | Dorm room | 1 physical room with 5 beds |
| 9300-1 | Dorm bed | One of 5 beds in the 9300 dorm |
| 9300-2 | Dorm bed | One of 5 beds |
| 9300-3 | Dorm bed | One of 5 beds |
| 9300-4 | Dorm bed | One of 5 beds |
| 9300-5 | Dorm bed | One of 5 beds |
| 9400 | Private room | — |
| 9500 | Private room | — |
| 9600 | Private room | — |

### Critical: 9300 Dorm Logic

**9300-1 through 9300-5 are NOT five rooms. They are five beds in ONE room.**

- `9300-1` + `9300-2` occupied simultaneously = **NORMAL** (different beds)
- Two reservations on `9300-1` overlapping = **BED CONFLICT**
- Dashboard goal: show `Sunrise / 9300 Dorm / 4/5 beds occupied`
- Capacity: 5 beds maximum

**The user manually adds Sunrise guests in HotelRunner** using bed IDs 9300-1 through 9300-5.

---

## Meal Rules

### Meal Plans

| Plan | Breakfast | Lunch | Dinner |
|------|-----------|-------|--------|
| Room Only | No | No | No |
| Bed And Breakfast | Yes | No | No |
| Half Board | Yes | No | Yes |
| Full Board | Yes | Yes | Yes |
| All Inclusive | Yes | Yes | Yes |

### Dinner Count Rule

The system must answer: **"How many people have dinner tonight?"**

Sources of dinner information (in priority order):
1. HotelRunner meal plan
2. HotelRunner Additional Fees / extras (explicit dinner extra)
3. Reservation notes (explicitly parsed)
4. Sunrise Google Sheet (meal plan inferred from surf package)

**Deduplication rule**: A guest must NOT be counted twice because dinner
appears in multiple sources. One guest = one dinner count, regardless of
how many sources confirm it.

**Audit requirement**: The total must be inspectable — show WHY each
person was counted.

**Ambiguity rule**: Unknown or ambiguous meal information → `NEEDS CONFIRMATION`.
Do NOT guess.

### Dinner Setup Thresholds

| Dinner Guests | Setup |
|---------------|-------|
| 0–13 | Normal dining setup |
| 14–24 | Extra tables required |
| >24 | Capacity warning — manager attention |

Olas can accommodate up to **24** with the extra table setup.

> **Do NOT invent how many extra tables are needed** — this has not been precisely defined.

---

## Transfer Rules

### When Does a Transfer Exist?

A transfer is detected when **ANY** of these is true:
1. Meal plan is **All Inclusive** (transfer is included in the package)
2. Reservation notes mention transfer keywords
3. HotelRunner Additional Fees / extras contain a transfer line

**Do NOT assume every arrival/departure has a transfer.**

### Transfer Preparation Workflow

Transfers are organized **one day before** (tomorrow preparation, not today action).

The driver message format (established by Olas management):

**Arrival:**
```
Arrivée *Olas*
20 septembre 2026
*Raiz Fatehmahomed* X1
Flight number: HV6491
Agadir airport
```

**Departure:**
```
Depart *Tide*
18 septembre 2026
*Luke* X2
Time: 15:30
Zephyr Agadir
```

### Transfer Status States

| State | Meaning |
|-------|---------|
| `needs_info` | Transfer detected but flight/time/airport missing |
| `ready_to_send` | All information available to send driver message |
| `sent` | Driver has been informed (marked manually by user) |

**Automatic WhatsApp/driver-message sending is NOT implemented.**
The current workflow is: Copy Driver Message → send manually.

The **user confirms sent status manually** (stored in `transfer_state.json`).

### Default Airport

All operations based in Imsouane → default airport: **Agadir airport**

### Late Transfer Changes

If a transfer is added to a reservation at 21:30 after the 20:00 run:
- The Change Watcher (running hourly) detects the reservation change
- Both systems (tomorrow_transfers + change_watcher) use the SAME `transfer_engine.py`
- **Do NOT implement transfer parsing in two places**

---

## Telegram Readability Rules

From user (verbatim):
> "Avoid excessive emojis and decorative symbols. Use simple headings, whitespace, separators. Red/orange indicators for actual attention only."

---

## Scheduled Times

- Tomorrow preparation jobs: **17:00 and 20:00** Morocco local time (Africa/Casablanca)
- Configured in `config.json` > `scheduled_jobs` > `tomorrow_transfer_times_local`
- In GitHub Actions UTC: 16:00 and 19:00 UTC

---

## UNCONFIRMED / Open Items

| Item | Status |
|------|--------|
| Exact number of extra tables for 14–24 dinner guests | Not yet defined |
| Automatic driver message sending | Not yet requested |
| Future Telegram conversational agent | Not yet implemented — do not prioritize |
