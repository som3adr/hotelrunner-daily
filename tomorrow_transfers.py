"""
tomorrow_transfers.py
─────────────────────
Independent daily preparation job for tomorrow's transfers.

Always sends a result — even 'No transfers tomorrow' — so the user
knows the job ran successfully.

Scheduled times (Morocco local / Africa/Casablanca): 17:00 and 20:00
Configured in config.json > scheduled_jobs > tomorrow_transfer_times_local

Usage:
  python tomorrow_transfers.py [--dry-run] [--date YYYY-MM-DD] [--send]

Required env vars for --send:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data_model import load_config
from transfer_engine import build_transfer_records, format_driver_message
from transfer_state import TransferStateStore


DIVIDER = "-" * 30


def _load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _load_reservations(cache_path: Path):
    """Load normalized reservations from cache (HotelRunner + Sunrise)."""
    from hotelrunner_daily_summary import load_reservation_cache, active_stay_lines
    from data_model import stayline_to_normalized
    from extras_engine import classify_extras_from_reservation
    from google_sheets import load_sunrise_reservations

    raw_reservations = load_reservation_cache(cache_path)
    raw_by_id = {str(r.get("reservation_id") or ""): r for r in raw_reservations}
    stay_lines = active_stay_lines(raw_reservations)

    hr_reservations = []
    for line in stay_lines:
        raw = raw_by_id.get(line.reservation_id)
        res = stayline_to_normalized(line, raw)
        if raw:
            res.extras = classify_extras_from_reservation(raw)
        hr_reservations.append(res)

    try:
        sunrise = load_sunrise_reservations()
    except Exception as exc:
        print(f"[tomorrow_transfers] Sunrise data unavailable: {exc}")
        sunrise = []

    return hr_reservations + sunrise


def build_tomorrow_transfer_message(
    cache_path: Path,
    date: dt.date,
    store: TransferStateStore | None = None,
) -> str:
    """
    Build the tomorrow-transfer preparation message.
    Always returns a message string (never silent).
    'date' is today — tomorrow = date + 1 day.
    """
    if store is None:
        store = TransferStateStore()

    tomorrow = date + dt.timedelta(days=1)
    reservations = _load_reservations(cache_path)
    records = build_transfer_records(reservations, tomorrow, store)

    # Sort into sections
    ready = [r for r in records if r.status == "ready_to_send"]
    needs_info = [r for r in records if r.status == "needs_info"]
    sent = [r for r in records if r.status == "sent"]

    tomorrow_str = tomorrow.strftime("%d %B %Y")
    lines = [f"TOMORROW — TRANSFERS\n{tomorrow_str}"]

    if not records:
        lines.append("\nNo transfers tomorrow — all clear ✓")
        return "\n".join(lines)

    if ready:
        lines.append("\nREADY TO SEND")
        for r in ready:
            direction = "Arrival" if r.is_arrival else "Departure"
            lines.append(f"\n{direction} — {r.house}")
            lines.append(f"{r.guest_name} X{r.passenger_count}")
            if r.is_arrival:
                if r.flight_number:
                    lines.append(f"Flight: {r.flight_number}")
                lines.append(r.airport or "Agadir Airport")
            else:
                if r.pickup_time:
                    lines.append(f"Time: {r.pickup_time}")
                lines.append(r.destination or "Agadir Airport")
            lines.append("\nDriver message:")
            lines.append(format_driver_message(r))
            lines.append(DIVIDER)

    if needs_info:
        lines.append("\nNEEDS INFORMATION")
        for r in needs_info:
            direction = "Arrival" if r.is_arrival else "Departure"
            lines.append(f"\n{direction} — {r.house}")
            lines.append(f"{r.guest_name} X{r.passenger_count}")
            if r.is_arrival and not r.flight_number:
                lines.append("⚠️ Flight number missing")
            if r.is_arrival and not r.airport:
                lines.append("⚠️ Airport missing")
            if r.is_departure and not r.pickup_time:
                lines.append("⚠️ Pickup time missing")
            if r.is_departure and not r.destination:
                lines.append("⚠️ Destination missing")
            lines.append(DIVIDER)

    if sent:
        lines.append("\nSENT")
        for r in sent:
            direction = "Arrival" if r.is_arrival else "Departure"
            lines.append(f"✓ {direction} — {r.house} — {r.guest_name}")

    return "\n".join(lines)


def _telegram_send(token: str, chat_id: str, text: str) -> bool:
    """Send text to Telegram. Returns True on success."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return bool(result.get("ok"))
    except Exception as exc:
        print(f"[tomorrow_transfers] Telegram error: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily tomorrow-transfer preparation job")
    parser.add_argument("--dry-run", action="store_true", help="Print without sending")
    parser.add_argument("--date", help="Base date YYYY-MM-DD (default: today)")
    parser.add_argument("--cache-file", default="reservations_cache.json")
    parser.add_argument("--send", action="store_true", help="Send via Telegram")
    args = parser.parse_args()

    _load_env(Path(".env"))

    target_date = dt.date.today()
    if args.date:
        try:
            target_date = dt.date.fromisoformat(args.date)
        except ValueError:
            sys.exit("Invalid --date. Use YYYY-MM-DD.")

    cache_path = Path(args.cache_file)
    if not cache_path.exists():
        sys.exit(f"Cache not found: {cache_path}. Run hotelrunner_daily_summary.py first.")

    store = TransferStateStore()
    message = build_tomorrow_transfer_message(cache_path, target_date, store)

    out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout

    if args.dry_run or not args.send:
        out.write(message.encode("utf-8"))
        out.write(b"\n")
        out.flush()
        return

    if args.send:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required for --send")
        success = _telegram_send(token, chat_id, message)
        if success:
            print(f"[tomorrow_transfers] Sent for {target_date + dt.timedelta(days=1)}")
        else:
            print("[tomorrow_transfers] Send failed")
            sys.exit(1)


if __name__ == "__main__":
    main()
