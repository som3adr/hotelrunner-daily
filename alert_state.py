"""
alert_state.py
──────────────
Persistent alert store — tracks first detection, escalation, and resolution.

Prevents spam: an alert that fired yesterday is NOT re-sent today unless
its severity has escalated (e.g. 7 days → action_required).

State stored in alert_state.json (excluded from git, cached in GitHub Actions).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


DEFAULT_STATE_FILE = Path(__file__).parent / "alert_state.json"


@dataclass
class AlertRecord:
    alert_id: str
    category: str
    title: str
    first_detected: str       # ISO date
    last_notified: str        # ISO date
    severity: str             # check | action_required | urgent
    acknowledged: bool = False
    resolved: bool = False
    resolved_at: str | None = None

    def days_since_detected(self) -> int:
        return (dt.date.today() - dt.date.fromisoformat(self.first_detected)).days

    def was_notified_today(self) -> bool:
        return self.last_notified == dt.date.today().isoformat()

    def severity_escalated(self, new_severity: str) -> bool:
        order = {"info": 0, "check": 1, "action_required": 2, "urgent": 3}
        return order.get(new_severity, 0) > order.get(self.severity, 0)


class AlertStateStore:
    def __init__(self, path: Path = DEFAULT_STATE_FILE):
        self.path = path
        self._records: dict[str, AlertRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("alerts", []):
                rec = AlertRecord(**item)
                self._records[rec.alert_id] = rec
        except (OSError, json.JSONDecodeError, TypeError):
            self._records = {}

    def save(self) -> None:
        payload = {
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "alerts": [asdict(r) for r in self._records.values()],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def should_notify(self, alert_id: str, severity: str, title: str, category: str) -> bool:
        """
        Returns True if this alert should be sent now.
        Logic:
          - New alert → always notify
          - Same alert, same severity, already notified today → silent
          - Same alert, severity escalated → notify
          - Resolved alert that reappears → notify (new detection)
        """
        if alert_id not in self._records:
            # New — register and notify
            self._records[alert_id] = AlertRecord(
                alert_id=alert_id,
                category=category,
                title=title,
                first_detected=dt.date.today().isoformat(),
                last_notified=dt.date.today().isoformat(),
                severity=severity,
            )
            return True

        rec = self._records[alert_id]

        # Previously resolved but reappeared
        if rec.resolved:
            rec.resolved = False
            rec.resolved_at = None
            rec.first_detected = dt.date.today().isoformat()
            rec.last_notified = dt.date.today().isoformat()
            rec.severity = severity
            return True

        # Severity escalated → notify
        if rec.severity_escalated(severity):
            rec.severity = severity
            rec.last_notified = dt.date.today().isoformat()
            return True

        # Same severity, already notified today → silent
        if rec.was_notified_today():
            return False

        # Same severity, not notified today → notify (daily reminder for unresolved)
        rec.last_notified = dt.date.today().isoformat()
        return True

    def mark_resolved(self, alert_id: str) -> None:
        if alert_id in self._records:
            rec = self._records[alert_id]
            rec.resolved = True
            rec.resolved_at = dt.date.today().isoformat()

    def resolve_missing(self, active_alert_ids: set[str]) -> list[str]:
        """Mark alerts as resolved if they no longer appear in the active set."""
        newly_resolved = []
        for alert_id, rec in self._records.items():
            if not rec.resolved and alert_id not in active_alert_ids:
                rec.resolved = True
                rec.resolved_at = dt.date.today().isoformat()
                newly_resolved.append(rec.title)
        return newly_resolved

    def get_record(self, alert_id: str) -> AlertRecord | None:
        return self._records.get(alert_id)

    def active_unresolved(self) -> list[AlertRecord]:
        return [r for r in self._records.values() if not r.resolved]
