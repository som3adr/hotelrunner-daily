"""
telegram_bot.py
───────────────
Reactive Telegram Q&A bot powered by Gemini.

Polls Telegram every 5 minutes (via GitHub Actions cron) for new messages.
For each new message from the manager, builds a context briefing from live
reservation data and asks Gemini to answer.

Usage:
  python telegram_bot.py --poll             # process new messages
  python telegram_bot.py --poll --dry-run   # print responses without sending

Required env vars:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  GEMINI_API_KEY
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

STATE_FILE = Path("telegram_bot_state.json")
CACHE_FILE = Path("reservations_cache.json")
GEMINI_MODEL = "gemini-1.5-flash"

QA_SYSTEM_PROMPT = """You are the operations assistant for Olas surf camp in Imsouane, Morocco.

Three properties: Olas (main), Tide, Sunrise.
The manager is asking you a question. Answer using ONLY the operational data provided below.
Be brief and practical. Use plain language. No bullet lists unless the question lists multiple items.
If you cannot find the information in the data, say so clearly — do not invent.

Current Morocco time: {morocco_time}

--- OPERATIONAL DATA ---
{context}
--- END DATA ---
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


# ── State (track processed message IDs) ──────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_update_id": 0}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── Telegram API helpers ──────────────────────────────────────────────────────

def _telegram_get_updates(token: str, offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&limit=20&timeout=0"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get("result", [])
    except Exception as exc:
        print(f"[telegram_bot] getUpdates error: {exc}")
        return []


def _telegram_reply(token: str, chat_id: int, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception as exc:
        print(f"[telegram_bot] reply error: {exc}")
        return False


# ── Context builder ───────────────────────────────────────────────────────────

def _build_context(cache_path: Path, today: dt.date) -> str:
    """Build a rich context string for Gemini to answer questions."""
    from hotelrunner_daily_summary import load_reservation_cache, active_stay_lines
    from data_model import stayline_to_normalized
    from extras_engine import classify_extras_from_reservation
    from transfer_engine import build_transfer_records
    from transfer_state import TransferStateStore
    from meal_engine import compute_meal_entitlements, summarize_dinner
    from conflict_engine import detect_conflicts

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

    try:
        from google_sheets import load_sunrise_reservations
        norm += load_sunrise_reservations()
    except Exception:
        pass

    tomorrow = today + dt.timedelta(days=1)
    store = TransferStateStore()

    in_house = [r for r in norm if r.is_active_on(today)]
    arriving_today = [r for r in norm if r.is_arriving_on(today)]
    departing_today = [r for r in norm if r.is_departing_on(today)]
    arriving_tomorrow = [r for r in norm if r.is_arriving_on(tomorrow)]
    departing_tomorrow = [r for r in norm if r.is_departing_on(tomorrow)]

    # Meals
    entitlements = compute_meal_entitlements(in_house, today)
    dinner_summary = summarize_dinner(entitlements)
    breakfast_total = sum(e.count for e in entitlements if e.meal == "breakfast")
    lunch_total = sum(e.count for e in entitlements if e.meal == "lunch")
    dinner_total = dinner_summary["total"]

    # Transfers
    transfers_today = build_transfer_records(norm, today, store)
    transfers_tomorrow = build_transfer_records(norm, tomorrow, store)

    # Conflicts
    conflicts, _ = detect_conflicts(norm, today)

    out: list[str] = []
    out.append(f"Today: {today.strftime('%A %d %B %Y')}")
    out.append(f"Tomorrow: {tomorrow.strftime('%A %d %B %Y')}")
    out.append("")

    out.append("IN-HOUSE TODAY:")
    if in_house:
        for r in in_house:
            out.append(f"  {r.guest_name} | {r.house} | {r.room or r.bed} | {r.meal_plan} | arrives {r.arrival_date} departs {r.departure_date}")
    else:
        out.append("  None")

    out.append("\nARRIVING TODAY:")
    out.append("\n".join(f"  {r.guest_name} ({r.house})" for r in arriving_today) or "  None")

    out.append("\nDEPARTING TODAY:")
    out.append("\n".join(f"  {r.guest_name} ({r.house})" for r in departing_today) or "  None")

    out.append("\nARRIVING TOMORROW:")
    out.append("\n".join(f"  {r.guest_name} ({r.house})" for r in arriving_tomorrow) or "  None")

    out.append("\nDEPARTING TOMORROW:")
    out.append("\n".join(f"  {r.guest_name} ({r.house})" for r in departing_tomorrow) or "  None")

    out.append(f"\nMEALS TODAY: Breakfast {breakfast_total} | Lunch {lunch_total} | Dinner {dinner_total}")
    by_house = dinner_summary["by_house"]
    if by_house:
        out.append("Dinner by house: " + " | ".join(f"{h}: {n}" for h, n in sorted(by_house.items()) if n > 0))

    out.append(f"\nTRANSFERS TODAY ({len(transfers_today)}):")
    for tr in transfers_today:
        out.append(f"  {tr.direction.upper()} {tr.guest_name} ({tr.house}) — {tr.status} — flight: {tr.flight_number or 'n/a'} | time: {tr.pickup_time or 'n/a'}")

    out.append(f"\nTRANSFERS TOMORROW (organize today) ({len(transfers_tomorrow)}):")
    for tr in transfers_tomorrow:
        out.append(f"  {tr.direction.upper()} {tr.guest_name} ({tr.house}) — {tr.status} — flight: {tr.flight_number or 'n/a'} | time: {tr.pickup_time or 'n/a'}")

    out.append(f"\nROOM CONFLICTS: {len(conflicts)}")
    for c in conflicts:
        out.append(f"  {c.room}: {c.description}")

    out.append("\nSPECIAL NOTES (diet / allergies / medical):")
    flagged = 0
    for r in norm:
        if not r.is_active_on(today) and not r.is_arriving_on(today) and not r.is_arriving_on(tomorrow):
            continue
        if r.diet or r.allergies or r.medical_notes:
            parts = []
            if r.diet:
                parts.append(f"diet: {r.diet}")
            if r.allergies:
                parts.append(f"allergies: {r.allergies}")
            if r.medical_notes:
                parts.append(r.medical_notes[:100])
            out.append(f"  {r.guest_name}: {' | '.join(parts)}")
            flagged += 1
        for note in r.notes:
            n_lower = note.casefold()
            if any(kw in n_lower for kw in ["allerg", "vegan", "vegetar", "gluten", "nut", "halal", "kosher", "intoleran", "special"]):
                out.append(f"  {r.guest_name} (note): {note[:120]}")
                flagged += 1
    if flagged == 0:
        out.append("  None flagged")

    return "\n".join(out)


# ── Gemini Q&A ────────────────────────────────────────────────────────────────

def _ask_gemini(api_key: str, question: str, context: str, morocco_time: str) -> str:
    system = QA_SYSTEM_PROMPT.format(morocco_time=morocco_time, context=context)
    full_prompt = f"{system}\n\nManager's question: {question}"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                return " ".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:
        return f"Sorry, I couldn't reach Gemini: {exc}"
    return "Sorry, no response from Gemini."


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Reactive Telegram Q&A bot")
    parser.add_argument("--poll", action="store_true", help="Poll Telegram for new messages")
    parser.add_argument("--dry-run", action="store_true", help="Print responses without sending")
    parser.add_argument("--cache-file", default="reservations_cache.json")
    args = parser.parse_args()

    if not args.poll:
        parser.print_help()
        return

    _load_env(Path(".env"))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    our_chat_id_str = os.environ.get("TELEGRAM_CHAT_ID", "")
    api_key = os.environ.get("GEMINI_API_KEY", "")

    if not token or not our_chat_id_str or not api_key:
        sys.exit("[telegram_bot] Missing TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, or GEMINI_API_KEY")

    # Only respond to messages from the configured chat (security)
    try:
        our_chat_id = int(our_chat_id_str)
    except ValueError:
        our_chat_id = 0

    state = _load_state()
    offset = state.get("last_update_id", 0) + 1

    updates = _telegram_get_updates(token, offset)
    if not updates:
        print("[telegram_bot] No new messages.")
        return

    morocco_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    morocco_time_str = morocco_now.strftime("%H:%M Morocco time")

    cache_path = Path(args.cache_file)
    context = None  # lazy-load once

    for update in updates:
        update_id = update.get("update_id", 0)
        state["last_update_id"] = max(state.get("last_update_id", 0), update_id)

        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue

        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()

        if not text or not chat_id:
            continue

        # Security: only respond to our own chat
        if our_chat_id and chat_id != our_chat_id:
            print(f"[telegram_bot] Ignoring message from unknown chat {chat_id}")
            continue

        # Skip bot commands (start, help)
        if text.startswith("/"):
            continue

        print(f"[telegram_bot] Question: {text!r}")

        # Lazy-load context
        if context is None:
            if not cache_path.exists():
                context = "(No reservation cache found — cannot answer operational questions)"
            else:
                try:
                    context = _build_context(cache_path, dt.date.today())
                except Exception as exc:
                    context = f"(Error loading reservation data: {exc})"

        answer = _ask_gemini(api_key, text, context, morocco_time_str)
        print(f"[telegram_bot] Answer: {answer[:80]}...")

        if args.dry_run:
            print(f"[telegram_bot] DRY RUN — would reply: {answer}")
        else:
            ok = _telegram_reply(token, chat_id, f"🤖 {answer}")
            if ok:
                print("[telegram_bot] Replied.")
            else:
                print("[telegram_bot] Reply failed.")

    _save_state(state)


if __name__ == "__main__":
    main()
