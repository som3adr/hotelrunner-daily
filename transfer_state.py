"""
transfer_state.py
─────────────────
Persistent store for transfer 'Sent' status.
Mirrors alert_state.py pattern.

Transfer records are keyed by: f"{reservation_id}:{direction}:{date}"

TODO: future integration — infer 'sent' from WhatsApp/Telegram delivery confirmation.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, asdict
from pathlib import Path


DEFAULT_STATE_FILE = Path(__file__).parent / "transfer_state.json"


@dataclass
class TransferSentRecord:
    key: str            # "{reservation_id}:{direction}:{date}"
    marked_sent_at: str # ISO datetime
    marked_by: str = "manual"  # manual | system


class TransferStateStore:
    def __init__(self, path: Path = DEFAULT_STATE_FILE):
        self.path = path
        self._records: dict[str, TransferSentRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("sent", []):
                rec = TransferSentRecord(**item)
                self._records[rec.key] = rec
        except (OSError, json.JSONDecodeError, TypeError):
            self._records = {}

    def save(self) -> None:
        payload = {
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "sent": [asdict(r) for r in self._records.values()],
        }
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _make_key(self, reservation_id: str, direction: str, date: dt.date) -> str:
        return f"{reservation_id}:{direction}:{date.isoformat()}"

    def mark_sent(self, reservation_id: str, direction: str, date: dt.date) -> None:
        key = self._make_key(reservation_id, direction, date)
        self._records[key] = TransferSentRecord(
            key=key,
            marked_sent_at=dt.datetime.now().isoformat(timespec="seconds"),
        )
        self.save()

    def is_sent(self, reservation_id: str, direction: str, date: dt.date) -> bool:
        key = self._make_key(reservation_id, direction, date)
        return key in self._records

    def get_sent_keys(self) -> set[str]:
        return set(self._records.keys())
