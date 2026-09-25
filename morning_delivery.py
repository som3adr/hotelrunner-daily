"""Prevent backup schedules from sending a duplicate morning report."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo


MOROCCO_TZ = ZoneInfo("Africa/Casablanca")


def local_day() -> str:
    return dt.datetime.now(MOROCCO_TZ).date().isoformat()


def was_sent(path: Path, day: str | None = None) -> bool:
    target = day or local_day()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return state.get("sent_date") == target


def mark_sent(path: Path, day: str | None = None) -> None:
    target = day or local_day()
    path.write_text(json.dumps({"sent_date": target}, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "mark"))
    parser.add_argument("--state-file", default="morning_delivery_state.json")
    args = parser.parse_args()
    path = Path(args.state_file)
    if args.action == "check":
        print("sent=true" if was_sent(path) else "sent=false")
    else:
        mark_sent(path)
        print(f"Morning delivery marked for {local_day()}")


if __name__ == "__main__":
    main()
