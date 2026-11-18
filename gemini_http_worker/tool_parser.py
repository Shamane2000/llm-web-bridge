"""Extract ReAct tool_calls from LLM text output — mirrors bridge_server.py logic."""
from __future__ import annotations

import json


def extract_tool_calls(res_content: str, tool_names: list[str]) -> list[tuple[str, dict]]:
    """Extract one or more valid {"tool_call": {"name": ..., "arguments": ...}} objects.

    Mirrors the _extract_tool_calls logic from the original bridge_server.py.
    Uses raw_decode to handle self-corrections and multiple JSON blocks.
    """
    if not tool_names or "tool_call" not in (res_content or ""):
        return []
    decoder = json.JSONDecoder()
    results: list[tuple[str, dict]] = []
    idx = 0
    length = len(res_content)
    while idx < length:
        brace_idx = res_content.find("{", idx)
        if brace_idx == -1:
            break
        try:
            obj, end_idx = decoder.raw_decode(res_content, brace_idx)
            idx = end_idx
            if isinstance(obj, dict) and "tool_call" in obj:
                tc_obj = obj.get("tool_call") or {}
                tc_name = tc_obj.get("name", "")
                tc_args = tc_obj.get("arguments", {})
                if tc_name in tool_names and isinstance(tc_args, dict):
                    results.append((tc_name, tc_args))
        except Exception:
            idx = brace_idx + 1
    return results
