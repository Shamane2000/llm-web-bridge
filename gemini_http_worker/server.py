"""FastAPI server — OpenAI-compatible /v1/chat/completions + /init for Gemini HTTP Worker."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .gemini_client import GeminiClient, SessionExpiredError
from .tool_parser import extract_tool_calls

app = FastAPI(title="Gemini HTTP Worker", version="0.1.0")

# Per-role conversation store: role -> GeminiClient
_clients: dict[str, GeminiClient] = {}
_init_souls: dict[str, str] = {}


def _get_client(role: str) -> GeminiClient:
    if role not in _clients:
        raise HTTPException(status_code=400, detail=f"No session initialized for role '{role}'. Call /init first.")
    return _clients[role]


class InitRequest(BaseModel):
    role: str  # "worker" | "reviewer"
    soul: str
    cookie_str: str
    snlm0e: str | None = None


@app.post("/init")
async def init_session(req: InitRequest):
    try:
        client = GeminiClient.from_cookie_str(req.cookie_str)
        if req.snlm0e:
            client.snlm0e = req.snlm0e
        _clients[req.role] = client
        _init_souls[req.role] = req.soul
        return {"status": "ok", "role": req.role}
    except SessionExpiredError as e:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": str(e), "type": "NEED_SESSION_SYNC", "code": "session_expired"}},
        )


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "gemini-browser"
    messages: list[ChatMessage]
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    extra_body: dict[str, Any] | None = None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    # Determine role from extra_body or default to worker
    role = "worker"
    if body.extra_body and "role" in body.extra_body:
        role = body.extra_body["role"]

    try:
        client = _get_client(role)
    except HTTPException:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": f"No session for role '{role}'", "type": "NEED_SESSION_SYNC", "code": "no_session"}},
        )

    # Build prompt: system soul + user messages
    soul = _init_souls.get(role, "")
    user_content = "\n".join(m.content for m in body.messages if m.role == "user")
    if not user_content:
        user_content = "\n".join(m.content for m in body.messages[-1:])

    full_prompt = f"{soul}\n\n--- ANFRAGE ---\n{user_content}" if soul else user_content

    tool_names = []
    if body.tools:
        for t in body.tools:
            fn = t.get("function", {})
            if "name" in fn:
                tool_names.append(fn["name"])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    try:
        result = client.chat(full_prompt)
        res_content = result.get("content", "")
    except SessionExpiredError as e:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": str(e), "type": "NEED_SESSION_SYNC", "code": "session_expired"}},
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(e), "type": "upstream_error", "code": "gemini_request_failed"}},
        )

    # Extract tool calls
    parsed_tcs = extract_tool_calls(res_content, tool_names) if tool_names else []

    if body.stream:
        async def event_stream():
            if parsed_tcs:
                base_ts = int(time.time() * 1000)
                for idx, (t_name, t_args) in enumerate(parsed_tcs):
                    args_str = json.dumps(t_args, ensure_ascii=False)
                    call_id = f"call_{base_ts}_{idx}"
                    delta_start = {"tool_calls": [{"index": idx, "id": call_id, "type": "function", "function": {"name": t_name, "arguments": ""}}]}
                    if idx == 0:
                        delta_start["role"] = "assistant"
                    chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": body.model, "choices": [{"index": 0, "delta": delta_start, "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    arg_chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": body.model, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "function": {"arguments": args_str}}]}, "finish_reason": None}]}
                    yield f"data: {json.dumps(arg_chunk)}\n\n"
                final = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": body.model, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
                yield f"data: {json.dumps(final)}\n\n"
            else:
                start = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": body.model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
                yield f"data: {json.dumps(start)}\n\n"
                content_chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": body.model, "choices": [{"index": 0, "delta": {"content": res_content}, "finish_reason": None}]}
                yield f"data: {json.dumps(content_chunk)}\n\n"
                final = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": body.model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming response
    if parsed_tcs:
        base_ts = int(time.time() * 1000)
        msg_obj = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{base_ts}_{idx}",
                    "type": "function",
                    "function": {"name": t_name, "arguments": json.dumps(t_args, ensure_ascii=False)},
                }
                for idx, (t_name, t_args) in enumerate(parsed_tcs)
            ],
        }
        finish_reason = "tool_calls"
    else:
        msg_obj = {"role": "assistant", "content": res_content}
        finish_reason = "stop"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.model,
        "choices": [{"index": 0, "message": msg_obj, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/health")
async def health():
    return {"status": "ok", "roles_initialized": list(_clients.keys())}
