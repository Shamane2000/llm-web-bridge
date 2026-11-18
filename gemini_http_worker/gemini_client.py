"""Gemini Web client via curl_cffi (cookie-based, no browser)."""
from __future__ import annotations

import json
import re
from typing import Any

from curl_cffi import requests as cffi_requests

GEMINI_BASE = "https://gemini.google.com"
GEMINI_API = f"{GEMINI_BASE}/_/GeminiUi/data/batchexecute"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class SessionExpiredError(Exception):
    """Raised when cookies are invalid or expired (NEED_SESSION_SYNC)."""


class GeminiClient:
    def __init__(self, cookies: dict[str, str], snlm0e: str | None = None):
        self.cookies = cookies
        self.snlm0e = snlm0e
        self.session = cffi_requests.Session(impersonate="chrome")
        self._conversation_id: str | None = None

    @classmethod
    def from_cookie_str(cls, cookie_str: str) -> GeminiClient:
        cookies = {}
        for part in cookie_str.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        required = {"__Secure-1PSID", "__Secure-1PSIDTS"}
        if not required.issubset(cookies.keys()):
            raise SessionExpiredError(f"Missing required cookies: {required - cookies.keys()}")
        return cls(cookies)

    def _ensure_token(self) -> str:
        if self.snlm0e:
            return self.snlm0e
        r = self.session.get(
            GEMINI_BASE,
            cookies=self.cookies,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            raise SessionExpiredError(f"Init request failed: {r.status_code}")
        m = re.search(r'"SNlM0e":"([^"]+)"', r.text)
        if not m:
            raise SessionExpiredError("SNlM0e token not found in response")
        self.snlm0e = m.group(1)
        return self.snlm0e

    def chat(self, message: str, conversation_id: str | None = None) -> dict[str, Any]:
        token = self._ensure_token()
        conv_id = conversation_id or self._conversation_id or ""
        payload = [
            [message, conv_id, None, None, [[], [], []]],
        ]
        data = {
            "f.req": json.dumps(payload),
            "at": token,
        }
        r = self.session.post(
            GEMINI_API,
            data=data,
            cookies=self.cookies,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": GEMINI_BASE,
                "Referer": f"{GEMINI_BASE}/app",
            },
            timeout=60,
        )
        if r.status_code in (401, 403):
            raise SessionExpiredError(f"Auth failed: {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"Gemini API error: {r.status_code}")
        # batchexecute returns multiple JSON arrays; parse the answer
        text = r.text.strip()
        parts = re.findall(r'(\[.*?\])\s*\n', text + "\n", re.DOTALL)
        if not parts:
            parts = [text]
        answer_text = ""
        new_conv_id = conv_id
        for part in reversed(parts):
            try:
                arr = json.loads(part)
                if isinstance(arr, list) and len(arr) >= 2:
                    inner = arr[2] if len(arr) > 2 else arr[0]
                    if isinstance(inner, str):
                        try:
                            inner_data = json.loads(inner)
                            if isinstance(inner_data, list) and len(inner_data) > 0:
                                candidate = inner_data[0]
                                if isinstance(candidate, list) and len(candidate) > 0:
                                    txt = candidate[0]
                                    if isinstance(txt, str) and len(txt) > len(answer_text):
                                        answer_text = txt
                                if len(inner_data) > 1 and isinstance(inner_data[1], str):
                                    new_conv_id = inner_data[1]
                        except (json.JSONDecodeError, TypeError):
                            pass
            except json.JSONDecodeError:
                continue
        if new_conv_id:
            self._conversation_id = new_conv_id
        return {"content": answer_text, "conversation_id": new_conv_id}
