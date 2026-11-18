"""Unit tests for tool_call extraction logic."""
from gemini_http_worker.tool_parser import extract_tool_calls


def test_extract_single_tool_call():
    text = 'Here is my answer.\n```json\n{"tool_call": {"name": "read_file", "arguments": {"path": "/tmp/test.txt"}}}\n```\nDone.'
    result = extract_tool_calls(text, ["read_file", "terminal"])
    assert len(result) == 1
    assert result[0][0] == "read_file"
    assert result[0][1] == {"path": "/tmp/test.txt"}


def test_extract_multiple_tool_calls():
    text = (
        '{"tool_call": {"name": "read_file", "arguments": {"path": "/a.txt"}}}\n'
        'Some text between.\n'
        '{"tool_call": {"name": "terminal", "arguments": {"command": "ls"}}}'
    )
    result = extract_tool_calls(text, ["read_file", "terminal"])
    assert len(result) == 2
    assert result[0] == ("read_file", {"path": "/a.txt"})
    assert result[1] == ("terminal", {"command": "ls"})


def test_no_match_when_tool_not_in_list():
    text = '{"tool_call": {"name": "unknown_tool", "arguments": {}}}'
    result = extract_tool_calls(text, ["read_file"])
    assert result == []


def test_empty_content():
    assert extract_tool_calls("", ["read_file"]) == []
    assert extract_tool_calls(None, ["read_file"]) == []  # type: ignore[arg-type]


def test_no_tool_call_keyword():
    text = "Just a normal response without any JSON."
    assert extract_tool_calls(text, ["read_file"]) == []


def test_malformed_json_skipped():
    text = '{"tool_call": {"name": "read_file", "arguments": INVALID}}\n{"tool_call": {"name": "terminal", "arguments": {"cmd": "pwd"}}}'
    result = extract_tool_calls(text, ["read_file", "terminal"])
    assert len(result) == 1
    assert result[0][0] == "terminal"
