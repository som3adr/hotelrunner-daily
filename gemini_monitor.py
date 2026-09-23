"""
gemini_monitor.py
─────────────────
Proactive AI operations monitor for Olas surf camp.

Runs on schedule (hourly via GitHub Actions).
Reads the current operational state, sends a briefing to Gemini,
and forwards any flagged issues to Telegram.
Silent when Gemini finds nothing to flag (NOTHING response).

Usage:
  python gemini_monitor.py --dry-run    # print without sending
  python gemini_monitor.py --send       # send to Telegram

Required env vars for --send:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  GEMINI_API_KEY
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-1.5-flash"
STATE_FILE = Path("gemini_monitor_state.json")
CACHE_FILE = Path("reservations_cache.json")

SYSTEM_PROMPT = """You are the operations assistant for Olas surf camp in Imsouane, Morocco.

Three properties managed together: Olas (main), Tide, Sunrise.
Transfers are organized ONE DAY BEFORE departure or arrival.
All Inclusive meal plan includes a transfer — it must be prepared today for tomorrow.

Review the operational briefing below and flag ONLY items needing immediate manager attention.

Rules:
- Be very concise — 1-2 sentences per alert maximum
- Only flag GENUINE issues that require action TODAY
- Do not flag things already resolved or already sent
- Do not invent rules not mentioned in the briefing
- If nothing needs attention, respond with exactly one word: NOTHING

Current Morocco time: {morocco_time}
"""


# ── Env loading ───────────────────────────────────────────────────────────────

def _load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


# ── State: avoid re-alerting same issue same day ──────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": "", "alerted_hashes": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _already_alerted(text: str, state: dict) -> bool:
    today = dt.date.today().isoformat()
    if state.get("date") != today:
        # New day — reset
        state["date"] = today
        state["alerted_hashes"] = []
        return False
    h = hashlib.md5(text.strip().lower().encode()).hexdigest()
    if h in state.get("alerted_hashes", []):
        return True
    state["alerted_hashes"].append(h)
    return False


# ── Operational briefing builder ──────────────────────────────────────────────

def _build_briefing(cache_path: Path, today: dt.date) -> str:
    """Build a compact operational briefing for Gemini to analyze."""
    from hotelrunner_daily_summary import load_reservation_cache, active_stay_lines
    from data_model import stayline_to_normalized
    from extras_engine import classify_extras_from_reservation
    from transfer_engine import build_transfer_records
    from transfer_state import TransferStateStore
    from meal_engine import compute_meal_entitlements, summarize_dinner

    raw = load_reservation_cache(cache_path)
    raw_by_id = {str(r.get("reservation_id") or ""): r for r in raw}
    lines = active_stay_lines(raw)

    norm = []
    for line in lines:
        raw_r = raw_by_id.get(line.reservation_id)
        res = stayline_to_normalized(line, raw_r)
        if raw_r:
            res.extras = classify_extras_from_reservation(raw_r)
        norm.append(res)

    # Try loading Sunrise sheet guests too
    try:
        from google_sheets import load_sunrise_reservations
        norm += load_sunrise_reservations()
    except Exception:
        pass

    tomorrow = today + dt.timedelta(days=1)
    store = TransferStateStore()

    # Today's state
    in_house_today = [r for r in norm if r.is_active_on(today)]
    arriving_today = [r for r in norm if r.is_arriving_on(today)]
    departing_today = [r for r in norm if r.is_departing_on(today)]

    # Tomorrow's state
    arriving_tomorrow = [r for r in norm if r.is_arriving_on(tomorrow)]
    departing_tomorrow = [r for r in norm if r.is_departing_on(tomorrow)]

    # Transfers
    transfers_today = build_transfer_records(norm, today, store)
    transfers_tomorrow = build_transfer_records(norm, tomorrow, store)

    # Meals
    entitlements = compute_meal_entitlements(in_house_today, today)
    dinner_summary = summarize_dinner(entitlements)

    lines_out: list[str] = []
    lines_out.append(f"DATE: {today.strftime('%A %d %B %Y')}")
    lines_out.append(f"TOMORROW: {tomorrow.strftime('%A %d %B %Y')}")
    lines_out.append("")

    lines_out.append(f"IN-HOUSE TODAY: {len(in_house_today)} guests")
    lines_out.append(f"ARRIVING TODAY: {len(arriving_today)}")
    lines_out.append(f"DEPARTING TODAY: {len(departing_today)}")
    lines_out.append(f"ARRIVING TOMORROW: {len(arriving_tomorrow)}")
    lines_out.append(f"DEPARTING TOMORROW: {len(departing_tomorrow)}")
    lines_out.append("")

    # Dinner
    dinner_total = dinner_summary["total"]
    by_house = dinner_summary["by_house"]
    house_str = " | ".join(f"{h}: {n}" for h, n in sorted(by_house.items()) if n > 0)
    lines_out.append(f"DINNERS TONIGHT: {dinner_total} ({house_str})")
    if dinner_total > 24:
        lines_out.append("⚠️ CAPACITY WARNING: exceeds 24")
    elif dinner_total > 13:
        lines_out.append("⚠️ Extra tables required tonight")
    lines_out.append("")

    # Transfers today
    lines_out.append(f"TRANSFERS TODAY: {len(transfers_today)}")
    for tr in transfers_today:
        lines_out.append(f"  - {tr.direction.upper()} {tr.guest_name} ({tr.house}) — status: {tr.status}")

    lines_out.append("")
    lines_out.append(f"TRANSFERS TOMORROW (to organize today): {len(transfers_tomorrow)}")
    for tr in transfers_tomorrow:
        missing = []
        if tr.is_arrival and not tr.flight_number:
            missing.append("flight number missing")
        if tr.is_arrival and not tr.airport:
            missing.append("airport missing")
        if tr.is_departure and not tr.pickup_time:
            missing.append("pickup time missing")
        if tr.is_departure and not tr.destination:
            missing.append("destination missing")
        status_str = f"status: {tr.status}"
        if missing:
            status_str += f" — {', '.join(missing)}"
        lines_out.append(f"  - {tr.direction.upper()} {tr.guest_name} ({tr.house}) — {status_str}")

    # Special notes
    lines_out.append("")
    lines_out.append("SPECIAL NOTES / DIETARY:")
    flagged = 0
    for r in in_house_today + arriving_tomorrow:
        if r.diet or r.allergies or r.medical_notes:
            note_parts = []
            if r.diet:
                note_parts.append(f"diet: {r.diet}")
            if r.allergies:
                note_parts.append(f"allergies: {r.allergies}")
            if r.medical_notes:
                note_parts.append(r.medical_notes[:80])
            lines_out.append(f"  - {r.guest_name}: {' | '.join(note_parts)}")
            flagged += 1
        for note in r.notes:
            n = note.casefold()
            if any(kw in n for kw in ["allerg", "vegan", "vegetar", "gluten", "nut", "halal", "kosher", "intoleran"]):
                lines_out.append(f"  - {r.guest_name} (note): {note[:100]}")
                flagged += 1
    if flagged == 0:
        lines_out.append("  None")

    return "\n".join(lines_out)


# ── Gemini API call ───────────────────────────────────────────────────────────

def _call_gemini(api_key: str, briefing: str, morocco_time: str) -> str:
    """Call Gemini API and return the response text."""
    system = SYSTEM_PROMPT.format(morocco_time=morocco_time)
    prompt = f"{system}\n\n--- OPERATIONAL BRIEFING ---\n{briefing}\n--- END BRIEFING ---"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512},
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                return " ".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:
        return f"[gemini_monitor] Gemini API error: {exc}"
    return "NOTHING"


# ── Telegram send ─────────────────────────────────────────────────────────────

def _telegram_send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception as exc:
        print(f"[gemini_monitor] Telegram error: {exc}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini proactive operations monitor")
    parser.add_argument("--dry-run", action="store_true", help="Print without sending")
    parser.add_argument("--send", action="store_true", help="Send to Telegram")
    parser.add_argument("--cache-file", default="reservations_cache.json")
    parser.add_argument("--date", help="Override today YYYY-MM-DD")
    args = parser.parse_args()

    _load_env(Path(".env"))

    today = dt.date.today()
    if args.date:
        try:
            today = dt.date.fromisoformat(args.date)
        except ValueError:
            sys.exit("Invalid --date. Use YYYY-MM-DD.")

    morocco_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    morocco_time_str = morocco_now.strftime("%H:%M Morocco time")

    # Quiet hours: 00:01–06:59 Morocco
    if not args.dry_run and 1 <= morocco_now.hour <= 6:
        print(f"[gemini_monitor] Quiet hours ({morocco_now.hour:02d}:xx Morocco). Skipping.")
        return

    cache_path = Path(args.cache_file)
    if not cache_path.exists():
        sys.exit(f"[gemini_monitor] Cache not found: {cache_path}")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        sys.exit("[gemini_monitor] GEMINI_API_KEY not set")

    print(f"[gemini_monitor] Building briefing for {today} ...")
    briefing = _build_briefing(cache_path, today)

    if args.dry_run:
        print("\n--- BRIEFING ---")
        print(briefing)
        print("\n--- (would send to Gemini) ---")
        return

    print("[gemini_monitor] Calling Gemini ...")
    response = _call_gemini(api_key, briefing, morocco_time_str)

    if response.strip().upper() == "NOTHING" or not response.strip():
        print("[gemini_monitor] Gemini: nothing to flag. Silent.")
        return

    # Check if already alerted today
    state = _load_state()
    if _already_alerted(response, state):
        print("[gemini_monitor] Already alerted this issue today. Skipping.")
        _save_state(state)
        return

    print(f"[gemini_monitor] Gemini flagged:\n{response}")

    if args.send:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            sys.exit("[gemini_monitor] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required")

        header = f"🤖 OPS ASSISTANT · {morocco_now.strftime('%d %b · %H:%M')}\n"
        ok = _telegram_send(token, chat_id, header + response)
        if ok:
            print("[gemini_monitor] Sent to Telegram.")
        else:
            print("[gemini_monitor] Telegram send failed.")

    _save_state(state)


if __name__ == "__main__":
    main()
