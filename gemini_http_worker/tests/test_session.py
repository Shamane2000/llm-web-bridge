"""Tests for session_store and /session/sync + /login endpoints."""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Ensure .data dir exists in test context
TEST_DATA_DIR = Path(__file__).resolve().parent.parent.parent / ".data"
TEST_SESSION_FILE = TEST_DATA_DIR / "gemini_session.json"


@pytest.fixture(autouse=True)
def clean_session_store():
    """Remove session file before/after each test."""
    if TEST_SESSION_FILE.exists():
        TEST_SESSION_FILE.unlink()
    yield
    if TEST_SESSION_FILE.exists():
        TEST_SESSION_FILE.unlink()


class TestSessionStore:
    def test_save_and_load(self):
        from gemini_http_worker.session_store import load_session, save_session

        cookies = {"__Secure-1PSID": "abc123", "__Secure-1PSIDTS": "ts456"}
        save_session(cookies, snlm0e="token789")

        # Verify file permissions
        mode = oct(TEST_SESSION_FILE.stat().st_mode & 0o777)
        assert mode == "0o600", f"Expected 0600, got {mode}"

        session = load_session()
        assert session is not None
        assert session["cookies"] == cookies
        assert session["snlm0e"] == "token789"
        assert "saved_at" in session

    def test_load_missing_returns_none(self):
        from gemini_http_worker.session_store import load_session

        assert load_session() is None

    def test_load_corrupt_returns_none(self):
        from gemini_http_worker.session_store import load_session

        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        TEST_SESSION_FILE.write_text("{invalid json")
        assert load_session() is None

    def test_load_missing_required_cookies_returns_none(self):
        from gemini_http_worker.session_store import load_session, save_session

        # Save with only one required cookie
        TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"cookies": {"__Secure-1PSID": "abc"}, "snlm0e": None, "saved_at": int(time.time())}
        TEST_SESSION_FILE.write_text(json.dumps(payload))
        os.chmod(str(TEST_SESSION_FILE), 0o600)

        assert load_session() is None

    def test_get_session_status_active(self):
        from gemini_http_worker.session_store import get_session_status, save_session

        save_session({"__Secure-1PSID": "x", "__Secure-1PSIDTS": "y"}, snlm0e="tok")
        status = get_session_status()
        assert status["status"] == "ACTIVE"
        assert status["has_snlm0e"] is True

    def test_get_session_status_need_sync(self):
        from gemini_http_worker.session_store import get_session_status

        status = get_session_status()
        assert status["status"] == "NEED_SESSION_SYNC"

    def test_redact(self):
        from gemini_http_worker.session_store import redact

        assert redact(None) == "***"
        assert redact("") == "***"
        assert redact("ab") == "***"
        assert redact("abcdefgh") == "***efgh"
        assert redact("abcdefgh", keep=2) == "***gh"


class TestSessionEndpoints:
    @pytest.fixture
    def client(self):
        from gemini_http_worker.server import app
        return TestClient(app)

    def test_login_page_shows_need_sync(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "NEED_SESSION_SYNC" in resp.text
        assert "Gemini Session Manager" in resp.text

    def test_login_page_shows_active_after_sync(self, client):
        # First sync a session
        client.post("/session/sync", json={
            "cookies": {"__Secure-1PSID": "a", "__Secure-1PSIDTS": "b"},
            "snlm0e": "tok",
        })
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "ACTIVE" in resp.text

    def test_session_sync_success(self, client):
        resp = client.post("/session/sync", json={
            "cookies": {"__Secure-1PSID": "psid_val", "__Secure-1PSIDTS": "ts_val"},
            "snlm0e": "snlm0e_val",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

        # Verify persisted
        assert TEST_SESSION_FILE.exists()
        stored = json.loads(TEST_SESSION_FILE.read_text())
        assert stored["cookies"]["__Secure-1PSID"] == "psid_val"
        assert stored["snlm0e"] == "snlm0e_val"

    def test_session_sync_missing_cookie(self, client):
        resp = client.post("/session/sync", json={
            "cookies": {"__Secure-1PSID": "only_one"},
        })
        assert resp.status_code == 400
        assert "Missing required cookies" in resp.json()["detail"]

    def test_session_sync_without_snlm0e(self, client):
        resp = client.post("/session/sync", json={
            "cookies": {"__Secure-1PSID": "a", "__Secure-1PSIDTS": "b"},
        })
        assert resp.status_code == 200
        stored = json.loads(TEST_SESSION_FILE.read_text())
        assert stored["snlm0e"] is None


class TestGeminiClientFromStore:
    def test_from_store_returns_none_when_empty(self):
        from gemini_http_worker.gemini_client import GeminiClient
        assert GeminiClient.from_store() is None

    def test_from_store_loads_valid_session(self):
        from gemini_http_worker.gemini_client import GeminiClient
        from gemini_http_worker.session_store import save_session

        save_session(
            {"__Secure-1PSID": "psid", "__Secure-1PSIDTS": "ts"},
            snlm0e="my_token",
        )
        client = GeminiClient.from_store()
        assert client is not None
        assert client.cookies["__Secure-1PSID"] == "psid"
        assert client.snlm0e == "my_token"
