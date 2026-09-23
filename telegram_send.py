"""
telegram_send.py
────────────────
Two modes:

  python telegram_send.py --mode full
      Sends the full 2-message daily briefing (use this at 7 AM).

  python telegram_send.py --mode check
      Compares current reservations with the last saved snapshot.
      Sends a change-alert ONLY if something actually changed.
      Silent if nothing changed (no spam).

Other flags:
  --get-chat-id   Print recent chat IDs and exit.
  --date          Override date (YYYY-MM-DD). Defaults to today.
  --cache-file    Path to reservation cache. Default: reservations_cache.json
  --snapshot-file Path to change-detection snapshot. Default: hotelrunner_snapshot.json
  --dry-run       Print messages without sending.

Required in .env:
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


# ── helpers ──────────────────────────────────────────────────────────────────

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        import os
        os.environ.setdefault(key.strip(), value.strip())


def get_env(key: str) -> str:
    import os
    value = os.environ.get(key, "").strip()
    if not value:
        sys.exit(
            f"\n[telegram_send] Missing '{key}' in .env\n"
            f"Add it like:  {key}=your_value_here\n"
        )
    return value


def telegram_post(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        sys.exit(f"\n[telegram_send] Telegram API error {exc.code}: {body}\n")


def send_text(token: str, chat_id: str, text: str, label: str = "") -> bool:
    """Send message. Returns True on success, False on failure. Does NOT exit."""
    result = telegram_post(token, "sendMessage", {"chat_id": chat_id, "text": text})
    if result.get("ok"):
        print(f"Sent: {label or text[:40]}")
        return True
    else:
        print(f"Error sending '{label}': {result}")
        return False


def send_with_retry(
    token: str, chat_id: str, text: str, label: str = "",
    max_retries: int = 3, backoff_seconds: float = 5.0,
) -> bool:
    """Send with retry on failure. Returns True if any attempt succeeds."""
    import time
    for attempt in range(1, max_retries + 1):
        if send_text(token, chat_id, text, label):
            return True
        if attempt < max_retries:
            print(f"[telegram] Retry {attempt}/{max_retries - 1} in {backoff_seconds}s ...")
            time.sleep(backoff_seconds)
    return False


def log_run(
    log_path: Path, mode: str, fetch_success: bool,
    records_processed: int, alert_count: int,
    telegram_result: bool, error: str = "",
) -> None:
    """Append a run log entry to telegram_run_log.json."""
    entry = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "fetch_success": fetch_success,
        "records_processed": records_processed,
        "alert_count": alert_count,
        "telegram_result": telegram_result,
        "error": error,
    }
    entries: list = []
    if log_path.exists():
        try:
            entries = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    entries.append(entry)
    entries = entries[-100:]  # keep last 100 entries
    log_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def utf8_print(text: str) -> None:
    """Print to terminal without crashing on emoji (Windows cp1252 issue)."""
    out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
    out.write(text.encode("utf-8"))
    out.write(b"\n")
    out.flush()


# ── import core functions from the main script ────────────────────────────────

def _import_core():
    script_dir = Path(__file__).parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    try:
        from hotelrunner_daily_summary import (
            load_reservation_cache,
            active_stay_lines,
            load_room_blocks,
            build_day_summaries,
            build_whatsapp_block,
            build_manager_block,
        )
        return load_reservation_cache, active_stay_lines, load_room_blocks, build_day_summaries, build_whatsapp_block, build_manager_block
    except ImportError as exc:
        sys.exit(f"\n[telegram_send] Could not import from hotelrunner_daily_summary.py: {exc}\n")


# ── full daily briefing ───────────────────────────────────────────────────────

def build_messages_from_cache(cache_path: Path, target_date: dt.date) -> tuple[str, str]:
    """Returns (whatsapp_block, manager_block) for the given date."""
    load_reservation_cache, active_stay_lines, load_room_blocks, build_day_summaries, build_whatsapp_block, build_manager_block = _import_core()
    script_dir = Path(__file__).parent

    reservations = load_reservation_cache(cache_path)
    stay_lines = active_stay_lines(reservations)
    blocks = load_room_blocks(script_dir / "hotelrunner_blocks.json")
    summaries = build_day_summaries(stay_lines, start=target_date, days_ahead=0, blocks=blocks)

    if not summaries:
        return f"No summary for {target_date.isoformat()}", ""

    return build_whatsapp_block(summaries[0]), build_manager_block(summaries[0])


# ── change detection ──────────────────────────────────────────────────────────

def _is_relevant_today_or_tomorrow(res: dict) -> bool:
    """
    Returns True if a reservation touches today or tomorrow:
    - arriving today or tomorrow
    - currently in-house (checked in before today, still here)
    - departing today or tomorrow
    Formula: checkin_date <= tomorrow  AND  checkout_date >= today
    """
    today    = dt.date.today()
    tomorrow = today + dt.timedelta(days=1)
    try:
        checkin  = dt.date.fromisoformat(str(res.get("checkin_date") or "")[:10])
        checkout = dt.date.fromisoformat(str(res.get("checkout_date") or "")[:10])
        return checkin <= tomorrow and checkout >= today
    except ValueError:
        return False


def build_snapshot(cache_path: Path) -> dict:
    """
    Snapshot of reservations relevant to TODAY and TOMORROW only.
    Future bookings (Oct, Nov, …) are ignored — no noise, no false alerts.
    """
    load_reservation_cache, *_ = _import_core()
    reservations = load_reservation_cache(cache_path)
    snapshot: dict = {"timestamp": dt.datetime.now().isoformat(), "reservations": {}}

    for res in reservations:
        # ── Only track today / tomorrow ──────────────────────────────────────
        if not _is_relevant_today_or_tomorrow(res):
            continue

        rid = str(res.get("reservation_id") or res.get("id") or "")
        if not rid:
            continue

        rooms      = res.get("rooms") or []
        first_room = rooms[0] if rooms else {}
        room_name  = first_room.get("name_presentation") or first_room.get("name") or ""
        meal_plan  = first_room.get("meal_plan_presentation") or first_room.get("meal_plan") or ""
        adults     = str(first_room.get("total_adult") or res.get("total_guests") or "")

        snapshot["reservations"][rid] = {
            "state":     str(res.get("state") or ""),
            "guest":     str(res.get("guest") or ""),
            "room":      room_name[:60],
            "arrival":   str(res.get("checkin_date") or "")[:10],
            "departure": str(res.get("checkout_date") or "")[:10],
            "adults":    adults,
            "meal_plan": meal_plan[:40],
        }
    return snapshot



def load_snapshot(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"reservations": {}}


def save_snapshot(snapshot: dict, path: Path) -> None:
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")


def build_change_alert(old: dict, new: dict, latest_updates_path: Path | None = None) -> str | None:
    """
    Diff two snapshots and check latest HotelRunner updates for cancellations.
    Returns a Telegram message describing what changed, or None if nothing changed.
    """
    old_res = old.get("reservations", {})
    new_res = new.get("reservations", {})
    notified_cancellations = set(old.get("notified_cancellations", []))

    added_ids    = set(new_res) - set(old_res)
    removed_ids  = set(old_res) - set(new_res)
    common_ids   = set(old_res) & set(new_res)

    lines: list[str] = []

    active_guest_names = {
        str(r.get("guest") or "").strip().casefold()
        for r in new_res.values()
    }
    today_str = dt.date.today().isoformat()

    # 1. Direct cancellations from latest HotelRunner API updates
    if latest_updates_path and latest_updates_path.exists():
        try:
            latest_updates = json.loads(latest_updates_path.read_text(encoding="utf-8"))
            for u in latest_updates:
                st = str(u.get("state") or "").lower()
                if st in ("canceled", "cancelled"):
                    # Only consider reservations affecting today or tomorrow
                    if not _is_relevant_today_or_tomorrow(u):
                        continue

                    # Only alert if the cancellation event actually happened TODAY
                    canc_ts = str(u.get("canceled_at") or u.get("updated_at") or "")
                    if not canc_ts.startswith(today_str):
                        continue

                    guest = str(u.get("guest") or u.get("firstname") or "Guest").strip()

                    # Ignore if guest is still active in house (e.g. stay extension)
                    if guest.casefold() in active_guest_names:
                        continue

                    rid = str(u.get("reservation_id") or u.get("id") or "")
                    if rid and rid not in notified_cancellations:
                        rooms = u.get("rooms") or []
                        first_room = rooms[0] if rooms else {}
                        room_name = first_room.get("name_presentation") or first_room.get("name") or "Room"
                        cin = str(u.get("checkin_date") or "")[:10]
                        cout = str(u.get("checkout_date") or "")[:10]
                        lines.append(f"❌ CANCELLED: {guest} · {room_name[:60]} · {cin} → {cout}")
                        notified_cancellations.add(rid)
        except Exception:
            pass


    # 2. New reservations
    for rid in sorted(added_ids):
        r = new_res[rid]
        lines.append(
            f"➕ NEW: {r['guest']} x{r['adults']} · {r['room']} · "
            f"{r['arrival']} → {r['departure']} · {r['meal_plan']}"
        )

    # 3. Cancelled / removed from snapshot
    for rid in sorted(removed_ids):
        if rid in notified_cancellations:
            continue
        r = old_res[rid]
        if r.get("state", "").lower() in ("cancelled", "canceled", "no_show", ""):
            lines.append(f"❌ REMOVED: {r['guest']} · {r['room']} · {r['arrival']}")
        else:
            lines.append(f"🗑️ GONE: {r['guest']} · {r['room']} · {r['arrival']}")
        notified_cancellations.add(rid)

    # 4. Modified
    IMPORTANT_FIELDS = {"state", "guest", "room", "arrival", "departure", "adults", "meal_plan"}
    for rid in sorted(common_ids):
        o, n = old_res[rid], new_res[rid]
        diffs = [
            f"{k}: {o[k]} → {n[k]}"
            for k in IMPORTANT_FIELDS
            if o.get(k) != n.get(k)
        ]
        if diffs:
            lines.append(f"✏️ CHANGED: {n['guest']} · {n['room']} · " + ", ".join(diffs))

    # Keep track of notified cancellations
    new["notified_cancellations"] = list(notified_cancellations)

    if not lines:
        return None  # nothing changed → stay silent

    now_str = dt.datetime.now().strftime("%d %b · %H:%M")
    header = f"🔔 HOTELRUNNER UPDATE · {now_str}\n"
    return header + "\n".join(lines)



# ── chat ID discovery ─────────────────────────────────────────────────────────

def print_chat_ids(token: str) -> None:
    result = telegram_post(token, "getUpdates", {"limit": 20, "offset": -20})
    updates = result.get("result", [])
    if not updates:
        print(
            "\nNo recent messages found.\n"
            "Send any message to your bot first, then run this again.\n"
        )
        return
    seen: set[int] = set()
    print("\nRecent chats seen by your bot:")
    print("-" * 50)
    for update in updates:
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        if chat_id and chat_id not in seen:
            seen.add(chat_id)
            title = chat.get("title") or chat.get("first_name") or "(unknown)"
            kind = chat.get("type", "?")
            print(f"  ID: {chat_id:>20}   type: {kind:<10}  name: {title}")
    print("-" * 50)
    print("Put the right ID in .env as TELEGRAM_CHAT_ID=\n")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Send HotelRunner updates to Telegram.")
    parser.add_argument("--mode", choices=["full", "check"], default="full",
                        help="'full' = daily briefing. 'check' = change alert only if something changed.")
    parser.add_argument("--get-chat-id", action="store_true", help="Print recent chat IDs and exit.")
    parser.add_argument("--date", help="Override date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--cache-file", default="reservations_cache.json")
    parser.add_argument("--snapshot-file", default="hotelrunner_snapshot.json",
                        help="File used to track last-seen state for change detection.")
    parser.add_argument("--dry-run", action="store_true", help="Print without sending.")
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    token = get_env("TELEGRAM_BOT_TOKEN")

    if args.get_chat_id:
        print_chat_ids(token)
        return

    chat_id = get_env("TELEGRAM_CHAT_ID")

    target_date = dt.date.today()
    if args.date:
        try:
            target_date = dt.date.fromisoformat(args.date)
        except ValueError:
            sys.exit("Invalid --date. Use YYYY-MM-DD.")

    cache_path = Path(args.cache_file)
    if not cache_path.exists():
        sys.exit(
            f"\n[telegram_send] Cache not found: {cache_path.resolve()}\n"
            f"Run hotelrunner_daily_summary.py first.\n"
        )

    # ── MODE: full daily briefing ─────────────────────────────────────────────
    if args.mode == "full":
        msg1, msg2 = build_messages_from_cache(cache_path, target_date)

        if args.dry_run:
            utf8_print("\n-- MESSAGE 1 (copy to WhatsApp) --\n")
            utf8_print(msg1)
            utf8_print("\n-- MESSAGE 2 (manager view) --\n")
            utf8_print(msg2)
            return

        print(f"[full] Sending daily briefing for {target_date} ...")
        send_text(token, chat_id, msg1, "WhatsApp block")
        if msg2:
            send_text(token, chat_id, msg2, "Manager block")

    # ── MODE: hourly change check ─────────────────────────────────────────────
    elif args.mode == "check":
        # ── Quiet hours: 00:01 – 06:59 Morocco time (UTC+1) ──────────────────
        # No notifications while sleeping, even if triggered manually.
        morocco_hour = (dt.datetime.now(dt.timezone.utc).hour + 1) % 24
        if 1 <= morocco_hour <= 6:
            print(f"[check] Quiet hours ({morocco_hour:02d}:xx Morocco time). No notifications until 07:00.")
            return

        snapshot_path = Path(args.snapshot_file)
        old_snapshot = load_snapshot(snapshot_path)
        new_snapshot = build_snapshot(cache_path)

        latest_updates_path = cache_path.parent / "hotelrunner_latest_updates.json"
        alert = build_change_alert(old_snapshot, new_snapshot, latest_updates_path)

        if alert is None:
            # Still save snapshot so quiet state is kept
            save_snapshot(new_snapshot, snapshot_path)
            run_log_path = cache_path.parent / "telegram_run_log.json"
            log_run(run_log_path, "check", True, len(new_snapshot.get("reservations", {})), 0, True)
            print("[check] No changes detected. Silent.")
            return

        if args.dry_run:
            utf8_print("\n-- CHANGE ALERT (would be sent) --\n")
            utf8_print(alert)
            return

        # ── CRITICAL: send FIRST, persist snapshot ONLY on success ──────────
        run_log_path = cache_path.parent / "telegram_run_log.json"
        print("[check] Changes detected — sending alert ...")
        sent_ok = send_with_retry(token, chat_id, alert, "Change alert")

        if sent_ok:
            # Only advance snapshot after confirmed delivery
            save_snapshot(new_snapshot, snapshot_path)
            log_run(run_log_path, "check", True,
                    len(new_snapshot.get("reservations", {})), 1, True)
        else:
            log_run(run_log_path, "check", True,
                    len(new_snapshot.get("reservations", {})), 1, False,
                    error="Telegram send failed — snapshot NOT advanced, will retry")
            print("[check] Telegram send failed — snapshot NOT advanced. Will retry next run.")


if __name__ == "__main__":
    main()
