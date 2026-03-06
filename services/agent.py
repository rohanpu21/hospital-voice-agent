"""
Groq LLM Orchestrator — The "Brain"
Uses Llama-3-70B with Tool Use for hospital appointment booking.

Cost: Free (limited) or ~$0.60 per 1M tokens via Groq.
Groq is used for sub-second inference — critical for voice conversations.
"""

import os
import json
import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from groq import Groq

from services.calendar_service import get_available_slots, book_appointment
from services.language_engine import detect_language_from_text, get_confirmation_message

IST = ZoneInfo("Asia/Kolkata")
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
MODEL = "llama-3.3-70b-versatile"  # Current Groq model with tool-use support


# --- Tool Definitions (sent to Groq so the LLM knows what it can call) ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_available_slots",
            "description": (
                "Check available appointment slots for a doctor on a given date. "
                "Always call this BEFORE confirming a time to the patient."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format (e.g. '2024-10-27')"
                    },
                    "doctor_id": {
                        "type": "string",
                        "description": "Doctor ID from the hospital config"
                    }
                },
                "required": ["date_str", "doctor_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": (
                "Book a confirmed appointment after the patient agrees to a slot. "
                "Only call this after get_available_slots confirms the slot is free."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {
                        "type": "string",
                        "description": "Full name of the patient"
                    },
                    "patient_phone": {
                        "type": "string",
                        "description": "Patient's phone number (optional)"
                    },
                    "doctor_id": {
                        "type": "string",
                        "description": "Doctor ID from hospital config"
                    },
                    "start_iso": {
                        "type": "string",
                        "description": "ISO format datetime e.g. '2024-10-27T10:00:00+05:30'"
                    }
                },
                "required": ["patient_name", "doctor_id", "start_iso"]
            }
        }
    }
]


def build_system_prompt(hospital_config: dict) -> str:
    """Build the dynamic system prompt from the hospital's config.json entry."""
    doctor_list = "\n".join(
        f"  - {d['name']} ({d['department']}) — ID: {d['id']}"
        for d in hospital_config.get("doctors", [])
    )

    wh = hospital_config.get("working_hours", {})
    working_hours = f"{wh.get('start', '09:00')} to {wh.get('end', '17:00')}"

    return f"""You are {hospital_config['agent_name']}, the AI receptionist for {hospital_config['hospital_name']} in Bhubaneswar.

Your ONLY job is to book doctor appointments using the tools provided. Nothing else.

LINGUISTIC RULES:
- Mirror the patient's language EXACTLY. Odia gets Odia reply. Hindi gets Hindi. Mixed gets mixed.
- Use 'Agya' at the end of Odia sentences. Use 'Ji' for Hindi. Be warm and respectful.
- Keep every response UNDER 15 words to minimize call time (and API cost).
- Never say you are an AI. You are {hospital_config['agent_name']}, a human receptionist.

AVAILABLE DOCTORS:
{doctor_list}

WORKING HOURS: {working_hours} IST (Monday to Saturday only)
TODAY: {datetime.datetime.now(IST).strftime("%A, %d %B %Y")}

STRICT BOOKING STEPS — follow in order, never skip:

STEP 1 — COLLECT NAME: Ask patient's full name. Do NOT proceed until you have it.
STEP 2 — COLLECT DOCTOR: Ask which doctor/department. Do NOT proceed until you have it.
STEP 3 — COLLECT DATE: Ask preferred date. Convert 'kal'=tomorrow ({(datetime.datetime.now(IST) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")}), 'aaj'=today ({datetime.datetime.now(IST).strftime("%Y-%m-%d")}). Do NOT proceed until you have it.
STEP 4 — CHECK SLOTS: ONLY after you have name+doctor+date, CALL get_available_slots tool.
STEP 5 — OFFER SLOTS: Tell patient the 2 available times from tool result. Wait for choice.
STEP 6 — BOOK: Patient picks a time. CALL book_appointment tool using the iso_start from tool result.
STEP 7 — CONFIRM: Read back name, doctor, date, time only if tool returned success=true.

GATE CONDITIONS (DO NOT violate):
- DO NOT call get_available_slots without a confirmed date in YYYY-MM-DD format.
- DO NOT call book_appointment without confirmed patient_name, doctor_id, AND iso_start from get_available_slots result.
- DO NOT say "confirmed" unless book_appointment tool returned success=true.

OTHER:
- Emergency: 'Emergency ward mein jaiye turant.'
- Fee question: 'Front desk pe poochh lijiye Ji.'"""


def run_agent_turn(
    user_message: str,
    conversation_history: list,
    hospital_config: dict
) -> dict:
    """
    Process one turn of conversation.

    Args:
        user_message: Transcript from Sarvam STT
        conversation_history: Full conversation so far (list of role/content dicts)
        hospital_config: The hospital's entry from hospitals.json

    Returns:
        {
            "response_text": str,      # Text for TTS
            "detected_lang": str,      # 'od-IN' / 'hi-IN' / 'en-IN'
            "appointment_booked": bool,
            "booking_result": dict | None
        }
    """
    if not GROQ_KEY:
        return {
            "response_text": "System error. Please call back later.",
            "detected_lang": "hi-IN",
            "appointment_booked": False,
            "booking_result": None
        }

    client = Groq(api_key=GROQ_KEY)

    # Detect language for TTS response
    detected_lang = detect_language_from_text(
        user_message,
        hospital_config.get("primary_lang", "hindi")
    )

    # Build messages
    system_prompt = build_system_prompt(hospital_config)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    booking_result = None
    appointment_booked = False

    # --- Agentic loop: LLM may call tools multiple times in one turn ---
    for _ in range(5):  # Max 5 tool calls per turn (safety limit)
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,      # Low temp = consistent, professional tone
            max_tokens=150        # Keep responses SHORT (voice latency)
        )

        choice = response.choices[0]

        # If LLM wants to call a tool
        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            # Add assistant message with tool calls to history
            messages.append({
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in choice.message.tool_calls
                ]
            })

            # Execute each tool call
            for tool_call in choice.message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                tool_result = _execute_tool(tool_name, tool_args, hospital_config)

                if tool_name == "book_appointment" and tool_result.get("success"):
                    appointment_booked = True
                    booking_result = tool_result

                # Feed tool result back to LLM
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result)
                })

        else:
            # LLM gave a final text response — done
            response_text = choice.message.content or "Koi samasya aayi. Dobara call karein."
            # Update conversation history for next turn
            conversation_history.append({"role": "user", "content": user_message})
            conversation_history.append({"role": "assistant", "content": response_text})

            return {
                "response_text": response_text,
                "detected_lang": detected_lang,
                "appointment_booked": appointment_booked,
                "booking_result": booking_result
            }

    # Fallback if loop exhausted
    return {
        "response_text": "Maafi chahta hoon, please dobara bolein.",
        "detected_lang": detected_lang,
        "appointment_booked": False,
        "booking_result": None
    }


def _execute_tool(tool_name: str, args: dict, hospital_config: dict) -> dict:
    """Route tool calls from the LLM to the actual Python functions."""

    if tool_name == "get_available_slots":
        # Find the doctor's calendar email from config
        doctor = _find_doctor(args["doctor_id"], hospital_config)
        if not doctor:
            return {"error": f"Doctor ID {args['doctor_id']} not found in config."}

        slots = get_available_slots(
            credentials_file=hospital_config["credentials_file"],
            calendar_id=doctor["calendar_email"],
            date_str=args["date_str"],
            working_start=hospital_config["working_hours"]["start"],
            working_end=hospital_config["working_hours"]["end"],
            slot_duration=hospital_config.get("slot_duration_minutes", 30)
        )

        if not slots:
            return {"available_slots": [], "message": "No slots available on this date."}

        # Include iso_start so LLM can directly pass it to book_appointment
        return {
            "available_slots": [
                {"display": s["start"], "iso_start": s["iso_start"]}
                for s in slots[:3]
            ],
            "instruction": "Offer these slots to the patient. When they pick one, call book_appointment using the iso_start value exactly as shown."
        }

    elif tool_name == "book_appointment":
        doctor = _find_doctor(args["doctor_id"], hospital_config)
        if not doctor:
            return {"success": False, "error": f"Doctor ID {args['doctor_id']} not found."}

        result = book_appointment(
            credentials_file=hospital_config["credentials_file"],
            calendar_id=doctor["calendar_email"],
            patient_name=args["patient_name"],
            doctor_name=doctor["name"],
            doctor_email=doctor["calendar_email"],
            start_iso=args["start_iso"],
            duration_minutes=hospital_config.get("slot_duration_minutes", 30),
            patient_phone=args.get("patient_phone")
        )
        return result

    return {"error": f"Unknown tool: {tool_name}"}


def _find_doctor(doctor_id: str, hospital_config: dict) -> Optional[dict]:
    """Find a doctor by ID in the hospital config. Returns None if not found."""
    for doc in hospital_config.get("doctors", []):
        if doc["id"] == doctor_id:
            return doc
    return None
