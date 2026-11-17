#!/usr/bin/env python3
"""Remote control CLI for Gemini/Claude Browser-Bridge (localhost:8765)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"


def api_call(base_url: str, path: str, payload: dict | None = None, timeout: int = 8) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, body
    except Exception as exc:
        return 0, str(exc)


def main() -> int:
    p = argparse.ArgumentParser(description="Control Gemini/Claude Browser-Bridge and tabs")
    p.add_argument("--url", default=DEFAULT_URL, help="Bridge base URL")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show bridge health & active tabs")
    sub.add_parser("tabs", help="List active browser tabs")
    sub.add_parser("restart-server", help="Restart the running bridge HTTP server via /control/restart")

    for act in ("reload", "new-chat", "ping"):
        sp = sub.add_parser(act, help=f"Send '{act}' command to matching browser tabs")
        sp.add_argument("--provider", default="all", choices=["all", "claude", "gemini"])
        sp.add_argument("--role", default="all", choices=["all", "worker", "coder", "reviewer", "solo", "image", "canvas", "research"])
        sp.add_argument("--tab-id", default="")

    sr = sub.add_parser("set-role", help="Change role of matching tab(s)")
    sr.add_argument("--value", required=True, choices=["worker", "coder", "reviewer", "solo", "image", "canvas", "research"])
    sr.add_argument("--provider", default="all", choices=["all", "claude", "gemini"])
    sr.add_argument("--role", default="all")
    sr.add_argument("--tab-id", default="")

    af = sub.add_parser("auto-focus", help="Enable or disable automatic X11 focus assist for Claude tabs")
    af.add_argument("state", choices=["on", "off"])

    args = p.parse_args()

    if args.cmd == "auto-focus":
        code, resp = api_call(args.url, "/control/auto_focus", payload={"enabled": args.state == "on"})
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0 if code == 200 else 1

    if args.cmd == "status":
        c1, h = api_call(args.url, "/health")
        c2, t = api_call(args.url, "/tabs")
        print(json.dumps({"health_code": c1, "health": h, "tabs_code": c2, "tabs": t}, indent=2, ensure_ascii=False))
        return 0 if c1 == 200 else 1

    if args.cmd == "tabs":
        code, resp = api_call(args.url, "/tabs")
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0 if code == 200 else 1

    if args.cmd == "restart-server":
        code, resp = api_call(args.url, "/control/restart", payload={})
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0 if code == 200 else 1

    action_map = {"reload": "reload", "new-chat": "new_chat", "ping": "ping", "set-role": "set_role"}
    payload = {
        "action": action_map[args.cmd],
        "provider": args.provider,
        "role": args.role,
        "tab_id": args.tab_id,
        "value": getattr(args, "value", ""),
    }
    code, resp = api_call(args.url, "/control", payload=payload)
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 0 if code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
