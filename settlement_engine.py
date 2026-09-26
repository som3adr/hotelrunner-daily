"""Checkout settlement reminders shared by reports, dashboard, and Q&A."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from data_model import NormalizedReservation, load_config


@dataclass
class SettlementComponent:
    reservation_id: str
    channel: str
    arrival_date: dt.date | None
    departure_date: dt.date
    total_amount: float
    paid_amount: float
    remaining_amount: float
    currency: str = "EUR"
    payment_note: str = ""


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
    components: list[SettlementComponent] = field(default_factory=list)
    currency_balances: dict[str, tuple[float, float, float]] = field(default_factory=dict)


_HOSTELWORLD_BALANCE_RE = re.compile(
    r"\bpaid\s*:\s*(\d+(?:\.\d+)?)\s*-\s*due\s*:\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _component_amounts(res: NormalizedReservation) -> tuple[float, float, float, str]:
    total = res.total_amount
    paid = res.paid_amount
    if "booking.com" in res.channel.casefold():
        return total, total, 0.0, "Paid through Booking.com"
    if "hostelworld" in res.channel.casefold():
        note_text = "\n".join(res.notes)
        match = _HOSTELWORLD_BALANCE_RE.search(note_text)
        if match:
            paid = float(match.group(1))
            due = float(match.group(2))
            return max(total, paid + due), paid, due, "HostelWorld deposit recorded"
    return total, paid, max(0.0, total - paid), ""


def _money(amount: float, currency: str) -> str:
    return f"€{amount:.2f}" if currency == "EUR" else f"{amount:.2f} {currency}"


def _eligible_channel(channel: str) -> bool:
    value = channel.casefold()
    return any(key in value for key in ("online", "hostelworld", "surf camp", "surfcamp"))


def _guest_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _linked_stay_chain(
    target: NormalizedReservation,
    reservations: list[NormalizedReservation],
) -> list[NormalizedReservation]:
    """Return prior consecutive records belonging to the same guest stay."""
    chain = [target]
    chain_start = target.arrival_date
    candidates = [
        res for res in reservations
        if res.reservation_id != target.reservation_id
        and _guest_key(res.guest_name) == _guest_key(target.guest_name)
        and res.departure_date
        and res.arrival_date
        and res.status.casefold() not in {"cancelled", "canceled"}
    ]
    while chain_start:
        previous = [
            res for res in candidates
            if res.departure_date == chain_start and res not in chain
        ]
        if len(previous) != 1:
            break
        prior = previous[0]
        chain.insert(0, prior)
        chain_start = prior.arrival_date
    return chain


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
        chain = _linked_stay_chain(res, reservations)
        for linked in chain:
            seen_reservations.add(linked.reservation_id or f"{linked.guest_name}:{linked.departure_date}")

        components = []
        for linked in chain:
            total, paid, remaining, payment_note = _component_amounts(linked)
            components.append(SettlementComponent(
                reservation_id=linked.reservation_id,
                channel=linked.channel or "Unknown",
                arrival_date=linked.arrival_date,
                departure_date=linked.departure_date,
                total_amount=total,
                paid_amount=paid,
                remaining_amount=remaining,
                currency=linked.currency or "EUR",
                payment_note=payment_note,
            ))
        currency_balances: dict[str, tuple[float, float, float]] = {}
        for component in components:
            current = currency_balances.get(component.currency, (0.0, 0.0, 0.0))
            currency_balances[component.currency] = tuple(
                current[index] + (component.total_amount, component.paid_amount, component.remaining_amount)[index]
                for index in range(3)
            )
        total_amount = sum(item.total_amount for item in components)
        paid_amount = sum(item.paid_amount for item in components)
        remaining = sum(item.remaining_amount for item in components)
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

        if len(currency_balances) > 1 and remaining > 0.5:
            status = "partial" if paid_amount > 0 else "confirm_external"
            action = "Collect each balance shown, or confirm external payment for each linked reservation."
        elif total_amount <= 0:
            status = "confirm_total"
            action = "Confirm the reservation total and any PayPal payment before checkout."
        elif remaining <= 0.5:
            status = "paid"
            action = "Accommodation is recorded as paid; verify final extras."
        elif paid_amount <= 0:
            status = "confirm_external"
            if len(components) > 1:
                action = (
                    f"Collect the combined balance of {_money(remaining, components[0].currency)}, or confirm whether any part was paid through PayPal. "
                    "Verify every linked reservation before closing the stay."
                )
            else:
                action = "Collect the full amount or confirm whether it was paid through PayPal."
        else:
            status = "partial"
            action = (
                f"Collect the recorded remaining balance of {_money(remaining, components[0].currency)}, "
                "then verify extras."
            )

        reminders.append(SettlementReminder(
            reservation_id=res.reservation_id,
            guest_name=res.guest_name,
            house=res.house,
            departure_date=res.departure_date,
            timing="today" if res.departure_date == today else "tomorrow",
            total_amount=total_amount,
            paid_amount=paid_amount,
            remaining_amount=remaining,
            payment_status=status,
            action=action,
            expected_deposit_percent=expected_percent,
            extra_checks=list(dict.fromkeys(
                check for linked in chain for check in _extra_checks(linked)
            )),
            components=components,
            currency_balances=currency_balances,
        ))
    reminders.sort(key=lambda item: (0 if item.timing == "today" else 1, item.guest_name.casefold()))
    return reminders


def format_settlement_section(reminders: list[SettlementReminder]) -> str:
    if not reminders:
        return ""
    lines = ["💳 CHECKOUT PAYMENTS"]
    for item in reminders:
        when = "COLLECT TODAY" if item.timing == "today" else "PREPARE FOR TOMORROW"
        lines.append(f"\n{when} — {item.guest_name} ({item.house})")
        if item.total_amount > 0:
            for currency, (total, paid, remaining) in item.currency_balances.items():
                lines.append(
                    f"Total {_money(total, currency)} · recorded {_money(paid, currency)} · remaining {_money(remaining, currency)}"
                )
        if len(item.components) > 1:
            lines.append("Linked reservation records:")
            for component in item.components:
                dates = (
                    f"{component.arrival_date.strftime('%d %b')} → {component.departure_date.strftime('%d %b')}"
                    if component.arrival_date else component.departure_date.strftime('%d %b')
                )
                lines.append(
                    f"• {component.channel} · {dates} · total {_money(component.total_amount, component.currency)} · "
                    f"recorded {_money(component.paid_amount, component.currency)} · remaining {_money(component.remaining_amount, component.currency)}"
                    + (f" · {component.payment_note}" if component.payment_note else "")
                )
        if item.expected_deposit_percent is not None:
            lines.append(f"Surf Camp policy: expected {item.expected_deposit_percent}% deposit")
        lines.append(item.action)
        if item.extra_checks:
            lines.append("Check extras: " + ", ".join(item.extra_checks))
        lines.append("Check group messages for anything not recorded.")
    return "\n".join(lines)
