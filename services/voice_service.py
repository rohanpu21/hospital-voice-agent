"""
Sarvam AI Voice Service
Handles Speech-to-Text (STT) and Text-to-Speech (TTS) for Indian languages.
Supports Hindi (hi-IN), Odia (od-IN), and English (en-IN).

Cost: ~₹30/hour of audio processed. Cheapest option for Indian regional languages.
Sarvam API Docs: https://docs.sarvam.ai
"""

import os
import base64
import requests
from typing import Optional

SARVAM_BASE_URL = "https://api.sarvam.ai"
SARVAM_KEY = os.getenv("SARVAM_API_KEY", "")

# Voice mapping per language — Sarvam bulbul:v1 voices
VOICE_MAP = {
    "od-IN": "pavithra",  # Odia — natural Odia female voice
    "hi-IN": "meera",     # Hindi — natural Hindi female voice
    "en-IN": "sarita",    # English (Indian accent)
}

# Speaker model map per language for STT
STT_LANGUAGE_MAP = {
    "od-IN": "od-IN",
    "hi-IN": "hi-IN",
    "en-IN": "en-IN",
    "auto": "hi-IN"  # fallback for auto-detect
}


def speech_to_text(audio_bytes: bytes, language_code: str = "hi-IN") -> dict:
    """
    Convert audio bytes to text using Sarvam STT.

    Args:
        audio_bytes: Raw audio bytes (WAV format, 16kHz, mono preferred)
        language_code: 'hi-IN', 'od-IN', or 'en-IN'

    Returns:
        {"transcript": "...", "language": "hi-IN", "success": True}
    """
    if not SARVAM_KEY:
        return {"transcript": "", "success": False, "error": "SARVAM_API_KEY not set"}

    # Sarvam expects base64-encoded audio or multipart file upload
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    payload = {
        "model": "saaras:v1",          # Sarvam's best STT model for Indian accents
        "language_code": language_code,
        "with_timestamps": False,
        "with_disfluencies": False
    }

    headers = {
        "api-subscription-key": SARVAM_KEY,
        "Content-Type": "application/json"
    }

    # Sarvam speech-to-text endpoint (file upload)
    files = {
        "file": ("audio.wav", audio_bytes, "audio/wav"),
        "model": (None, "saaras:v1"),
        "language_code": (None, language_code)
    }

    try:
        response = requests.post(
            f"{SARVAM_BASE_URL}/speech-to-text",
            files=files,
            headers={"api-subscription-key": SARVAM_KEY},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return {
            "transcript": data.get("transcript", ""),
            "language": language_code,
            "success": True
        }
    except requests.exceptions.RequestException as e:
        return {"transcript": "", "success": False, "error": str(e)}


def text_to_speech(text: str, language_code: str = "hi-IN") -> dict:
    """
    Convert text to speech audio using Sarvam TTS (bulbul:v1).

    Args:
        text: Text to speak (keep under 500 chars for low latency)
        language_code: 'hi-IN', 'od-IN', or 'en-IN'

    Returns:
        {"audio_bytes": bytes, "success": True} or {"success": False, "error": "..."}

    Cost: ~₹30 per 10,000 characters
    """
    if not SARVAM_KEY:
        return {"audio_bytes": None, "success": False, "error": "SARVAM_API_KEY not set"}

    speaker = VOICE_MAP.get(language_code, "meera")

    payload = {
        "inputs": [text],
        "target_language_code": language_code,
        "speaker": speaker,
        "model": "bulbul:v1",       # Best Indian language TTS model
        "pitch": 0,
        "pace": 1.0,                # Normal speed — crucial for phone calls
        "loudness": 1.5,
        "speech_sample_rate": 8000, # 8kHz — standard for telephony (PCMU/G.711)
        "enable_preprocessing": True
    }

    headers = {
        "api-subscription-key": SARVAM_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            f"{SARVAM_BASE_URL}/text-to-speech",
            json=payload,
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        data = response.json()

        # Sarvam returns base64-encoded audio
        audio_b64 = data.get("audios", [""])[0]
        if not audio_b64:
            return {"audio_bytes": None, "success": False, "error": "Empty audio response"}

        audio_bytes = base64.b64decode(audio_b64)
        return {"audio_bytes": audio_bytes, "success": True}

    except requests.exceptions.RequestException as e:
        return {"audio_bytes": None, "success": False, "error": str(e)}


def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """
    Translate text between Indian languages using Sarvam translate API.
    Useful for confirming bookings in the patient's preferred language.

    Cost: Negligible — included in Sarvam subscription.
    """
    if not SARVAM_KEY:
        return text  # fallback: return original

    payload = {
        "input": text,
        "source_language_code": source_lang,
        "target_language_code": target_lang,
        "model": "mayura:v1",       # Sarvam's translation model
        "mode": "formal"
    }

    headers = {
        "api-subscription-key": SARVAM_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            f"{SARVAM_BASE_URL}/translate",
            json=payload,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json().get("translated_text", text)
    except Exception:
        return text  # fallback: return original text
