"""Router/API tests for the OpenAI-compatible endpoints (mocked upstream)."""
import json

import pytest
from fastapi.testclient import TestClient

from gemini_http_worker import server
from gemini_http_worker.gemini_client import SessionExpiredError

COOKIE_OK = "__Secure-1PSID=abc; __Secure-1PSIDTS=def"
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]


class FakeClient:
    def __init__(self, content="hello", error=None):
        self.content = content
        self.error = error
        self.calls = []

    def chat(self, message, conversation_id=None):
        self.calls.append(message)
        if self.error:
            raise self.error
        return {"content": self.content, "conversation_id": "c_123"}


@pytest.fixture(autouse=True)
def reset_state():
    server._clients.clear()
    server._init_souls.clear()
    yield
    server._clients.clear()
    server._init_souls.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


def test_init_missing_cookie_returns_need_session_sync(client):
    r = client.post("/init", json={"role": "worker", "soul": "s", "cookie_str": "foo=bar"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "NEED_SESSION_SYNC"


def test_init_sets_separate_role_contexts(client):
    for role, soul in (("worker", "WORKER-SOUL"), ("reviewer", "REVIEWER-SOUL")):
        r = client.post("/init", json={"role": role, "soul": soul, "cookie_str": COOKIE_OK})
        assert r.status_code == 200
        assert r.json()["role"] == role
    assert set(server._clients) == {"worker", "reviewer"}
    assert server._clients["worker"] is not server._clients["reviewer"]
    assert server._init_souls["worker"] == "WORKER-SOUL"
    assert server._init_souls["reviewer"] == "REVIEWER-SOUL"


def test_chat_without_session_returns_need_session_sync(client):
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "NEED_SESSION_SYNC"


def test_chat_plain_openai_shape(client):
    server._clients["worker"] = FakeClient(content="plain answer")
    server._init_souls["worker"] = "SOUL"
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "plain answer"
    assert "SOUL" in server._clients["worker"].calls[0]


def test_chat_tool_calls_non_stream(client):
    tc = '{"tool_call": {"name": "read_file", "arguments": {"path": "/tmp/x"}}}'
    server._clients["worker"] = FakeClient(content=f"```json\n{tc}\n```")
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "tools": TOOLS},
    )
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "/tmp/x"}


def test_chat_tool_calls_stream(client):
    tc = '{"tool_call": {"name": "read_file", "arguments": {"path": "/tmp/x"}}}'
    server._clients["worker"] = FakeClient(content=tc)
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "tools": TOOLS, "stream": True},
    )
    assert r.status_code == 200
    lines = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    names = [
        t["function"]["name"]
        for c in chunks
        for t in c["choices"][0]["delta"].get("tool_calls", [])
        if "name" in t.get("function", {})
    ]
    assert names == ["read_file"]


def test_reviewer_role_routed_to_own_conversation(client):
    worker, reviewer = FakeClient(content="w"), FakeClient(content="r")
    server._clients["worker"], server._clients["reviewer"] = worker, reviewer
    server._init_souls["reviewer"] = "REVIEWER-SOUL"
    r = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "review this"}],
            "extra_body": {"role": "reviewer"},
        },
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "r"
    assert worker.calls == []
    assert "REVIEWER-SOUL" in reviewer.calls[0]


def test_expired_session_passthrough(client):
    server._clients["worker"] = FakeClient(error=SessionExpiredError("Auth failed: 401"))
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["type"] == "NEED_SESSION_SYNC"
    assert err["code"] == "session_expired"


def test_upstream_error_is_502(client):
    server._clients["worker"] = FakeClient(error=RuntimeError("Gemini API error: 500"))
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_error"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
