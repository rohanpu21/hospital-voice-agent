"""
Hospital Voice Agent — FastAPI Application
==========================================
Exposes webhook endpoints for telephony providers (Exotel / Plivo / CloudBharat SIP).

Endpoints:
  POST /call/start          — Incoming call handler (returns greeting audio)
  POST /call/input          — Patient speech input handler
  GET  /health              — Health check
  GET  /hospitals           — List configured hospitals (admin)

Session State:
  In-memory dict (per call_id) — good enough for MVP.
  For production, swap with Redis.

Run locally:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
from dotenv import load_dotenv
load_dotenv()  # loads .env before anything else reads os.getenv()

import json
import base64
import uuid
import logging
import pathlib
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Header, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
import uvicorn

from services.agent import run_agent_turn
from services.voice_service import speech_to_text, text_to_speech
from services.language_engine import detect_language_from_text
from services.remote_control import (
    connect_admin, disconnect_admin, broadcast,
    set_injection, pop_injection, active_admin_count
)

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


def load_hospitals() -> dict:
    with open(HOSPITALS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["hospitals"]


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
    logger.info("=" * 60)
    logger.info("  Hospital Voice Agent Starting")
    logger.info(f"  Loaded {len(load_hospitals())} hospitals from config")
    logger.info(f"  Auth {'ENABLED' if AGENT_API_KEY else 'DISABLED (set AGENT_API_KEY)'}")
    logger.info("=" * 60)
    yield
    logger.info(f"Shutting down. Active sessions: {len(SESSIONS)}")

app = FastAPI(
    title="Hospital Voice Agent",
    description="Trilingual (Hindi/Odia/English) AI receptionist for Indian hospitals",
    version="1.0.0",
    lifespan=lifespan
)

# ── Health Check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    hospitals = load_hospitals()
    return {
        "status": "ok",
        "hospitals_loaded": len(hospitals),
        "active_calls": len(SESSIONS),
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

# ── Incoming Call Handler ─────────────────────────────────────────────────────

@app.post("/call/start")
async def call_start(
    request: Request,
    _: None = Depends(require_api_key)
):
    """
    Called by telephony provider when a new call comes in.

    Expected body (JSON):
      {
        "hospital_id": "aiims-bbsr-001",   ← from your SIP routing rules
        "caller_phone": "+919876543210"
      }

    Returns:
      { "call_id": "...", "audio_b64": "...", "text": "greeting text", "lang": "od-IN" }
    """
    body = await request.json()
    # call_id is always generated server-side — never trust client
    call_id = str(uuid.uuid4())
    hospital_id = body.get("hospital_id", "aiims-bbsr-001")
    caller_phone = body.get("caller_phone", "")

    hospitals = load_hospitals()
    hospital = hospitals.get(hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital '{hospital_id}' not found in config")

    # Create session
    lang = "od-IN" if hospital["primary_lang"] == "odia" else "hi-IN"
    SESSIONS[call_id] = {
        "hospital_id": hospital_id,
        "hospital": hospital,
        "history": [],
        "lang": lang,
        "caller_phone": caller_phone
    }

    logger.info(f"[{call_id}] New call for {hospital['hospital_name']} from {caller_phone or 'unknown'}")
    await broadcast("session_started", call_id, {
        "hospital_id": hospital_id,
        "hospital_name": hospital["hospital_name"],
        "caller_phone": caller_phone,
        "lang": lang
    })

    # Generate greeting in hospital's primary language
    agent_name = hospital["agent_name"]
    hospital_name = hospital["hospital_name"]

    greetings = {
        "od-IN": f"Namaskar! {hospital_name} — {agent_name} speaking. Appointment darkara ki?",
        "hi-IN": f"Namaskar! {hospital_name} — {agent_name} bol rahi hoon. Appointment chahiye?",
        "en-IN": f"Hello! {hospital_name}, {agent_name} speaking. How may I help you?"
    }
    greeting_text = greetings.get(lang, greetings["hi-IN"])

    # Convert to audio
    tts_result = text_to_speech(greeting_text, lang)

    SESSIONS[call_id]["history"].append({"role": "assistant", "content": greeting_text})

    return JSONResponse(content={
        "call_id": call_id,
        "text": greeting_text,
        "lang": lang,
        "audio_b64": base64.b64encode(tts_result["audio_bytes"]).decode() if tts_result["success"] else None
    })


# ── Patient Speech Input Handler ───────────────────────────────────────────────

@app.post("/call/input")
async def call_input(
    call_id: str = Form(...),
    audio_file: Optional[UploadFile] = File(None),
    text_input: Optional[str] = Form(None),
    _: None = Depends(require_api_key)
):
    """
    Called each time the patient finishes speaking.

    Accepts either:
      - audio_file: WAV audio bytes from telephony provider
      - text_input: Plain text (for testing)

    Returns:
      {
        "text": "agent response",
        "audio_b64": "base64 WAV",
        "lang": "od-IN",
        "appointment_booked": false,
        "end_call": false
      }
    """
    session = SESSIONS.get(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    hospital = session["hospital"]
    history = session["history"]
    current_lang = session["lang"]

    # ── Step 1: Get patient's text (from audio or direct) ─────────────────
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

    # ── Step 2: Detect language from what patient said ──────────────────────
    detected_lang = detect_language_from_text(patient_text, hospital.get("primary_lang", "hindi"))
    session["lang"] = detected_lang

    logger.info(f"[{call_id}] Patient said ({detected_lang}): {patient_text[:80]}")

    # ── Step 3: Check for pending supervisor injection ──────────────────────
    supervisor_note = pop_injection(call_id)

    # ── Step 4: Run the LLM agent turn ─────────────────────────────────────
    result = run_agent_turn(
        user_message=patient_text,
        conversation_history=history,
        hospital_config=hospital,
        supervisor_note=supervisor_note
    )

    response_text = result["response_text"]
    appointment_booked = result["appointment_booked"]

    logger.info(f"[{call_id}] Agent replied: {response_text[:80]} | booked={appointment_booked}")

    # ── Step 5: Broadcast transcript update to admin watchers ───────────────
    await broadcast("transcript_update", call_id, {
        "patient_said": patient_text,
        "agent_replied": response_text,
        "lang": result["detected_lang"],
        "appointment_booked": appointment_booked,
        "supervisor_note_used": supervisor_note or None
    })

    if appointment_booked:
        await broadcast("appointment_booked", call_id, result.get("booking_result") or {})

    # ── Step 6: Convert response to audio ────────────────────────────────────
    tts_result = text_to_speech(response_text, result["detected_lang"])

    # ── Step 7: Detect end-of-call signals ─────────────────────────────────
    end_keywords = ["dhanyabad", "shukriya", "thank you", "goodbye", "bye", "ok done", "confirmed"]
    end_call = appointment_booked or any(kw in response_text.lower() for kw in end_keywords)

    if end_call and call_id in SESSIONS:
        logger.info(f"[{call_id}] Call ended. Cleaning up session.")
        del SESSIONS[call_id]
        await broadcast("call_ended", call_id, {"reason": "natural"})

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
    """
    Text-only endpoint for testing without a real phone call.
    Use this to verify the LLM and Calendar integration work correctly.

    Body: { "hospital_id": "aiims-bbsr-001", "call_id": "test-123", "message": "mote appointment darkara" }
    """
    body = await request.json()
    hospital_id = body.get("hospital_id", "aiims-bbsr-001")
    call_id = body.get("call_id", "test-" + str(uuid.uuid4())[:8])
    message = body.get("message", "")

    hospitals = load_hospitals()
    hospital = hospitals.get(hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital '{hospital_id}' not found")

    # Auto-create session if doesn't exist
    if call_id not in SESSIONS:
        SESSIONS[call_id] = {
            "hospital_id": hospital_id,
            "hospital": hospital,
            "history": [],
            "lang": "od-IN" if hospital["primary_lang"] == "odia" else "hi-IN",
            "caller_phone": "test"
        }

    session = SESSIONS[call_id]

    supervisor_note = pop_injection(call_id)

    result = run_agent_turn(
        user_message=message,
        conversation_history=session["history"],
        hospital_config=hospital,
        supervisor_note=supervisor_note
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


# ── Admin / Remote Control ────────────────────────────────────────────────────
#
# All admin endpoints require the same AGENT_API_KEY bearer token.
# WebSocket /admin/ws also requires the key (passed as ?api_key=<key> query param
# since browsers cannot set custom headers on WebSocket upgrades).

VALID_LANGS = {"od-IN", "hi-IN", "en-IN"}


def _require_admin_key(authorization: str = Header(default="")):
    """Re-use the same bearer-token guard for admin routes."""
    if AGENT_API_KEY and authorization != f"Bearer {AGENT_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.websocket("/admin/ws")
async def admin_ws(ws: WebSocket, api_key: str = ""):
    """
    Real-time event stream for admin dashboards.
    Connect with:  ws://host/admin/ws?api_key=<AGENT_API_KEY>

    Events pushed (JSON):
      session_started, transcript_update, appointment_booked,
      call_ended, language_changed, supervisor_injected, session_list
    """
    if AGENT_API_KEY and api_key != AGENT_API_KEY:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await connect_admin(ws)

    # Send current session snapshot immediately on connect
    sessions_snapshot = {
        cid: {
            "hospital_id": s["hospital_id"],
            "hospital_name": s["hospital"]["hospital_name"],
            "lang": s["lang"],
            "caller_phone": s.get("caller_phone", ""),
            "history_turns": len(s["history"]) // 2
        }
        for cid, s in SESSIONS.items()
    }
    await broadcast("session_list", "", {"sessions": sessions_snapshot})

    try:
        while True:
            # Keep connection alive; admins only receive — they don't send data
            await ws.receive_text()
    except WebSocketDisconnect:
        disconnect_admin(ws)


@app.get("/admin/sessions")
async def admin_list_sessions(_: None = Depends(_require_admin_key)):
    """List all currently active call sessions."""
    return {
        "active_calls": len(SESSIONS),
        "admin_watchers": active_admin_count(),
        "sessions": [
            {
                "call_id": cid,
                "hospital_id": s["hospital_id"],
                "hospital_name": s["hospital"]["hospital_name"],
                "caller_phone": s.get("caller_phone", ""),
                "lang": s["lang"],
                "history_turns": len(s["history"]) // 2
            }
            for cid, s in SESSIONS.items()
        ]
    }


@app.get("/admin/sessions/{call_id}")
async def admin_get_session(call_id: str, _: None = Depends(_require_admin_key)):
    """Get the full transcript and state of a specific call."""
    session = SESSIONS.get(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "call_id": call_id,
        "hospital_id": session["hospital_id"],
        "hospital_name": session["hospital"]["hospital_name"],
        "caller_phone": session.get("caller_phone", ""),
        "lang": session["lang"],
        "history": session["history"]
    }


@app.post("/admin/sessions/{call_id}/inject")
async def admin_inject(call_id: str, request: Request, _: None = Depends(_require_admin_key)):
    """
    Inject a supervisor note into the next agent turn for a live call.

    Body: { "note": "The patient is a VIP — offer earliest slot." }

    The note is prepended to the LLM system prompt on the very next patient turn,
    then discarded. Use this to guide the agent mid-call without hanging up.
    """
    if call_id not in SESSIONS:
        raise HTTPException(status_code=404, detail="Session not found")

    body = await request.json()
    note = body.get("note", "").strip()
    if not note:
        raise HTTPException(status_code=422, detail="'note' field is required and cannot be empty")

    set_injection(call_id, note)
    await broadcast("supervisor_injected", call_id, {"note": note})
    logger.info(f"[{call_id}] Supervisor injected: {note[:120]}")

    return {"status": "queued", "call_id": call_id, "note": note}


@app.delete("/admin/sessions/{call_id}")
async def admin_terminate_session(call_id: str, _: None = Depends(_require_admin_key)):
    """
    Force-terminate an active call session.
    The telephony provider will detect end_call=true on the next /call/input response
    (which the agent will now return immediately) or this can be used for cleanup.
    """
    session = SESSIONS.pop(call_id, None)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await broadcast("call_ended", call_id, {"reason": "admin_terminated"})
    logger.info(f"[{call_id}] Session force-terminated by admin.")

    return {"status": "terminated", "call_id": call_id}


@app.post("/admin/sessions/{call_id}/set-lang")
async def admin_set_language(call_id: str, request: Request, _: None = Depends(_require_admin_key)):
    """
    Override the TTS/STT language for a live call.

    Body: { "lang": "hi-IN" }   # od-IN | hi-IN | en-IN
    """
    session = SESSIONS.get(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    body = await request.json()
    lang = body.get("lang", "").strip()
    if lang not in VALID_LANGS:
        raise HTTPException(status_code=422, detail=f"'lang' must be one of {sorted(VALID_LANGS)}")

    old_lang = session["lang"]
    session["lang"] = lang
    await broadcast("language_changed", call_id, {"from": old_lang, "to": lang})
    logger.info(f"[{call_id}] Language changed {old_lang} → {lang} by admin.")

    return {"status": "updated", "call_id": call_id, "lang": lang}


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=False,
        log_level="info"
    )
