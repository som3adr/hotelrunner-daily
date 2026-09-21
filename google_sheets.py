"""
google_sheets.py
────────────────
Read-only Google Sheets integration for Sunrise house reservations.

Setup (one-time):
  1. Go to console.cloud.google.com → Enable Google Sheets API
  2. Create a Service Account → download JSON key
  3. Share the spreadsheet with the service account email
  4. Add the JSON content as GOOGLE_CREDENTIALS_JSON in .env or GitHub Secrets

The script gracefully skips Sunrise data if credentials are not configured.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from data_model import NormalizedReservation, load_config, sheet_row_to_normalized


def _load_gspread():
    """Import gspread only when needed (avoids ImportError if not installed)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        return gspread, Credentials
    except ImportError:
        return None, None


def _get_credentials_path() -> Path | None:
    """
    Find Google credentials from:
    1. GOOGLE_CREDENTIALS_JSON env var (JSON string — used in GitHub Actions)
    2. credentials/google_credentials.json file (local development)
    """
    # Option 1: JSON string in environment variable
    json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if json_str:
        try:
            data = json.loads(json_str)
            # Write to a temp file so gspread can read it
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            )
            json.dump(data, tmp)
            tmp.close()
            return Path(tmp.name)
        except (json.JSONDecodeError, OSError):
            return None

    # Option 2: Local credentials file
    local = Path(__file__).parent / "credentials" / "google_credentials.json"
    if local.exists():
        return local

    return None


def load_sunrise_reservations(cache_path: Path | None = None) -> list[NormalizedReservation]:
    """
    Load all reservations from the Sunrise Google Sheet.
    Returns empty list if credentials are not configured or gspread not installed.
    """
    gspread, Credentials = _load_gspread()
    if gspread is None:
        print("[google_sheets] gspread not installed. Run: pip install gspread google-auth")
        return []

    creds_path = _get_credentials_path()
    if creds_path is None:
        print("[google_sheets] No Google credentials found. Sunrise data not loaded.")
        print("  Add GOOGLE_CREDENTIALS_JSON to .env or put credentials/google_credentials.json")
        return []

    cfg = load_config()
    sheets_cfg = cfg.get("google_sheets", {})
    spreadsheet_id = sheets_cfg.get("spreadsheet_id", "")
    tab_name = sheets_cfg.get("sunrise_tab", "Sheet1")

    if not spreadsheet_id:
        print("[google_sheets] No spreadsheet_id in config.json")
        return []

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        client = gspread.authorize(creds)
        # Select worksheet: try configured tab, then month name, then first available
        ws = None
        available_worksheets = client.open_by_key(spreadsheet_id).worksheets()
        month_name = dt.date.today().strftime("%B")
        for candidate in [tab_name, month_name, "September", "Sheet1"]:
            for w in available_worksheets:
                if w.title.casefold() == candidate.casefold():
                    ws = w
                    break
            if ws:
                break
        if not ws and available_worksheets:
            ws = available_worksheets[0]

        if not ws:
            print("[google_sheets] No worksheet found in spreadsheet.")
            return []

        all_values = ws.get_all_values()
        if not all_values:
            return []

        # Find header row containing 'Guest Name' or 'Check-in'
        header_idx = -1
        headers = []
        for idx, row in enumerate(all_values):
            lower_row = [str(cell).strip().casefold() for cell in row]
            if "guest name" in lower_row or "check-in" in lower_row:
                header_idx = idx
                headers = [str(cell).strip() for cell in row]
                break

        if header_idx == -1:
            print("[google_sheets] Could not find header row with 'Guest Name' or 'Check-in'")
            return []

        records = []
        for r_idx in range(header_idx + 1, len(all_values)):
            row_data = all_values[r_idx]
            # Skip completely empty rows
            if not any(str(c).strip() for c in row_data):
                continue
            row_dict = {}
            for col_idx, col_name in enumerate(headers):
                if col_name and col_idx < len(row_data):
                    row_dict[col_name] = str(row_data[col_idx]).strip()
            records.append(row_dict)

    except Exception as exc:
        print(f"[google_sheets] Failed to load Sunrise sheet: {exc}")
        return []
    finally:
        # Clean up temp file if we created one
        json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
        if json_str and creds_path and str(creds_path).startswith(tempfile.gettempdir()):
            try:
                creds_path.unlink()
            except OSError:
                pass

    reservations: list[NormalizedReservation] = []
    for i, row in enumerate(records, start=header_idx + 2):
        res = sheet_row_to_normalized(row, row_index=i)
        if res is not None:
            reservations.append(res)

    print(f"[google_sheets] Loaded {len(reservations)} Sunrise reservations from tab '{ws.title}'")
    return reservations



def load_surf_schedule(date: dt.date | None = None) -> list[dict[str, Any]]:
    """
    Load surf schedule from the 'Surf Schedule' tab in the Google Sheet.

    Expected columns: Date | Time | Level | Spot | Guests | Notes

    Returns list of session dicts for the requested date (defaults to today).
    """
    gspread, Credentials = _load_gspread()
    if gspread is None:
        return []

    creds_path = _get_credentials_path()
    if creds_path is None:
        return []

    cfg = load_config()
    sheets_cfg = cfg.get("google_sheets", {})
    spreadsheet_id = sheets_cfg.get("spreadsheet_id", "")
    surf_tab = cfg.get("surf", {}).get("google_sheet_tab", "Surf Schedule")
    col_map = cfg.get("surf", {}).get("schedule_columns", {})

    target_date = date or dt.date.today()

    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        client = gspread.authorize(creds)
        try:
            sheet = client.open_by_key(spreadsheet_id).worksheet(surf_tab)
        except gspread.exceptions.WorksheetNotFound:
            print(f"[google_sheets] Surf Schedule tab '{surf_tab}' not found. Create it in your Google Sheet.")
            return []
        records = sheet.get_all_records()
    except Exception as exc:
        print(f"[google_sheets] Failed to load surf schedule: {exc}")
        return []
    finally:
        json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
        if json_str and creds_path and str(creds_path).startswith(tempfile.gettempdir()):
            try:
                creds_path.unlink()
            except OSError:
                pass

    date_col = col_map.get("date", "Date")
    time_col = col_map.get("time", "Time")
    level_col = col_map.get("level", "Level")
    spot_col = col_map.get("spot", "Spot")
    guests_col = col_map.get("guests", "Guests")
    notes_col = col_map.get("notes", "Notes")

    sessions = []
    for row in records:
        raw_date = str(row.get(date_col) or "").strip()
        if not raw_date:
            continue
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                row_date = dt.datetime.strptime(raw_date[:10], fmt).date()
                break
            except ValueError:
                row_date = None

        if row_date != target_date:
            continue

        raw_time = str(row.get(time_col) or "").strip()
        try:
            guests_count = int(str(row.get(guests_col) or 0).strip())
        except (ValueError, TypeError):
            guests_count = 0

        sessions.append({
            "date": row_date,
            "time": raw_time,
            "level": str(row.get(level_col) or "").strip(),
            "spot": str(row.get(spot_col) or "").strip(),
            "guest_count": guests_count,
            "notes": str(row.get(notes_col) or "").strip(),
        })

    sessions.sort(key=lambda s: s["time"])
    return sessions
