# HotelRunner Daily Summary

> New developers should begin with the complete
> [Developer Guide](docs/DEVELOPER_GUIDE.md), which explains the product,
> architecture, business rules, automation, privacy model, and deployment.

This folder contains a read-only HotelRunner report script.

It is separate from the invoice files so it can be kept, moved, or deleted without touching the invoice work.

## Files

- `hotelrunner_daily_summary.py` creates the daily operations report.
- `.env.example` shows the private settings needed by the script.
- `.env` is the private file you create yourself with your real HotelRunner token.
- `hotelrunner_blocks.example.json` shows how to add manual room/house blocks.
- `hotelrunner_blocks.json` is optional. Create it when you want the dashboard to count blocks and warn about reservation overlaps.

## Setup

Create a file named `.env` in this folder:

```text
HOTELRUNNER_TOKEN=your_real_token
HOTELRUNNER_HR_ID=410082883
```

## Run

Open this folder in the terminal, then run:

```powershell
python hotelrunner_daily_summary.py
```

The script prints the report and saves:

```text
hotelrunner_daily_summary.md
hotelrunner_dashboard.html
```

## What It Does

- Reads HotelRunner reservations.
- Covers today and the next 7 days.
- Shows check-ins, check-outs, in-house totals, meal plans, notes, messages, bed requests, and possible duplicate/multi-bed situations.
- Creates a local visual dashboard you can open in your browser.
- Does not change, confirm, cancel, or update reservations.

## Useful Commands

Create the normal report and dashboard:

```powershell
python hotelrunner_daily_summary.py
```

The normal run uses `reservations_cache.json` and only asks HotelRunner for reservations updated in the last 14 days.

Daily shortcut:

```powershell
run.bat
```

One-time cache bootstrap or deep repair:

```powershell
python hotelrunner_daily_summary.py --lookback-days 730 --max-pages 40 --page-delay 2 --retries 5 --retry-wait 45
```

Create only the Markdown report:

```powershell
python hotelrunner_daily_summary.py --no-dashboard
```

Search older reservations too, for example reservations created in the last 2 years:

```powershell
python hotelrunner_daily_summary.py --lookback-days 730
```

Use manual room/house blocks:

```powershell
copy hotelrunner_blocks.example.json hotelrunner_blocks.json
notepad hotelrunner_blocks.json
python hotelrunner_daily_summary.py
```

Inspect one guest privately when meal plans or duplicates look wrong:

```powershell
python hotelrunner_daily_summary.py --find-guest GuestName
```

Inspect one room or bed privately, for example The Bay room `6500`:

```powershell
python hotelrunner_daily_summary.py --find-room 6500 --lookback-days 50 --max-pages 8 --page-delay 1
```

If the HotelRunner calendar shows a reservation that is missing from the report, run a deeper room check:

```powershell
python hotelrunner_daily_summary.py --find-room 6500 --lookback-days 730 --max-pages 40 --page-delay 2 --retries 5 --retry-wait 45
```

The normal run also saves `hotelrunner_audit.json`, which summarizes fetched reservations, active lines, conflicts, duplicates, and bed requests.

## Remove

If you do not want this tool anymore, delete this whole folder:

```text
HotelRunner Daily Summary
```
