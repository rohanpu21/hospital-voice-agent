"""
SMS Confirmation Service (Optional)
Uses Fast2SMS — cheapest Indian SMS provider (~₹0.20/SMS)
Get API key at fast2sms.com

Only sends appointment confirmation SMS to patient after booking.
"""

import os
import requests
from typing import Optional

FAST2SMS_KEY = os.getenv("FAST2SMS_API_KEY", "")
FAST2SMS_URL = "https://www.fast2sms.com/dev/bulkV2"


def send_appointment_sms(
    phone: str,
    patient_name: str,
    doctor_name: str,
    appointment_time: str,
    hospital_name: str,
    language: str = "hi-IN"
) -> dict:
    """
    Send appointment confirmation SMS to patient.
    Cost: ~₹0.20 per SMS via Fast2SMS.

    Args:
        phone: Indian mobile number (10 digits, no +91)
        language: 'hi-IN', 'od-IN', or 'en-IN'
    """
    if not FAST2SMS_KEY:
        print("[SMS] FAST2SMS_API_KEY not set — skipping SMS")
        return {"success": False, "error": "FAST2SMS_API_KEY not configured"}

    # Keep SMS short — DLT regulations require pre-approved templates in India
    # This is a generic format — register your template at fast2sms.com DLT panel
    messages = {
        "hi-IN": f"Priy {patient_name} Ji, aapka appointment {doctor_name} ke saath {appointment_time} ko confirm hua hai. -{hospital_name}",
        "od-IN": f"{patient_name} Agya, apananka appointment {doctor_name} nka sahita {appointment_time} re confirm hela. -{hospital_name}",
        "en-IN": f"Dear {patient_name}, your appointment with {doctor_name} is confirmed at {appointment_time}. -{hospital_name}"
    }
    message = messages.get(language, messages["hi-IN"])
    # Truncate to 160 chars (1 SMS unit)
    message = message[:160]

    # Strip +91 prefix if present
    phone_clean = phone.replace("+91", "").replace(" ", "").strip()

    payload = {
        "authorization": FAST2SMS_KEY,
        "route": "q",              # Transactional route (non-DND)
        "message": message,
        "language": "english",
        "flash": 0,
        "numbers": phone_clean
    }

    try:
        response = requests.post(FAST2SMS_URL, json=payload, timeout=10)
        data = response.json()
        if data.get("return"):
            return {"success": True, "message_id": data.get("request_id")}
        return {"success": False, "error": data.get("message", "Unknown error")}
    except Exception as e:
        return {"success": False, "error": str(e)}
