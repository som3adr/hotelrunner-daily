"""
surf_schedule.py
────────────────
Surf schedule loading and surf/meal conflict detection.

- Loads today's surf sessions from Google Sheet (via google_sheets.py)
- Fetches live conditions from Open-Meteo (free, no API key)
- Applies local Imsouane spot rules: Bay = low/mid tide, Cathedral = high/mid tide
- Detects conflicts between surf times and meal times
- Checks early surf vs breakfast implications
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.request
import urllib.error
from typing import Any

from data_model import NormalizedReservation, load_config


# ── Conditions from Open-Meteo (free, no key needed) ─────────────────────────

def fetch_conditions(date: dt.date | None = None) -> dict[str, Any]:
    """
    Fetch wave + wind forecast for Imsouane from Open-Meteo.
    Returns a dict with today's and tomorrow's conditions.
    Gracefully returns empty dict on network failure.
    """
    cfg = load_config()
    loc = cfg.get("camp", {}).get("location", {})
    lat = loc.get("latitude", 30.85)
    lon = loc.get("longitude", -9.79)

    marine_url = (
        f"https://marine-api.open-meteo.com/v1/marine"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=swell_wave_height,swell_wave_period,wave_height,wave_direction,wave_period"
        f"&daily=wave_height_max,swell_wave_height_max"
        f"&timezone=Africa/Casablanca&forecast_days=2"
    )
    weather_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=wind_speed_10m,wind_direction_10m"
        f"&daily=wind_speed_10m_max,wind_direction_10m_dominant,wind_gusts_10m_max"
        f"&timezone=Africa/Casablanca&forecast_days=2"
    )

    marine = _fetch_json(marine_url)
    weather = _fetch_json(weather_url)

    target_date = date or dt.date.today()
    return _extract_conditions(marine, weather, target_date)


def _fetch_json(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OlasManager/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return {}


def _extract_conditions(marine: dict, weather: dict, date: dt.date) -> dict[str, Any]:
    """Extract relevant daily and key-hour conditions for the given date."""
    result: dict[str, Any] = {
        "date": date.isoformat(),
        "available": bool(marine or weather),
        "swell_height_max": None,
        "swell_period": None,
        "wave_direction": None,
        "wind_speed_max_kmh": None,
        "wind_direction": None,
        "wind_gusts_kmh": None,
        "morning_swell": None,     # 07:00-09:00 average
        "morning_wind_kmh": None,
        "afternoon_swell": None,   # 13:00-15:00 average
        "afternoon_wind_kmh": None,
    }

    # Daily maximums
    daily = marine.get("daily", {})
    times = daily.get("time", [])
    date_str = date.isoformat()
    if date_str in times:
        idx = times.index(date_str)
        swell_max = (daily.get("swell_wave_height_max") or [])[idx:idx+1]
        wave_max = (daily.get("wave_height_max") or [])[idx:idx+1]
        result["swell_height_max"] = swell_max[0] if swell_max else None
        if not result["swell_height_max"]:
            result["swell_height_max"] = wave_max[0] if wave_max else None

    w_daily = weather.get("daily", {})
    w_times = w_daily.get("time", [])
    if date_str in w_times:
        idx = w_times.index(date_str)
        result["wind_speed_max_kmh"] = _get_idx(w_daily.get("wind_speed_10m_max"), idx)
        result["wind_gusts_kmh"] = _get_idx(w_daily.get("wind_gusts_10m_max"), idx)
        result["wind_direction"] = _get_idx(w_daily.get("wind_direction_10m_dominant"), idx)

    # Hourly for morning/afternoon
    h_times = marine.get("hourly", {}).get("time", [])
    for label, hours in [("morning", [7, 8, 9]), ("afternoon", [13, 14, 15])]:
        swells = []
        for h in hours:
            ts = f"{date_str}T{h:02d}:00"
            if ts in h_times:
                idx = h_times.index(ts)
                v = _get_idx(marine.get("hourly", {}).get("swell_wave_height"), idx)
                if v is not None:
                    swells.append(v)
        if swells:
            result[f"{label}_swell"] = round(sum(swells) / len(swells), 2)

    # Wave direction (from hourly, 08:00)
    morning_ts = f"{date_str}T08:00"
    if morning_ts in h_times:
        idx = h_times.index(morning_ts)
        result["wave_direction"] = _get_idx(marine.get("hourly", {}).get("wave_direction"), idx)
        result["swell_period"] = _get_idx(marine.get("hourly", {}).get("swell_wave_period"), idx)

    # Wind at morning hours from hourly weather
    w_h_times = weather.get("hourly", {}).get("time", [])
    morning_winds = []
    for h in [7, 8, 9]:
        ts = f"{date_str}T{h:02d}:00"
        if ts in w_h_times:
            idx = w_h_times.index(ts)
            v = _get_idx(weather.get("hourly", {}).get("wind_speed_10m"), idx)
            if v is not None:
                morning_winds.append(v)
    if morning_winds:
        result["morning_wind_kmh"] = round(sum(morning_winds) / len(morning_winds), 1)

    return result


def _get_idx(lst: list | None, idx: int) -> Any:
    if lst and 0 <= idx < len(lst):
        return lst[idx]
    return None


def _wind_direction_name(degrees: float | None) -> str:
    if degrees is None:
        return ""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(degrees / 22.5) % 16
    return dirs[idx]


def format_conditions_summary(conditions: dict[str, Any]) -> str:
    """Format conditions into a short Telegram-friendly summary."""
    if not conditions.get("available"):
        return "Conditions: data unavailable"

    parts = []

    swell = conditions.get("swell_height_max") or conditions.get("morning_swell")
    if swell is not None:
        parts.append(f"Swell: {swell:.1f}m")

    period = conditions.get("swell_period")
    if period is not None:
        parts.append(f"{period:.0f}s")

    wind_max = conditions.get("wind_speed_max_kmh")
    wind_dir = _wind_direction_name(conditions.get("wind_direction"))
    if wind_max is not None:
        parts.append(f"Wind: {wind_max:.0f}km/h {wind_dir}".strip())

    return " · ".join(parts) if parts else "No data"


def spot_assessment(spot: str, conditions: dict[str, Any]) -> str:
    """
    Apply local Imsouane rules to assess a spot.
    NOTE: This is INFORMATION ONLY. Coach makes final decision.
    """
    cfg = load_config()
    spot_cfg = cfg.get("surf", {}).get("spots", {}).get(spot, {})
    preferred = spot_cfg.get("preferred_tide", [])
    description = spot_cfg.get("description", "")
    return f"{spot}: {description} (coach decides)"


# ── Surf session model ────────────────────────────────────────────────────────

def parse_time(time_str: str) -> dt.time | None:
    """Parse '06:30', '6:30', '0630' into dt.time."""
    if not time_str:
        return None
    time_str = time_str.strip().replace("h", ":").replace(".", ":")
    for fmt in ("%H:%M", "%H%M", "%I:%M%p", "%I:%M %p"):
        try:
            return dt.datetime.strptime(time_str, fmt).time()
        except ValueError:
            continue
    return None


def session_end_time(start: dt.time, cfg: dict) -> dt.time:
    """Calculate expected end time of a surf session."""
    duration = cfg.get("surf", {}).get("default_duration_minutes", 120)
    buffer = cfg.get("surf", {}).get("return_buffer_minutes", 30)
    total_minutes = duration + buffer
    start_dt = dt.datetime.combine(dt.date.today(), start)
    end_dt = start_dt + dt.timedelta(minutes=total_minutes)
    return end_dt.time()


# ── Conflict detection ────────────────────────────────────────────────────────

def detect_surf_meal_conflicts(
    sessions: list[dict],
    reservations: list[NormalizedReservation],
    date: dt.date,
) -> list[dict]:
    """
    Detect conflicts between surf sessions and meal times.

    Returns list of conflict dicts with severity, description, and recommended_action.
    """
    cfg = load_config()
    meals_cfg = cfg.get("meals", {})

    # Parse meal times
    def _meal_time(key: str) -> dt.time | None:
        return parse_time(meals_cfg.get(key, ""))

    breakfast_time = _meal_time("breakfast_time") or dt.time(8, 30)
    lunch_time = _meal_time("lunch_time") or dt.time(14, 30)
    dinner_time = _meal_time("dinner_time") or dt.time(19, 30)

    conflicts = []

    # Count guests with each meal type in-house today
    def count_meal_guests(meal_type: str) -> int:
        count = 0
        for res in reservations:
            if not res.is_active_on(date):
                continue
            plan = res.meal_plan.casefold()
            if meal_type == "breakfast" and ("breakfast" in plan or "b&b" in plan
                                              or "bed and breakfast" in plan
                                              or "half board" in plan
                                              or "all inclusive" in plan):
                count += res.guest_count
            elif meal_type == "lunch" and ("all inclusive" in plan or "full board" in plan):
                count += res.guest_count
            elif meal_type == "dinner" and ("half board" in plan or "full board" in plan
                                            or "all inclusive" in plan):
                count += res.guest_count
        return count

    total_breakfast = count_meal_guests("breakfast")
    total_lunch = count_meal_guests("lunch")
    total_dinner = count_meal_guests("dinner")

    for session in sessions:
        start_str = session.get("time", "")
        start = parse_time(start_str)
        if not start:
            continue

        guest_count = session.get("guest_count", 0)
        level = session.get("level", "")
        spot = session.get("spot", "")
        end = session_end_time(start, cfg)
        end_str = end.strftime("%H:%M")

        # Check: surf ends after lunch
        if start < lunch_time <= end:
            surfers_with_lunch = min(guest_count, total_lunch)
            if surfers_with_lunch > 0:
                conflicts.append({
                    "severity": "action_required",
                    "category": "SURF_LUNCH_CONFLICT",
                    "title": "SURF / LUNCH CONFLICT",
                    "description": (
                        f"{surfers_with_lunch} guests with lunch included have surf at {start_str}.\n"
                        f"Expected return: ~{end_str}.\n"
                        f"Normal lunch: {lunch_time.strftime('%H:%M')}."
                    ),
                    "recommended_action": (
                        f"Inform kitchen before preparation.\n"
                        f"Keep {surfers_with_lunch} lunches for ~{end_str}."
                    ),
                    "session": session,
                })

        # Check: early surf affects breakfast
        if start < breakfast_time:
            surfers_with_breakfast = min(guest_count, total_breakfast)
            if surfers_with_breakfast > 0:
                conflicts.append({
                    "severity": "info",
                    "category": "EARLY_SURF_BREAKFAST",
                    "title": "EARLY SURF — BREAKFAST TIMING",
                    "description": (
                        f"Surf session at {start_str} ({level}) before breakfast time "
                        f"({breakfast_time.strftime('%H:%M')}).\n"
                        f"~{surfers_with_breakfast} surfers have breakfast included.\n"
                        f"Expected return: ~{end_str}."
                    ),
                    "recommended_action": (
                        f"Inform kitchen: {surfers_with_breakfast} surfers return ~{end_str}."
                    ),
                    "session": session,
                })

        # Check: surf ends after dinner
        if start < dinner_time <= end:
            surfers_with_dinner = min(guest_count, total_dinner)
            if surfers_with_dinner > 0:
                conflicts.append({
                    "severity": "check",
                    "category": "SURF_DINNER_CONFLICT",
                    "title": "SURF / DINNER TIMING",
                    "description": (
                        f"{surfers_with_dinner} dinner guests have surf at {start_str}.\n"
                        f"Expected return: ~{end_str}.\n"
                        f"Normal dinner: {dinner_time.strftime('%H:%M')}."
                    ),
                    "recommended_action": "Inform kitchen of late return if needed.",
                    "session": session,
                })

    return conflicts
