"""
Google Calendar Service
Handles slot checking and appointment booking using a Service Account.
Zero cost - Google Calendar API is free for normal usage volumes.
"""

import json
import datetime
import logging
import pathlib
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.errors import HttpError

logger = logging.getLogger("calendar_service")

IST = ZoneInfo("Asia/Kolkata")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
APP_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _safe_credentials_path(credentials_file: str) -> str:
    """Resolve credentials path and ensure it stays within the app directory."""
    resolved = (APP_ROOT / credentials_file).resolve()
    if not str(resolved).startswith(str(APP_ROOT)):
        raise ValueError("Credentials file path is outside the application directory")
    if not resolved.exists():
        raise FileNotFoundError(f"Credentials file not found: {resolved}")
    return str(resolved)


def get_calendar_service(credentials_file: str):
    """Build and return a Google Calendar API service object."""
    safe_path = _safe_credentials_path(credentials_file)
    creds = service_account.Credentials.from_service_account_file(
        safe_path, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)


def get_available_slots(
    credentials_file: str,
    calendar_id: str,
    date_str: str,           # e.g. "2024-10-27"
    working_start: str,      # e.g. "09:00"
    working_end: str,        # e.g. "17:00"
    slot_duration: int = 30  # minutes
) -> list[dict]:
    """
    Returns list of free time slots on a given date.
    Example return: [{"start": "10:00", "end": "10:30"}, ...]
    """
    try:
        service = get_calendar_service(credentials_file)

        date = datetime.date.fromisoformat(date_str)
        start_h, start_m = map(int, working_start.split(":"))
        end_h, end_m = map(int, working_end.split(":"))

        day_start = datetime.datetime(date.year, date.month, date.day, start_h, start_m, tzinfo=IST)
        day_end = datetime.datetime(date.year, date.month, date.day, end_h, end_m, tzinfo=IST)

        # Fetch busy slots from Google Calendar
        body = {
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items": [{"id": calendar_id}]
        }
        freebusy = service.freebusy().query(body=body).execute()
        busy_periods = freebusy["calendars"].get(calendar_id, {}).get("busy", [])

        # Build list of all possible slots
        all_slots = []
        current = day_start
        while current + datetime.timedelta(minutes=slot_duration) <= day_end:
            slot_end = current + datetime.timedelta(minutes=slot_duration)
            all_slots.append((current, slot_end))
            current = slot_end

        # Filter out busy slots
        free_slots = []
        for slot_start, slot_end in all_slots:
            is_busy = False
            for busy in busy_periods:
                busy_start = datetime.datetime.fromisoformat(busy["start"])
                busy_end = datetime.datetime.fromisoformat(busy["end"])
                # Check overlap
                if slot_start < busy_end and slot_end > busy_start:
                    is_busy = True
                    break
            if not is_busy:
                free_slots.append({
                    "start": slot_start.strftime("%I:%M %p"),
                    "end": slot_end.strftime("%I:%M %p"),
                    "iso_start": slot_start.isoformat(),
                    "iso_end": slot_end.isoformat()
                })

        return free_slots

    except HttpError as e:
        logger.error(f"Google API error in get_available_slots: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error in get_available_slots: {e}")
        return []


def book_appointment(
    credentials_file: str,
    calendar_id: str,
    patient_name: str,
    doctor_name: str,
    doctor_email: str,
    start_iso: str,          # e.g. "2024-10-27T10:00:00+05:30"
    duration_minutes: int = 30,
    patient_phone: Optional[str] = None
) -> dict:
    """
    Creates a calendar event for the appointment.
    Returns {"success": True, "event_id": "..."} or {"success": False, "error": "..."}
    """
    try:
        service = get_calendar_service(credentials_file)

        start_dt = datetime.datetime.fromisoformat(start_iso)
        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)

        description = f"Patient: {patient_name}"
        if patient_phone:
            description += f"\nPhone: {patient_phone}"
        description += "\n\nBooked via AI Hospital Assistant (Asha)"

        event = {
            "summary": f"Appointment: {patient_name} with {doctor_name}",
            "description": description,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": "Asia/Kolkata"
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "Asia/Kolkata"
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 15}
                ]
            }
        }

        # sendUpdates="none" required — service accounts cannot send
        # email invites without Google Workspace Domain-Wide Delegation.
        created = service.events().insert(
            calendarId=calendar_id,
            body=event,
            sendUpdates="none"
        ).execute()

        return {
            "success": True,
            "event_id": created.get("id"),
            "event_link": created.get("htmlLink"),
            "start": start_dt.strftime("%d %B %Y at %I:%M %p"),
            "doctor": doctor_name
        }

    except HttpError as e:
        return {"success": False, "error": f"Google API error: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def cancel_appointment(credentials_file: str, calendar_id: str, event_id: str) -> dict:
    """Cancel an existing appointment by its event ID."""
    try:
        service = get_calendar_service(credentials_file)
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return {"success": True}
    except HttpError as e:
        return {"success": False, "error": str(e)}
