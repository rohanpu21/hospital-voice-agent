"""
Trilingual Language Detection Engine
Detects Hindi, Odia, and English in real-time from patient speech.

Strategy:
  1. Keyword-based fast detection (zero cost, instant)
  2. Script detection (Devanagari vs Odia Unicode vs Latin)
  3. Fallback to hospital's configured primary_lang
"""

import re
import unicodedata
from typing import Optional


# --- Odia keyword bank (transliterated to Roman script as patients often speak) ---
ODIA_KEYWORDS_ROMAN = [
    "darkara", "darka", "agya", "namaskar", "kemiti", "achanti",
    "daktara", "daktare", "hospital re", "appointment darkara",
    "miluchi", "nahi miluchi", "phisa", "beshi", "tharu", "mote",
    "apana", "apananka", "hela", "kaile", "kahali", "kal",
    "aau", "thikaa", "dhanyabad", "bhai", "bhaina", "mate",
    "tume", "sei", "kana", "kebe", "kathi", "jaga", "nahu",
    "ebe", "pariba", "boliba", "dekhiba", "asiba"
]

# --- Hindi keyword bank (Roman-script transliterations) ---
HINDI_KEYWORDS_ROMAN = [
    "chahiye", "chahte", "milna", "milni", "doctor hai", "available hai",
    "appointment chahiye", "time denge", "kab", "kaise", "kitna",
    "theek hai", "haan", "nahi", "kyun", "kahan", "kya", "karo",
    "karein", "abhi", "kal", "parso", "aaj", "subah", "shaam",
    "shukriya", "dhanyawad", "ji haan", "bilkul", "zaroor",
    "doctorji", "saab", "madam", "behen", "bhai", "please"
]

# --- Unicode ranges ---
DEVANAGARI_RANGE = (0x0900, 0x097F)   # Hindi Unicode block
ODIA_UNICODE_RANGE = (0x0B00, 0x0B7F) # Odia Unicode block


def detect_language_from_text(text: str, hospital_primary_lang: str = "hindi") -> str:
    """
    Detect language from transcript text.

    Returns:
        'od-IN'  — Odia
        'hi-IN'  — Hindi
        'en-IN'  — English

    Priority:
        1. Unicode script detection (most reliable for typed/Devanagari input)
        2. Keyword-based detection (for Romanized/transliterated speech)
        3. Hospital's configured primary language fallback
    """
    if not text or not text.strip():
        return _lang_code(hospital_primary_lang)

    text_lower = text.lower().strip()

    # --- Step 1: Unicode script detection ---
    odia_chars = 0
    hindi_chars = 0
    for char in text:
        cp = ord(char)
        if ODIA_UNICODE_RANGE[0] <= cp <= ODIA_UNICODE_RANGE[1]:
            odia_chars += 1
        elif DEVANAGARI_RANGE[0] <= cp <= DEVANAGARI_RANGE[1]:
            hindi_chars += 1

    if odia_chars > 0 and odia_chars >= hindi_chars:
        return "od-IN"
    if hindi_chars > 0:
        return "hi-IN"

    # --- Step 2: Keyword-based detection (Romanized speech from STT) ---
    odia_score = sum(1 for kw in ODIA_KEYWORDS_ROMAN if kw in text_lower)
    hindi_score = sum(1 for kw in HINDI_KEYWORDS_ROMAN if kw in text_lower)

    # Boosted score for longer/more specific keywords (avoid false positives)
    odia_score += sum(0.5 for kw in ODIA_KEYWORDS_ROMAN if len(kw) > 6 and kw in text_lower)
    hindi_score += sum(0.5 for kw in HINDI_KEYWORDS_ROMAN if len(kw) > 6 and kw in text_lower)

    if odia_score > hindi_score and odia_score >= 1:
        return "od-IN"
    if hindi_score > odia_score and hindi_score >= 1:
        return "hi-IN"

    # --- Step 3: Check if mostly ASCII/Latin (likely English) ---
    latin_chars = sum(1 for c in text if c.isalpha() and ord(c) < 128)
    total_alpha = sum(1 for c in text if c.isalpha())
    if total_alpha > 0 and (latin_chars / total_alpha) > 0.85:
        # Mostly Latin — but could be Romanized Hindi/Odia
        # If no keywords matched, default to primary language (Indian hospitals = rarely pure English)
        if odia_score == 0 and hindi_score == 0:
            return "en-IN"

    # --- Fallback: Hospital's primary language ---
    return _lang_code(hospital_primary_lang)


def _lang_code(lang: str) -> str:
    """Normalize language string to BCP-47 code."""
    lang = lang.lower().strip()
    mapping = {
        "odia": "od-IN",
        "oriya": "od-IN",
        "od-in": "od-IN",
        "hindi": "hi-IN",
        "hi-in": "hi-IN",
        "english": "en-IN",
        "en-in": "en-IN"
    }
    return mapping.get(lang, "hi-IN")


def get_tts_language(detected_lang: str, hospital_primary_lang: str) -> str:
    """
    Decide which language to USE for TTS response.
    Simple rule: mirror what the patient spoke.
    """
    if detected_lang in ("od-IN", "hi-IN", "en-IN"):
        return detected_lang
    return _lang_code(hospital_primary_lang)


def format_time_for_language(time_str: str, language: str) -> str:
    """
    Format a time string naturally for the given language.
    e.g. "10:30 AM" → "das baje tees minute" (Hindi) or "dasha ghantaa" (Odia)
    For now, returns a readable 12-hour format.
    """
    # Simple pass-through — the LLM handles natural phrasing
    return time_str


def get_confirmation_message(
    doctor_name: str,
    time_str: str,
    patient_name: str,
    language: str
) -> str:
    """Return a pre-built confirmation string in the correct language."""
    templates = {
        "od-IN": f"{patient_name} agya, apananka appointment confirm hela. {doctor_name} nka sahita {time_str} re. Dhanyabad Agya.",
        "hi-IN": f"{patient_name} Ji, aapka appointment confirm ho gaya. {doctor_name} ke saath {time_str} baje. Shukriya Ji.",
        "en-IN": f"{patient_name}, your appointment is confirmed with {doctor_name} at {time_str}. Thank you!"
    }
    return templates.get(language, templates["hi-IN"])
