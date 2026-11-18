"""Secure session store for Gemini Web cookies (HttpOnly).

Adapted from mailorganizer/app/google_oauth.py.
Stores __Secure-1PSID, __Secure-1PSIDTS, SNlM0e in .data/gemini_session.json (mode 0600).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("gemini.session")

SESSION_STORE = Path(__file__).resolve().parent.parent / ".data" / "gemini_session.json"
REQUIRED_COOKIES = {"__Secure-1PSID", "__Secure-1PSIDTS"}


def redact(value: str | None, keep: int = 4) -> str:
    """Redact secret values for logging."""
    if not value:
        return "***"
    if len(value) <= keep:
        return "***"
    return f"***{value[-keep:]}"


def _ensure_store_dir() -> None:
    store_dir = SESSION_STORE.parent
    store_dir.mkdir(mode=0o700, exist_ok=True)
    # Do NOT touch/create empty file here; load_session must return None if missing


def load_session() -> dict[str, Any] | None:
    """Load session from disk. Returns None if missing/corrupt/expired."""
    _ensure_store_dir()
    try:
        data = SESSION_STORE.read_text().strip()
        if not data:
            return None
        session = json.loads(data)
        # Validate required keys
        cookies = session.get("cookies", {})
        if not REQUIRED_COOKIES.issubset(cookies.keys()):
            log.warning("Session store missing required cookies: %s", REQUIRED_COOKIES - cookies.keys())
            return None
        return session
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load session store: %s", e)
        return None


def save_session(cookies: dict[str, str], snlm0e: str | None = None) -> None:
    """Persist session securely (mode 0600)."""
    _ensure_store_dir()
    payload = {
        "cookies": cookies,
        "snlm0e": snlm0e,
        "saved_at": int(time.time()),
    }
    tmp_path = SESSION_STORE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.chmod(str(tmp_path), 0o600)
    tmp_path.replace(SESSION_STORE)
    log.info(
        "Session saved (PSID=%s, PSIDTS=%s, SNlM0e=%s)",
        redact(cookies.get("__Secure-1PSID")),
        redact(cookies.get("__Secure-1PSIDTS")),
        redact(snlm0e),
    )


def get_session_status() -> dict[str, Any]:
    """Return current session status for GUI."""
    session = load_session()
    if not session:
        return {"status": "NEED_SESSION_SYNC", "message": "No valid session found. Please sync from browser."}
    
    saved_at = session.get("saved_at", 0)
    age_hours = (time.time() - saved_at) / 3600
    has_snlm0e = bool(session.get("snlm0e"))
    
    return {
        "status": "ACTIVE",
        "saved_at": saved_at,
        "age_hours": round(age_hours, 1),
        "has_snlm0e": has_snlm0e,
        "psid_redacted": redact(session["cookies"].get("__Secure-1PSID")),
        "message": f"Session active ({age_hours:.1f}h old). SNlM0e: {"Y" if has_snlm0e else "N"}",
    }
