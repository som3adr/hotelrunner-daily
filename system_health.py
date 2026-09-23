"""
system_health.py
────────────────
Tracks last-run/last-success/error for all automation jobs.
Used by the dashboard System Health section.

Data persisted in system_health.json.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


DEFAULT_HEALTH_FILE = Path(__file__).parent / "system_health.json"
STALE_THRESHOLD_HOURS = 4  # data older than 4h is considered stale


class SystemHealthStore:
    def __init__(self, path: Path = DEFAULT_HEALTH_FILE):
        self.path = path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        self._data["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def record_fetch(self, source: str, success: bool, error: str = "") -> None:
        """Record a data fetch event. source: 'hotelrunner' | 'sunrise'"""
        key = f"last_{source}_fetch"
        self._data[key] = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "success": success,
            "error": error,
        }
        self._save()

    def record_checker_run(self, success: bool, alert_count: int = 0, error: str = "") -> None:
        """Record a change-checker run."""
        self._data["last_checker_run"] = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "success": success,
            "alert_count": alert_count,
            "error": error,
        }
        self._save()

    def record_telegram_send(self, success: bool, mode: str = "", error: str = "") -> None:
        """Record a Telegram send attempt."""
        now = dt.datetime.now().isoformat(timespec="seconds")
        self._data["last_telegram_attempt"] = {
            "timestamp": now,
            "success": success,
            "mode": mode,
            "error": error,
        }
        if success:
            self._data["last_telegram_success"] = {"timestamp": now, "mode": mode}
        self._save()

    def get_health_status(self) -> dict[str, Any]:
        """Return health summary with stale-data warnings."""
        now = dt.datetime.now()
        warnings: list[str] = []

        def _age_hours(key: str) -> float | None:
            entry = self._data.get(key, {})
            ts_str = entry.get("timestamp") if isinstance(entry, dict) else None
            if not ts_str:
                return None
            try:
                ts = dt.datetime.fromisoformat(ts_str)
                return (now - ts).total_seconds() / 3600
            except ValueError:
                return None

        for source_key, label in [
            ("last_hotelrunner_fetch", "HotelRunner"),
            ("last_sunrise_fetch", "Sunrise/Sheet"),
        ]:
            age = _age_hours(source_key)
            if age is None:
                warnings.append(f"{label} data: never fetched")
            elif age > STALE_THRESHOLD_HOURS:
                warnings.append(f"{label} data: stale ({age:.1f}h ago)")

        return {
            "last_hotelrunner_fetch": self._data.get("last_hotelrunner_fetch"),
            "last_sunrise_fetch": self._data.get("last_sunrise_fetch"),
            "last_checker_run": self._data.get("last_checker_run"),
            "last_telegram_attempt": self._data.get("last_telegram_attempt"),
            "last_telegram_success": self._data.get("last_telegram_success"),
            "stale_warnings": warnings,
        }
