"""
Remote Control Service
======================
Provides real-time admin visibility and control over active call sessions.

Capabilities:
  - WebSocket broadcast: every call event is pushed to connected admins
  - Inject: supervisor can insert a note/override that the LLM reads on the next turn
  - Terminate: force-end any active call session
  - Language override: change TTS/STT language mid-call

Event schema (sent over WebSocket):
  {
    "event":    "session_started" | "transcript_update" | "appointment_booked"
                | "call_ended" | "language_changed" | "supervisor_injected"
                | "session_list",
    "call_id":  "...",
    "payload":  { ... }   # event-specific data
  }
"""

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("remote_control")

# ── WebSocket connection registry ─────────────────────────────────────────────
# Each admin client that connects to /admin/ws gets added here.
# Multiple admins can watch simultaneously.
_admin_sockets: list[WebSocket] = []


async def connect_admin(ws: WebSocket) -> None:
    """Register a new admin WebSocket connection."""
    await ws.accept()
    _admin_sockets.append(ws)
    logger.info(f"Admin connected. Total watchers: {len(_admin_sockets)}")


def disconnect_admin(ws: WebSocket) -> None:
    """Unregister a closed admin WebSocket connection."""
    if ws in _admin_sockets:
        _admin_sockets.remove(ws)
    logger.info(f"Admin disconnected. Total watchers: {len(_admin_sockets)}")


async def broadcast(event: str, call_id: str, payload: dict) -> None:
    """
    Push an event to all connected admin WebSocket clients.
    Dead sockets are silently pruned.
    """
    if not _admin_sockets:
        return

    message = json.dumps({
        "event": event,
        "call_id": call_id,
        "ts": time.time(),
        "payload": payload
    })

    dead: list[WebSocket] = []
    for ws in list(_admin_sockets):
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)

    for ws in dead:
        disconnect_admin(ws)


# ── Supervisor injection store ────────────────────────────────────────────────
# Maps call_id → pending supervisor note.
# Consumed (and cleared) the next time run_agent_turn is called for that call.
_pending_injections: dict[str, str] = {}


def set_injection(call_id: str, note: str) -> None:
    """Store a supervisor note to be picked up on the next agent turn."""
    _pending_injections[call_id] = note


def pop_injection(call_id: str) -> Optional[str]:
    """Return and clear the pending supervisor injection for a call, if any."""
    return _pending_injections.pop(call_id, None)


def active_admin_count() -> int:
    return len(_admin_sockets)
