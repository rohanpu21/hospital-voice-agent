"""
Hospital Voice Agent — FastAPI Application
==========================================
Exposes webhook endpoints for telephony providers (Exotel / Plivo / CloudBharat SIP).

Endpoints:
  POST /call/start          — Incoming call handler (returns greeting audio)
  POST /call/input          — Patient speech input handler
  POST /test/chat           — Text-only test — no phone needed
  GET  /health              — Health check
  GET  /hospitals           — List configured hospitals (admin)

  --- Exotel Telephony (real phone calls) ---
  POST /exotel/incoming     — Exotel passthru webhook (new call)
  POST /exotel/handle-speech— Exotel recording callback (speech loop)
  GET  /audio/{filename}    — Serve cached TTS audio for Exotel <Play>

Session State:
  In-memory dict (per call_id) — good enough for MVP.
  For production, swap with Redis.

Run locally:
  python main.py
  # Auto-starts ngrok tunnel + FastAPI server
"""

import os
from dotenv import load_dotenv
load_dotenv()  # loads .env before anything else reads os.getenv()

import json
import base64
import uuid
import time
import logging
import pathlib
import glob as glob_mod
import requests as http_requests
from typing import Optional
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape as xml_escape

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Header, Depends
from fastapi.responses import JSONResponse, Response, FileResponse
import uvicorn

from services.agent import run_agent_turn
from services.voice_service import speech_to_text, text_to_speech
from services.language_engine import detect_language_from_text

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s"
)
logger = logging.getLogger("hospital_agent")

# ── Config ────────────────────────────────────────────────────────────────────

APP_ROOT = pathlib.Path(__file__).parent.resolve()
HOSPITALS_CONFIG_PATH = str(APP_ROOT / "config" / "hospitals.json")

# Optional API key to protect webhook endpoints.
# Set AGENT_API_KEY in .env. Leave blank to disable auth (dev only).
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")

MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MB

# ── Audio Cache (for serving TTS audio to Exotel via <Play> URL) ──────────────

AUDIO_CACHE_DIR = APP_ROOT / "audio_cache"
AUDIO_CACHE_DIR.mkdir(exist_ok=True)
AUDIO_CACHE_MAX_AGE_SECONDS = 600  # 10 minutes — auto-delete old files

# ── Ngrok / Public URL ───────────────────────────────────────────────────────
# Set NGROK_URL in .env to use a fixed URL, otherwise auto-start ngrok tunnel.
NGROK_URL = os.getenv("NGROK_URL", "")


def _start_ngrok(port: int) -> str:
    """Start ngrok tunnel and return the public URL."""
    try:
        from pyngrok import ngrok
        tunnel = ngrok.connect(port, "http")
        public_url = tunnel.public_url
        # Force HTTPS
        if public_url.startswith("http://"):
            public_url = public_url.replace("http://", "https://", 1)
        return public_url
    except ImportError:
        logger.warning("pyngrok not installed. Run: pip install pyngrok")
        return ""
    except Exception as e:
        logger.warning(f"Failed to start ngrok: {e}")
        return ""


def get_public_url() -> str:
    """Get the public URL (ngrok or manual)."""
    global NGROK_URL
    if NGROK_URL:
        return NGROK_URL.rstrip("/")
    return f"http://localhost:{os.getenv('PORT', 8000)}"


# ── Audio Cache Helpers ──────────────────────────────────────────────────────

def save_audio_to_cache(audio_bytes: bytes, prefix: str = "resp") -> str:
    """Save audio bytes to cache directory. Returns filename."""
    filename = f"{prefix}_{uuid.uuid4().hex[:10]}.wav"
    filepath = AUDIO_CACHE_DIR / filename
    with open(filepath, "wb") as f:
        f.write(audio_bytes)
    return filename


def get_audio_url(filename: str) -> str:
    """Get the full public URL for a cached audio file."""
    return f"{get_public_url()}/audio/{filename}"


def cleanup_audio_cache():
    """Delete audio files older than AUDIO_CACHE_MAX_AGE_SECONDS."""
    now = time.time()
    count = 0
    for filepath in AUDIO_CACHE_DIR.glob("*.wav"):
        if now - filepath.stat().st_mtime > AUDIO_CACHE_MAX_AGE_SECONDS:
            filepath.unlink(missing_ok=True)
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old audio files from cache")


# ── ExoML Response Builders ──────────────────────────────────────────────────

def exoml_play_and_record(audio_url: str, callback_url: str) -> str:
    """ExoML: Play TTS audio, then record patient's response."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{xml_escape(audio_url)}</Play>
    <Record action="{xml_escape(callback_url)}" maxLength="15" timeout="3" playBeep="false" />
</Response>"""


def exoml_play_and_hangup(audio_url: str) -> str:
    """ExoML: Play final TTS audio, then hang up."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{xml_escape(audio_url)}</Play>
    <Hangup />
</Response>"""


def exoml_say_and_record(text: str, callback_url: str) -> str:
    """ExoML fallback: Use Exotel's built-in TTS (if Sarvam fails)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{xml_escape(text)}</Say>
    <Record action="{xml_escape(callback_url)}" maxLength="15" timeout="3" playBeep="false" />
</Response>"""


def exoml_say_and_hangup(text: str) -> str:
    """ExoML fallback: Say text and hang up."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>{xml_escape(text)}</Say>
    <Hangup />
</Response>"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_hospitals() -> dict:
    with open(HOSPITALS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["hospitals"]


def _make_greeting(hospital: dict, lang: str) -> str:
    agent_name = hospital["agent_name"]
    hospital_name = hospital["hospital_name"]
    greetings = {
        "od-IN": f"Namaskar! {hospital_name} — {agent_name} speaking. Appointment darkara ki?",
        "hi-IN": f"Namaskar! {hospital_name} — {agent_name} bol rahi hoon. Appointment chahiye?",
        "en-IN": f"Hello! {hospital_name}, {agent_name} speaking. How may I help you?"
    }
    return greetings.get(lang, greetings["hi-IN"])


# ── Auth Dependency ───────────────────────────────────────────────────────────

def require_api_key(authorization: str = Header(default="")):
    """Simple bearer-token guard. Skipped when AGENT_API_KEY is not set."""
    if AGENT_API_KEY and authorization != f"Bearer {AGENT_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── In-memory session store (per active call) ─────────────────────────────────
# Structure: { call_id: { "hospital_id": str, "history": list, "lang": str } }
SESSIONS: dict = {}

# ── App Lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cleanup_audio_cache()
    logger.info("=" * 60)
    logger.info("  Hospital Voice Agent Starting")
    logger.info(f"  Loaded {len(load_hospitals())} hospitals from config")
    logger.info(f"  Auth {'ENABLED' if AGENT_API_KEY else 'DISABLED (set AGENT_API_KEY)'}")
    logger.info(f"  Public URL: {get_public_url()}")
    logger.info(f"  Exotel webhook: {get_public_url()}/exotel/incoming?hospital_id=aiims-bbsr-001")
    logger.info("=" * 60)
    yield
    # Shutdown
    logger.info(f"Shutting down. Active sessions: {len(SESSIONS)}")

app = FastAPI(
    title="Hospital Voice Agent",
    description="Trilingual (Hindi/Odia/English) AI receptionist for Indian hospitals",
    version="1.1.0",
    lifespan=lifespan
)

# ══════════════════════════════════════════════════════════════════════════════
#  EXISTING ENDPOINTS (JSON API — for custom integrations / testing)
# ══════════════════════════════════════════════════════════════════════════════

# ── Health Check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    hospitals = load_hospitals()
    return {
        "status": "ok",
        "hospitals_loaded": len(hospitals),
        "active_calls": len(SESSIONS),
        "public_url": get_public_url(),
        "services": {
            "groq": bool(os.getenv("GROQ_API_KEY")),
            "sarvam": bool(os.getenv("SARVAM_API_KEY"))
        }
    }

# ── List Hospitals (Admin) ─────────────────────────────────────────────────────

@app.get("/hospitals")
async def list_hospitals(_: None = Depends(require_api_key)):
    hospitals = load_hospitals()
    return {
        "count": len(hospitals),
        "hospitals": [
            {
                "id": h["hospital_id"],
                "name": h["hospital_name"],
                "agent_name": h["agent_name"],
                "primary_lang": h["primary_lang"],
                "doctor_count": len(h.get("doctors", []))
            }
            for h in hospitals.values()
        ]
    }

# ── Incoming Call Handler (JSON API) ──────────────────────────────────────────

@app.post("/call/start")
async def call_start(
    request: Request,
    _: None = Depends(require_api_key)
):
    body = await request.json()
    call_id = str(uuid.uuid4())
    hospital_id = body.get("hospital_id", "aiims-bbsr-001")
    caller_phone = body.get("caller_phone", "")

    hospitals = load_hospitals()
    hospital = hospitals.get(hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital '{hospital_id}' not found in config")

    lang = "od-IN" if hospital["primary_lang"] == "odia" else "hi-IN"
    SESSIONS[call_id] = {
        "hospital_id": hospital_id,
        "hospital": hospital,
        "history": [],
        "lang": lang,
        "caller_phone": caller_phone
    }

    logger.info(f"[{call_id}] New call for {hospital['hospital_name']} from {caller_phone or 'unknown'}")

    greeting_text = _make_greeting(hospital, lang)
    tts_result = text_to_speech(greeting_text, lang)
    SESSIONS[call_id]["history"].append({"role": "assistant", "content": greeting_text})

    return JSONResponse(content={
        "call_id": call_id,
        "text": greeting_text,
        "lang": lang,
        "audio_b64": base64.b64encode(tts_result["audio_bytes"]).decode() if tts_result["success"] else None
    })


# ── Patient Speech Input Handler (JSON API) ──────────────────────────────────

@app.post("/call/input")
async def call_input(
    call_id: str = Form(...),
    audio_file: Optional[UploadFile] = File(None),
    text_input: Optional[str] = Form(None),
    _: None = Depends(require_api_key)
):
    session = SESSIONS.get(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    hospital = session["hospital"]
    history = session["history"]
    current_lang = session["lang"]

    if text_input:
        patient_text = text_input
    elif audio_file:
        audio_bytes = await audio_file.read()
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Audio file too large (max 10MB)")
        stt_result = speech_to_text(audio_bytes, current_lang)
        if not stt_result["success"]:
            error_text = "Aapki awaaz nahi suni. Phir se bolein." if current_lang == "hi-IN" else "Apana katha sunagala nahi. Pheri boli."
            tts = text_to_speech(error_text, current_lang)
            return JSONResponse(content={
                "text": error_text,
                "audio_b64": base64.b64encode(tts["audio_bytes"]).decode() if tts["success"] else None,
                "lang": current_lang,
                "appointment_booked": False,
                "end_call": False
            })
        patient_text = stt_result["transcript"]
    else:
        raise HTTPException(status_code=400, detail="Provide either audio_file or text_input")

    if not patient_text.strip():
        silence_text = "Kuch suna nahi. Phir se bolein Ji." if current_lang == "hi-IN" else "Kichhi sunagala nahi Agya."
        tts = text_to_speech(silence_text, current_lang)
        return JSONResponse(content={
            "text": silence_text,
            "audio_b64": base64.b64encode(tts["audio_bytes"]).decode() if tts["success"] else None,
            "lang": current_lang,
            "appointment_booked": False,
            "end_call": False
        })

    detected_lang = detect_language_from_text(patient_text, hospital.get("primary_lang", "hindi"))
    session["lang"] = detected_lang

    logger.info(f"[{call_id}] Patient said ({detected_lang}): {patient_text[:80]}")

    result = run_agent_turn(
        user_message=patient_text,
        conversation_history=history,
        hospital_config=hospital
    )

    response_text = result["response_text"]
    appointment_booked = result["appointment_booked"]

    logger.info(f"[{call_id}] Agent replied: {response_text[:80]} | booked={appointment_booked}")

    tts_result = text_to_speech(response_text, result["detected_lang"])

    end_keywords = ["dhanyabad", "shukriya", "thank you", "goodbye", "bye", "ok done", "confirmed"]
    end_call = appointment_booked or any(kw in response_text.lower() for kw in end_keywords)

    if end_call and call_id in SESSIONS:
        logger.info(f"[{call_id}] Call ended. Cleaning up session.")
        del SESSIONS[call_id]

    return JSONResponse(content={
        "patient_said": patient_text,
        "text": response_text,
        "audio_b64": base64.b64encode(tts_result["audio_bytes"]).decode() if tts_result["success"] else None,
        "lang": result["detected_lang"],
        "appointment_booked": appointment_booked,
        "booking_details": result.get("booking_result"),
        "end_call": end_call
    })


# ── Test Endpoint (No Audio — Pure Text) ──────────────────────────────────────

@app.post("/test/chat")
async def test_chat(request: Request):
    body = await request.json()
    hospital_id = body.get("hospital_id", "aiims-bbsr-001")
    call_id = body.get("call_id", "test-" + str(uuid.uuid4())[:8])
    message = body.get("message", "")

    hospitals = load_hospitals()
    hospital = hospitals.get(hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital '{hospital_id}' not found")

    if call_id not in SESSIONS:
        SESSIONS[call_id] = {
            "hospital_id": hospital_id,
            "hospital": hospital,
            "history": [],
            "lang": "od-IN" if hospital["primary_lang"] == "odia" else "hi-IN",
            "caller_phone": "test"
        }

    session = SESSIONS[call_id]

    result = run_agent_turn(
        user_message=message,
        conversation_history=session["history"],
        hospital_config=hospital
    )

    return JSONResponse(content={
        "call_id": call_id,
        "patient_said": message,
        "agent_replied": result["response_text"],
        "detected_language": result["detected_lang"],
        "appointment_booked": result["appointment_booked"],
        "booking_details": result.get("booking_result"),
        "history_length": len(session["history"])
    })


# ══════════════════════════════════════════════════════════════════════════════
#  EXOTEL TELEPHONY ENDPOINTS (real phone calls via ExoML)
# ══════════════════════════════════════════════════════════════════════════════

# ── Serve Cached TTS Audio Files ─────────────────────────────────────────────

@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    """Serve TTS audio files for Exotel's <Play> tag."""
    # Sanitize filename to prevent path traversal
    safe_name = pathlib.Path(filename).name
    filepath = AUDIO_CACHE_DIR / safe_name
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(str(filepath), media_type="audio/wav")


# ── Exotel: New Incoming Call ────────────────────────────────────────────────

@app.post("/exotel/incoming")
async def exotel_incoming(request: Request):
    """
    Exotel Passthru Applet webhook — called when a new call arrives.

    Configure in Exotel dashboard:
      Passthru URL: https://<ngrok-url>/exotel/incoming?hospital_id=aiims-bbsr-001

    Exotel sends form-encoded params: CallSid, CallFrom, CallTo, Direction, etc.
    We respond with ExoML: Play greeting audio + Record patient speech.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid", uuid.uuid4()))
    caller_phone = str(form.get("CallFrom", ""))
    hospital_id = request.query_params.get("hospital_id", "aiims-bbsr-001")

    hospitals = load_hospitals()
    hospital = hospitals.get(hospital_id)
    if not hospital:
        # Fallback to first hospital
        hospital_id = list(hospitals.keys())[0]
        hospital = hospitals[hospital_id]

    # Create session keyed by Exotel's CallSid
    lang = "od-IN" if hospital["primary_lang"] == "odia" else "hi-IN"
    SESSIONS[call_sid] = {
        "hospital_id": hospital_id,
        "hospital": hospital,
        "history": [],
        "lang": lang,
        "caller_phone": caller_phone
    }

    logger.info(f"[EXOTEL {call_sid}] New call for {hospital['hospital_name']} from {caller_phone}")

    # Generate greeting
    greeting_text = _make_greeting(hospital, lang)
    tts_result = text_to_speech(greeting_text, lang)
    SESSIONS[call_sid]["history"].append({"role": "assistant", "content": greeting_text})

    callback_url = f"{get_public_url()}/exotel/handle-speech?call_sid={call_sid}"

    if tts_result["success"]:
        filename = save_audio_to_cache(tts_result["audio_bytes"], "greet")
        audio_url = get_audio_url(filename)
        exoml = exoml_play_and_record(audio_url, callback_url)
    else:
        exoml = exoml_say_and_record(greeting_text, callback_url)

    logger.info(f"[EXOTEL {call_sid}] Greeting sent, waiting for patient speech")
    return Response(content=exoml, media_type="application/xml")


# ── Exotel: Handle Patient Speech (the main conversation loop) ───────────────

@app.post("/exotel/handle-speech")
async def exotel_handle_speech(request: Request):
    """
    Exotel recording callback — called after patient finishes speaking.

    Flow: Download recording → Sarvam STT → Groq LLM → Sarvam TTS → ExoML response.
    This endpoint loops: each ExoML response includes another <Record> tag,
    so Exotel calls this endpoint again after the next speech turn.
    """
    form = await request.form()
    call_sid = request.query_params.get("call_sid", str(form.get("CallSid", "")))
    recording_url = str(form.get("RecordingUrl", ""))

    session = SESSIONS.get(call_sid)
    if not session:
        logger.warning(f"[EXOTEL {call_sid}] Session not found, hanging up")
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup /></Response>',
            media_type="application/xml"
        )

    hospital = session["hospital"]
    history = session["history"]
    current_lang = session["lang"]
    callback_url = f"{get_public_url()}/exotel/handle-speech?call_sid={call_sid}"

    # ── Step 1: Download recording from Exotel ────────────────────────────
    patient_text = ""
    if recording_url:
        try:
            logger.info(f"[EXOTEL {call_sid}] Downloading recording: {recording_url[:80]}")
            audio_response = http_requests.get(recording_url, timeout=15)
            audio_response.raise_for_status()
            audio_bytes = audio_response.content

            if len(audio_bytes) < 500:
                logger.info(f"[EXOTEL {call_sid}] Recording too short ({len(audio_bytes)} bytes), likely silence")
            else:
                # Detect format from URL or content-type
                content_type = audio_response.headers.get("Content-Type", "audio/wav")
                audio_format = "mp3" if "mp3" in recording_url.lower() or "mp3" in content_type else "wav"

                stt_result = speech_to_text(audio_bytes, current_lang, audio_format=audio_format)
                if stt_result["success"]:
                    patient_text = stt_result["transcript"]
                else:
                    logger.warning(f"[EXOTEL {call_sid}] STT failed: {stt_result.get('error')}")
        except Exception as e:
            logger.error(f"[EXOTEL {call_sid}] Failed to process recording: {e}")

    # ── If no speech detected, ask patient to repeat ──────────────────────
    if not patient_text.strip():
        retry_texts = {
            "od-IN": "Kichhi sunagala nahi Agya. Pheri boli pariba ki?",
            "hi-IN": "Aapki awaaz nahi suni Ji. Phir se bolein?",
            "en-IN": "Sorry, I couldn't hear you. Could you repeat?"
        }
        retry_text = retry_texts.get(current_lang, retry_texts["hi-IN"])
        tts = text_to_speech(retry_text, current_lang)

        if tts["success"]:
            filename = save_audio_to_cache(tts["audio_bytes"], "retry")
            return Response(
                content=exoml_play_and_record(get_audio_url(filename), callback_url),
                media_type="application/xml"
            )
        return Response(
            content=exoml_say_and_record(retry_text, callback_url),
            media_type="application/xml"
        )

    # ── Step 2: Detect language ───────────────────────────────────────────
    detected_lang = detect_language_from_text(patient_text, hospital.get("primary_lang", "hindi"))
    session["lang"] = detected_lang

    logger.info(f"[EXOTEL {call_sid}] Patient said ({detected_lang}): {patient_text[:80]}")

    # ── Step 3: Run LLM agent turn ────────────────────────────────────────
    result = run_agent_turn(
        user_message=patient_text,
        conversation_history=history,
        hospital_config=hospital
    )

    response_text = result["response_text"]
    appointment_booked = result["appointment_booked"]

    logger.info(f"[EXOTEL {call_sid}] Agent: {response_text[:80]} | booked={appointment_booked}")

    # ── Step 4: Generate TTS audio ────────────────────────────────────────
    tts_result = text_to_speech(response_text, result["detected_lang"])

    # ── Step 5: Check if call should end ──────────────────────────────────
    end_keywords = ["dhanyabad", "shukriya", "thank you", "goodbye", "bye", "ok done", "confirmed"]
    end_call = appointment_booked or any(kw in response_text.lower() for kw in end_keywords)

    if end_call:
        # Clean up session
        if call_sid in SESSIONS:
            logger.info(f"[EXOTEL {call_sid}] Call complete. Cleaning up.")
            del SESSIONS[call_sid]

        if tts_result["success"]:
            filename = save_audio_to_cache(tts_result["audio_bytes"], "final")
            return Response(
                content=exoml_play_and_hangup(get_audio_url(filename)),
                media_type="application/xml"
            )
        return Response(
            content=exoml_say_and_hangup(response_text),
            media_type="application/xml"
        )

    # ── Step 6: Continue conversation — play response + record next turn ──
    if tts_result["success"]:
        filename = save_audio_to_cache(tts_result["audio_bytes"], "resp")
        return Response(
            content=exoml_play_and_record(get_audio_url(filename), callback_url),
            media_type="application/xml"
        )
    return Response(
        content=exoml_say_and_record(response_text, callback_url),
        media_type="application/xml"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT — Auto-starts ngrok tunnel
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))

    # Auto-start ngrok if NGROK_URL not set in .env
    if not NGROK_URL:
        logger.info("NGROK_URL not set — starting ngrok tunnel...")
        detected_url = _start_ngrok(port)
        if detected_url:
            # Set as env var so uvicorn's module reimport picks it up
            os.environ["NGROK_URL"] = detected_url
            logger.info(f"ngrok tunnel active: {detected_url}")
            logger.info(f"")
            logger.info(f"  Configure Exotel Passthru Applet URL:")
            logger.info(f"    {detected_url}/exotel/incoming?hospital_id=aiims-bbsr-001")
            logger.info(f"")
        else:
            logger.warning("ngrok failed to start. Set NGROK_URL manually in .env")
    else:
        logger.info(f"Using manual NGROK_URL: {NGROK_URL}")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )
