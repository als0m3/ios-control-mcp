"""Exercise the public stdio boundary without connecting to a device."""

import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from ios_control_mcp import server


def exchange(payload, *, env=None):
    result = subprocess.run(
        [sys.executable, "-m", "ios_control_mcp"],
        input=payload,
        capture_output=True,
        timeout=10,
        env={**os.environ, "IOS_CONTROL_TRANSPORT": "", "IOS_CONTROL_TIMEOUT": "60", **(env or {})},
    )
    return result, [json.loads(line) for line in result.stdout.splitlines()]


def request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


@pytest.fixture
def replies(monkeypatch):
    output = []
    monkeypatch.setattr(server, "_send", output.append)
    return output


def test_stdio_initialization_tools_and_ping():
    messages = [
        request(
            "initialize",
            {
                "protocolVersion": "unsupported",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        request("tools/list", request_id=2),
        request("ping", request_id=3),
    ]
    result, replies = exchange("\n".join(map(json.dumps, messages)).encode() + b"\n")
    assert result.returncode == 0
    assert result.stderr == b""
    assert len(replies) == 3
    assert replies[0]["result"]["protocolVersion"] == server.DEFAULT_PROTOCOL
    assert len(replies[1]["result"]["tools"]) == 11
    assert all("handler" not in tool for tool in replies[1]["result"]["tools"])
    assert replies[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}


@pytest.mark.parametrize("bad", [b"{", b"\xff", b'{"x": NaN}', b'{"x": Infinity}', b"[" * 2000])
def test_parse_errors_recover(bad):
    result, replies = exchange(bad + b"\n" + json.dumps(request("ping")).encode() + b"\n")
    assert result.returncode == 0
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["result"] == {}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        3,
        "request",
        {},
        {"id": 1, "method": "ping"},
        request("ping", request_id=True),
        request("ping", request_id={}),
    ],
)
def test_invalid_requests(bad, replies):
    server.handle(bad)
    assert replies[0]["error"]["code"] == -32600


def test_notification_never_executes_tool(monkeypatch, replies):
    handler = Mock()
    monkeypatch.setitem(server.HANDLERS, "iphone_tap", handler)
    server.handle(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "iphone_tap", "arguments": {"x": 0.5, "y": 0.5}},
        }
    )
    handler.assert_not_called()
    assert replies == []


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("iphone_tap", {"x": 100, "y": 0.5}),
        ("iphone_tap", {"x": True, "y": 0.5}),
        ("iphone_tap", {"x": "0.5", "y": 0.5}),
        ("iphone_tap", {"x": float("nan"), "y": 0.5}),
        ("iphone_tap", {"x": float("inf"), "y": 0.5}),
        ("iphone_tap", {"x": 0.5}),
        ("iphone_tap", {"x": 0.5, "y": 0.5, "hold_ms": -1}),
        ("iphone_swipe", {"x": 0, "y": 0, "x1": 1, "y1": 1, "ms": 1000000}),
        ("iphone_screenshot", {"max_width": -1}),
        ("iphone_screenshot", {"max_width": 900.5}),
        ("iphone_type", {"text": "x" * 1001}),
        ("iphone_type", {"text": ""}),
        ("iphone_type", {"text": None}),
        ("iphone_button", {"name": "invalid"}),
        ("iphone_open_app", {"app": "  "}),
        ("iphone_wait", {"ms": 10001}),
        ("iphone_wait", {"ms": -1}),
        ("iphone_status", {"extra": True}),
        ("iphone_wifi_connections", {"enabled": 1}),
        ("iphone_wifi_connections", {"enabled": "true"}),
        ("iphone_list_devices", {"udid": "x"}),
        ("iphone_status", []),
        ("iphone_status", None),
    ],
)
def test_invalid_arguments_never_reach_device(name, arguments, monkeypatch, replies):
    handler = Mock()
    monkeypatch.setitem(server.HANDLERS, name, handler)
    server.handle(request("tools/call", {"name": name, "arguments": arguments}))
    handler.assert_not_called()
    assert replies[0]["result"]["isError"] is True


@pytest.mark.parametrize("name", [[], None, "missing"])
def test_unknown_tool(name, replies):
    server.handle(request("tools/call", {"name": name}))
    assert replies[0]["error"]["code"] == -32602


def test_private_exception_is_not_exposed(monkeypatch, replies):
    monkeypatch.setitem(
        server.HANDLERS, "iphone_status", Mock(side_effect=RuntimeError("PRIVATE_DEVICE_DATA"))
    )
    server.handle(request("tools/call", {"name": "iphone_status"}))
    assert replies[0]["result"]["isError"] is True
    assert "PRIVATE_DEVICE_DATA" not in json.dumps(replies)


def test_oversize_request_closes_stream():
    result, replies = exchange(b"x" * (server.MAX_REQUEST_BYTES + 1) + b"\n")
    assert result.returncode == 0
    assert replies[0]["error"]["code"] == -32600


@pytest.mark.parametrize("value", ["bad", "nan", "inf", "0", "-1", "301"])
def test_invalid_timeout_has_clean_startup_error(value):
    result, replies = exchange(b"", env={"IOS_CONTROL_TIMEOUT": value})
    assert result.returncode == 2
    assert not replies
    assert b"IOS_CONTROL_TIMEOUT" in result.stderr
    assert b"Traceback" not in result.stderr


def test_invalid_transport():
    result, replies = exchange(b"", env={"IOS_CONTROL_TRANSPORT": "typo"})
    assert result.returncode == 2
    assert not replies


def test_valid_wait_tool(replies, monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(server.time, "sleep", sleep)
    server.handle(request("tools/call", {"name": "iphone_wait", "arguments": {"ms": 123}}))
    sleep.assert_called_once_with(0.123)
    assert replies[0]["result"]["isError"] is False
