"""
meal_engine.py
──────────────
Auditable, deduplicated meal entitlement engine.

Rules:
  - Each guest is counted ONCE per meal regardless of how many sources
    confirm it (meal plan + note + extra all confirming dinner = 1 dinner).
  - Only extras explicitly representing ADDITIONAL PEOPLE add to the count.
  - Unknown/ambiguous meal wording → AttentionItem (never guessed).
  - Supports HotelRunner (meal_plan + extras) and Google Sheet (meal_plan from package).

Public API:
  compute_meal_entitlements(reservations, date) -> list[MealEntitlement]
  summarize_dinner(entitlements) -> dict
  dinner_preparation_notice(count) -> str | None
  flag_ambiguous_meals(reservations, date) -> list[AttentionItem]
"""
from __future__ import annotations

import datetime as dt
import re

from data_model import (
    NormalizedReservation, MealEntitlement, AttentionItem,
    dinner_setup_rule, load_config,
)


# ── Note-based meal detection ─────────────────────────────────────────────────

# Positive patterns: clearly identify a specific meal from a note
_DINNER_NOTE_PATTERNS = [
    r"\bdiner\b", r"\bdîner\b", r"\bdinner\b", r"\bsouper\b",
    r"\bdemi.pension\b", r"\bhalf.board\b", r"\brepas du soir\b",
]
_LUNCH_NOTE_PATTERNS = [
    r"\bdejeun", r"\bdéjeun", r"\blunch\b", r"\bmidi\b",
]
_BREAKFAST_NOTE_PATTERNS = [
    r"\bpetit.d[eé]jeuner\b", r"\bbreakfast\b", r"\bpetit dej\b",
]

# Ambiguous patterns: mention meals but can't be cleanly parsed
_AMBIGUOUS_NOTE_PATTERNS = [
    r"\brepas\b", r"\bmeal\b", r"\bfood\b", r"\bnourriture\b",
    r"\btable\b.*\bguest\b", r"\bjoin.*dinner\b", r"\bmanges?\b",
]


def parse_note_for_meal(note_text: str) -> str | None:
    """
    Returns 'dinner', 'lunch', 'breakfast', or None.
    Returns None for ambiguous or unrelated text.
    """
    t = note_text.casefold()
    if any(re.search(p, t) for p in _DINNER_NOTE_PATTERNS):
        return "dinner"
    if any(re.search(p, t) for p in _LUNCH_NOTE_PATTERNS):
        return "lunch"
    if any(re.search(p, t) for p in _BREAKFAST_NOTE_PATTERNS):
        return "breakfast"
    return None


def _note_is_ambiguous(note_text: str) -> bool:
    """True if note mentions food/meals but can't be cleanly categorized."""
    t = note_text.casefold()
    # Only ambiguous if NOT already cleanly parseable
    if parse_note_for_meal(t) is not None:
        return False
    return any(re.search(p, t) for p in _AMBIGUOUS_NOTE_PATTERNS)


# ── Core entitlement computation ──────────────────────────────────────────────

def _meals_from_plan(meal_plan: str) -> set[str]:
    """Return set of {'breakfast','lunch','dinner'} from plan string."""
    cfg = load_config()
    plans = cfg.get("meals", {}).get("plans", {})
    plan_lower = meal_plan.casefold()
    for key, flags in plans.items():
        if key in plan_lower or plan_lower in key:
            result: set[str] = set()
            if flags.get("breakfast"):
                result.add("breakfast")
            if flags.get("lunch"):
                result.add("lunch")
            if flags.get("dinner"):
                result.add("dinner")
            return result
    return set()


def _meals_from_extras(res: NormalizedReservation) -> set[str]:
    """Return set of meal types confirmed by extras."""
    result: set[str] = set()
    cfg = load_config()
    keywords = cfg.get("meals", {}).get("meal_extra_keywords", {})
    for extra in res.meal_extras:
        label = extra.raw_label.casefold()
        for meal_type, kws in keywords.items():
            if any(kw in label for kw in kws):
                result.add(meal_type)
    return result


def _meals_from_notes(res: NormalizedReservation) -> set[str]:
    """Return set of meal types confirmed by notes."""
    result: set[str] = set()
    for note in res.notes:
        meal = parse_note_for_meal(note)
        if meal:
            result.add(meal)
    return result


def compute_meal_entitlements(
    reservations: list[NormalizedReservation],
    date: dt.date,
) -> list[MealEntitlement]:
    """
    Compute deduplicated meal entitlements for all active guests on the given date.

    Deduplication rule: each guest is counted ONCE per meal regardless of how
    many sources (plan + extra + note) confirm the same meal.
    A guest with Half Board + a 'dinner' note = 1 dinner entitlement, not 2.
    """
    entitlements: list[MealEntitlement] = []

    for res in reservations:
        if not res.is_active_on(date):
            continue

        # Collect confirmed meals from all sources (union = deduplication)
        from_plan = _meals_from_plan(res.meal_plan)
        from_extras = _meals_from_extras(res)
        from_notes = _meals_from_notes(res)
        confirmed_meals = from_plan | from_extras | from_notes

        guest_count = res.guest_count or 1

        for meal in ("breakfast", "lunch", "dinner"):
            if meal not in confirmed_meals:
                continue

            # Determine source label for audit trail
            sources: list[str] = []
            if meal in from_plan:
                sources.append(res.meal_plan or "meal plan")
            if meal in from_extras:
                sources.append("extra")
            if meal in from_notes:
                sources.append("note")
            reason = " + ".join(sources) if sources else (res.meal_plan or "unknown")

            # Determine primary source
            if from_plan and meal in from_plan:
                primary_source = "meal_plan"
            elif from_extras and meal in from_extras:
                primary_source = "extra"
            else:
                primary_source = "note"

            entitlements.append(MealEntitlement(
                reservation_id=res.reservation_id,
                guest_name=res.guest_name,
                house=res.house,
                date=date,
                meal=meal,
                count=guest_count,
                source=primary_source,
                reason_text=reason,
            ))

    return entitlements


# ── Dinner summary ────────────────────────────────────────────────────────────

def summarize_dinner(entitlements: list[MealEntitlement]) -> dict:
    """
    Summarize dinner entitlements.
    Returns:
      {
        'total': int,
        'by_house': {'Olas': n, 'Tide': n, 'Sunrise': n},
        'audit': [MealEntitlement, ...],
        'setup': 'normal' | 'extra_tables' | 'capacity_warning',
        'notice': str | None,
      }
    """
    dinner_items = [e for e in entitlements if e.meal == "dinner"]
    total = sum(e.count for e in dinner_items)

    by_house: dict[str, int] = {}
    for e in dinner_items:
        by_house[e.house] = by_house.get(e.house, 0) + e.count

    setup = dinner_setup_rule(total)
    notice = dinner_preparation_notice(total)

    return {
        "total": total,
        "by_house": by_house,
        "audit": dinner_items,
        "setup": setup,
        "notice": notice,
    }


# ── Dinner preparation notice ─────────────────────────────────────────────────

def dinner_preparation_notice(count: int) -> str | None:
    """
    Returns a warning string if extra tables or capacity action is needed.
    Returns None if setup is normal (≤13 guests).
    Note: number of extra tables is NOT calculated — seats-per-table not configured.
    """
    rule = dinner_setup_rule(count)
    if rule == "capacity_warning":
        return f"🔴 CAPACITY WARNING — {count} dinner guests exceeds capacity of 24"
    if rule == "extra_tables":
        return f"⚠️ Extra tables required ({count} dinner guests)"
    return None


# ── Ambiguous meal flagging ───────────────────────────────────────────────────

def flag_ambiguous_meals(
    reservations: list[NormalizedReservation],
    date: dt.date,
) -> list[AttentionItem]:
    """
    Flag reservations with notes or extras that mention food/meals
    but cannot be cleanly parsed into an entitlement.
    These require manager confirmation — never guessed.
    """
    items: list[AttentionItem] = []

    for res in reservations:
        if not res.is_active_on(date) and not res.is_arriving_on(date):
            continue

        for note in res.notes:
            if _note_is_ambiguous(note):
                items.append(AttentionItem(
                    category="meal",
                    severity="check",
                    title=f"Ambiguous meal note — {res.guest_name}",
                    description=f"Note may reference a meal but could not be parsed: '{note[:80]}'",
                    reservation_id=res.reservation_id,
                ))

        for extra in res.unknown_extras:
            label = extra.raw_label.casefold()
            if any(kw in label for kw in ("meal", "dinner", "lunch", "breakfast", "repas", "food")):
                items.append(AttentionItem(
                    category="meal",
                    severity="check",
                    title=f"Unknown meal extra — {res.guest_name}",
                    description=f"Unrecognized extra may be a meal: '{extra.raw_label}'",
                    reservation_id=res.reservation_id,
                ))

    return items
