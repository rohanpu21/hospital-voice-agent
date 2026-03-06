"""
Session Store Utility
=====================
Simple in-memory session store for MVP.
Each active phone call gets a session keyed by call_id.

To upgrade for production:
  Replace with Redis using: pip install redis
  session = redis_client.get(call_id)
  redis_client.setex(call_id, 1800, json.dumps(session))  # 30-min TTL
"""

import time
from typing import Optional

# { call_id: { "data": {...}, "created_at": timestamp, "last_active": timestamp } }
_store: dict = {}

SESSION_TTL_SECONDS = 1800  # 30 minutes — auto-expire idle calls


def create_session(call_id: str, data: dict) -> None:
    now = time.time()
    _store[call_id] = {
        "data": data,
        "created_at": now,
        "last_active": now
    }


def get_session(call_id: str) -> Optional[dict]:
    """Return session data or None if expired/missing."""
    entry = _store.get(call_id)
    if not entry:
        return None
    # Auto-expire
    if time.time() - entry["last_active"] > SESSION_TTL_SECONDS:
        del _store[call_id]
        return None
    entry["last_active"] = time.time()
    return entry["data"]


def update_session(call_id: str, data: dict) -> None:
    if call_id in _store:
        _store[call_id]["data"] = data
        _store[call_id]["last_active"] = time.time()


def delete_session(call_id: str) -> None:
    _store.pop(call_id, None)


def active_session_count() -> int:
    # Purge expired sessions first
    expired = [k for k, v in _store.items() if time.time() - v["last_active"] > SESSION_TTL_SECONDS]
    for k in expired:
        del _store[k]
    return len(_store)
