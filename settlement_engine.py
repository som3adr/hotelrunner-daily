"""Checkout settlement reminders shared by reports, dashboard, and Q&A."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from data_model import NormalizedReservation, load_config


@dataclass
class SettlementReminder:
    reservation_id: str
    guest_name: str
    house: str
    departure_date: dt.date
    timing: str
    total_amount: float
    paid_amount: float
    remaining_amount: float
    payment_status: str
    action: str
    expected_deposit_percent: int | None = None
    extra_checks: list[str] = field(default_factory=list)


def _eligible_channel(channel: str) -> bool:
    value = channel.casefold()
    return any(key in value for key in ("online", "hostelworld", "surf camp", "surfcamp"))


def _extra_checks(res: NormalizedReservation) -> list[str]:
    found: set[str] = set()
    labels = " ".join(extra.raw_label for extra in res.extras).casefold()
    categories = {extra.category for extra in res.extras}
    if "meal" in categories or any(word in labels for word in ("dinner", "lunch", "board formula")):
        found.add("meals")
    if "surf_lesson" in categories or "surf class" in labels or "surf lesson" in labels:
        found.add("surf lessons")
    if "surf_equipment" in categories or "board rental" in labels:
        found.add("board rental")
    if "transfer" in categories:
        found.add("transfer")
    if "timlalin" in labels or "timlaline" in labels:
        found.add("Timlalin")
    order = ["meals", "surf lessons", "Timlalin", "board rental", "transfer"]
    return [item for item in order if item in found]


def build_settlement_reminders(
    reservations: list[NormalizedReservation],
    today: dt.date,
) -> list[SettlementReminder]:
    reminders: list[SettlementReminder] = []
    tomorrow = today + dt.timedelta(days=1)
    seen_reservations: set[str] = set()
    for res in reservations:
        if res.departure_date not in {today, tomorrow} or not _eligible_channel(res.channel):
            continue
        identity = res.reservation_id or f"{res.guest_name}:{res.departure_date}"
        if identity in seen_reservations:
            continue
        seen_reservations.add(identity)
        remaining = max(0.0, res.total_amount - res.paid_amount)
        expected_percent = None
        if "surf camp" in res.channel.casefold() or "surfcamp" in res.channel.casefold():
            if res.booking_date:
                cfg = load_config().get("payments", {})
                change_date = dt.date.fromisoformat(
                    cfg.get("surf_camp_policy_change_date", "2026-09-01")
                )
                expected_percent = (
                    int(cfg.get("surf_camp_deposit_after_percent", 20))
                    if res.booking_date >= change_date
                    else int(cfg.get("surf_camp_deposit_before_percent", 50))
                )

        if res.total_amount <= 0:
            status = "confirm_total"
            action = "Confirm the reservation total and any PayPal payment before checkout."
        elif remaining <= 0.5:
            status = "paid"
            action = "Accommodation is recorded as paid; verify final extras."
        elif res.payment_record_count == 0 and res.paid_amount <= 0:
            status = "confirm_external"
            action = "Collect the full amount or confirm whether it was paid through PayPal."
        else:
            status = "partial"
            action = f"Collect the recorded remaining balance of €{remaining:.2f}, then verify extras."

        reminders.append(SettlementReminder(
            reservation_id=res.reservation_id,
            guest_name=res.guest_name,
            house=res.house,
            departure_date=res.departure_date,
            timing="today" if res.departure_date == today else "tomorrow",
            total_amount=res.total_amount,
            paid_amount=res.paid_amount,
            remaining_amount=remaining,
            payment_status=status,
            action=action,
            expected_deposit_percent=expected_percent,
            extra_checks=_extra_checks(res),
        ))
    return reminders


def format_settlement_section(reminders: list[SettlementReminder]) -> str:
    if not reminders:
        return ""
    lines = ["💳 CHECKOUT PAYMENTS"]
    for item in reminders:
        when = "COLLECT TODAY" if item.timing == "today" else "PREPARE FOR TOMORROW"
        lines.append(f"\n{when} — {item.guest_name} ({item.house})")
        if item.total_amount > 0:
            lines.append(
                f"Total €{item.total_amount:.2f} · recorded €{item.paid_amount:.2f} · remaining €{item.remaining_amount:.2f}"
            )
        if item.expected_deposit_percent is not None:
            lines.append(f"Surf Camp policy: expected {item.expected_deposit_percent}% deposit")
        lines.append(item.action)
        if item.extra_checks:
            lines.append("Check extras: " + ", ".join(item.extra_checks))
        lines.append("Check group messages for anything not recorded.")
    return "\n".join(lines)
