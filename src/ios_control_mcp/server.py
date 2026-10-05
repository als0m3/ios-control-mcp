"""MCP server exposing an iPhone over CoreDevice.

Screenshots, touch, keyboard, hardware buttons and app launching, driven
through the same developer transport Xcode uses. No companion application is
required on the Mac, and nothing has to run on the phone.

Design note — why on-demand capture rather than a live mirror: capturing an
iPhone screen as a continuous video stream goes through CoreMediaIO, whose
device stops delivering frames after a few minutes of sustained capture. The
session keeps reporting itself as healthy, so callers silently act on a frozen
picture. The condition is not recoverable in-process: reattaching the input,
rebuilding the capture session and releasing every retained buffer all fail,
and a minimal consumer that retains nothing reproduces it identically. Taking
one screenshot at a time keeps no stream alive, so the failure cannot occur.
It also yields the native resolution and a genuine status bar, where screen
recording substitutes a placeholder one.

Transport is stdio, JSON-RPC 2.0, newline delimited.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

__all__ = ["main"]

SERVER_NAME = "ios-control"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"

#: Pin a device when several are attached; otherwise the first one is used.
UDID = os.environ.get("IOS_CONTROL_UDID", "")
#: ``usb``, ``wifi`` or empty to prefer USB and fall back to the network.
TRANSPORT = os.environ.get("IOS_CONTROL_TRANSPORT", "").strip().lower()
CALL_TIMEOUT_S = float(os.environ.get("IOS_CONTROL_TIMEOUT", "60"))

#: Hardware buttons as (HID usage page, usage code).
BUTTONS = {
    "home": (0x0C, 0x40),
    "lock": (0x0C, 0x30),
    "volume-up": (0x0C, 0xE9),
    "volume-down": (0x0C, 0xEA),
    "mute": (0x0C, 0xE2),
    "siri": (0x0C, 0xCF),
}
BUTTON_HOLD_S = {"lock": 0.5, "siri": 1.0}
DEFAULT_BUTTON_HOLD_S = 0.05

KEY_LEFT_SHIFT = 0xE1
DIGITIZER_MAX = 65535

#: A lone touch report is discarded by the digitizer as a spurious reading, so
#: the initial contact is always sent twice.
CONTACT_REPEAT_DELAY_S = 0.045
#: Closing the HID service immediately after the release cancels the gesture
#: before it reaches the phone.
GESTURE_FLUSH_S = 0.25


class DeviceError(Exception):
    """Failure worth showing to the caller rather than raising as a crash."""


# --------------------------------------------------------------------------
# Service event loop
#
# pymobiledevice3 is asynchronous while the MCP loop is synchronous, so the
# event loop lives in a daemon thread and the tunnel stays open between calls.
# --------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_tunnel = None
_rsd = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
        return _loop


def _run(coro, timeout: float = CALL_TIMEOUT_S):
    future = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise DeviceError(
            f"Timed out after {timeout:.0f}s. Is the iPhone unlocked?"
        ) from None


async def _maybe(value):
    """Await a value when the installed pymobiledevice3 returns a coroutine."""
    return await value if asyncio.iscoroutine(value) else value


def _connection_kind(device) -> str:
    return "Network" if getattr(device, "connection_type", "USB") != "USB" else "USB"


async def _list_transports() -> list[str]:
    from pymobiledevice3.usbmux import list_devices

    devices = await _maybe(list_devices())
    return sorted({_connection_kind(device) for device in devices})


async def _pick_device() -> tuple[str, str]:
    """Return ``(serial, "USB" | "Network")`` for the device to drive."""
    from pymobiledevice3.usbmux import list_devices

    devices = await _maybe(list_devices())
    if not devices:
        raise DeviceError(
            "No iPhone found. Over USB: plug the device in and unlock it. "
            "Wirelessly: enable wireless connections once while plugged in "
            "(see the README), and stay on the same network."
        )

    if UDID:
        devices = [d for d in devices if d.serial == UDID] or devices

    if TRANSPORT in ("wifi", "network"):
        for device in devices:
            if _connection_kind(device) == "Network":
                return device.serial, "Network"
        raise DeviceError(
            "No iPhone reachable over the network. Enable wireless connections "
            "while plugged in, and check that both devices share a network."
        )
    if TRANSPORT == "usb":
        for device in devices:
            if _connection_kind(device) == "USB":
                return device.serial, "USB"
        raise DeviceError("No iPhone on USB. Plug the device in.")

    for preference in ("USB", "Network"):
        for device in devices:
            if _connection_kind(device) == preference:
                return device.serial, preference
    return devices[0].serial, _connection_kind(devices[0])


async def _open_tunnel():
    global _tunnel, _rsd
    import pymobiledevice3.remote.userspace_tunnel as ut

    serial, connection_type = await _pick_device()

    # The tunnel helper does not expose the connection type, so it is injected
    # for the duration of the handshake.
    original = ut.create_using_usbmux

    async def patched(*args, **kwargs):
        kwargs["connection_type"] = connection_type
        return await original(*args, **kwargs)

    ut.create_using_usbmux = patched
    try:
        _tunnel = ut.UserspaceRsdTunnel(serial=serial, autopair=True)
        _rsd = await _tunnel.aopen()
    finally:
        ut.create_using_usbmux = original
    return _rsd


async def _close_tunnel() -> None:
    global _tunnel, _rsd
    if _tunnel is not None:
        try:
            await _maybe(_tunnel.aclose())
        except Exception:
            pass
    _tunnel, _rsd = None, None


async def _with_service(factory, action):
    """Open a CoreDevice service, run ``action``, then close it.

    Services do not survive a dropped link: a stale connection raises on the
    next call. One retry on a fresh tunnel turns that into a transparent
    recovery instead of an error the caller has to handle.
    """
    for attempt in (1, 2):
        try:
            if _rsd is None:
                await _open_tunnel()
            service = factory(_rsd)
            await _maybe(service.connect())
            try:
                return await action(service)
            finally:
                try:
                    await _maybe(service.close())
                except Exception:
                    pass
        except DeviceError:
            raise
        except Exception as exc:
            await _close_tunnel()
            if attempt == 2:
                raise DeviceError(
                    f"Could not reach the iPhone ({type(exc).__name__}). "
                    "Check that it is connected, unlocked and paired."
                ) from exc
    raise DeviceError("Unexpected failure.")


# --------------------------------------------------------------------------
# Screen capture
# --------------------------------------------------------------------------

def _to_jpeg(png: bytes, width: int) -> tuple[bytes, str]:
    """Downscale to JPEG with ``sips``, which ships with macOS.

    A native screenshot is roughly 7 MB of PNG, far too large to hand to a
    model as-is. Returns the original bytes if the conversion fails.
    """
    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "screen.png")
        target = os.path.join(folder, "screen.jpg")
        with open(source, "wb") as handle:
            handle.write(png)
        result = subprocess.run(
            # --resampleWidth bounds the width; -Z would bound the longest
            # side, which on a phone screen is the height.
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
             "--resampleWidth", str(width), source, "--out", target],
            capture_output=True, check=False,
        )
        if result.returncode != 0 or not os.path.exists(target):
            return png, "image/png"
        with open(target, "rb") as handle:
            return handle.read(), "image/jpeg"


async def _capture_png() -> bytes:
    from pymobiledevice3.remote.core_device.screen_capture_service import (
        ScreenCaptureService,
    )

    async def grab(service):
        payload = await _maybe(service.capture_screenshot())
        for value in payload.values():
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
        for value in payload.values():
            if isinstance(value, str) and len(value) > 1000:
                return base64.b64decode(value)
        raise DeviceError(f"Unexpected screenshot payload: {list(payload)[:6]}")

    return await _with_service(ScreenCaptureService, grab)


# --------------------------------------------------------------------------
# Touch, keyboard and buttons
# --------------------------------------------------------------------------

def _to_digitizer(value: float, name: str) -> int:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise DeviceError(
            f"{name}={number} is out of range: coordinates run from 0.0 to 1.0, "
            "not in pixels."
        )
    return int(number * DIGITIZER_MAX)


async def _touch_path(points: list[tuple[int, int]], hold_s: float) -> None:
    from pymobiledevice3.remote.core_device.hid_service import (
        DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
        TOUCHSCREEN_STATE_CONTACT,
        TOUCHSCREEN_STATE_RELEASE,
        UniversalHIDServiceService,
    )

    async def play(service):
        async def contact(x: int, y: int) -> None:
            await _maybe(service.send_touchscreen(
                TOUCHSCREEN_STATE_CONTACT, x, y,
                service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN))

        first_x, first_y = points[0]
        await contact(first_x, first_y)
        await asyncio.sleep(CONTACT_REPEAT_DELAY_S)
        await contact(first_x, first_y)
        await asyncio.sleep(max(hold_s, 0.10))

        for x, y in points[1:]:
            await contact(x, y)
            await asyncio.sleep(0.016)

        last_x, last_y = points[-1]
        await _maybe(service.send_touchscreen(
            TOUCHSCREEN_STATE_RELEASE, last_x, last_y,
            service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN))
        await asyncio.sleep(GESTURE_FLUSH_S)

    await _with_service(UniversalHIDServiceService, play)


async def _type_text(text: str) -> int:
    from pymobiledevice3.remote.core_device.hid_service import (
        ASCII_TO_HID,
        UniversalHIDServiceService,
    )

    async def play(service):
        keyboard = await _maybe(service.create_keyboard_service())
        typed = 0
        for char in text:
            mapping = ASCII_TO_HID.get(char)
            if mapping is None:
                continue
            usage, needs_shift = mapping
            codes = ([KEY_LEFT_SHIFT] if needs_shift else []) + [usage]
            await _maybe(service.send_keyboard(keyboard, codes))
            await asyncio.sleep(0.012)
            await _maybe(service.send_keyboard(keyboard, []))
            await asyncio.sleep(0.012)
            typed += 1
        return typed

    return await _with_service(UniversalHIDServiceService, play)


async def _press_button(name: str) -> None:
    from pymobiledevice3.remote.core_device.hid_service import (
        HID_BUTTON_STATE_DOWN,
        HID_BUTTON_STATE_UP,
        IndigoHIDService,
    )

    page, code = BUTTONS[name]
    hold = BUTTON_HOLD_S.get(name, DEFAULT_BUTTON_HOLD_S)

    async def play(service):
        await _maybe(service.send_button(page, code, HID_BUTTON_STATE_DOWN))
        await asyncio.sleep(hold)
        await _maybe(service.send_button(page, code, HID_BUTTON_STATE_UP))

    await _with_service(IndigoHIDService, play)


async def _device_info() -> dict:
    from pymobiledevice3.lockdown import create_using_usbmux

    lockdown = await _maybe(create_using_usbmux())
    return await _maybe(lockdown.all_values)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _text(payload: dict) -> list[dict]:
    return [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]


def tool_status(_arguments: dict) -> list[dict]:
    try:
        info = _run(_device_info())
    except DeviceError:
        raise
    except Exception as exc:
        raise DeviceError(
            f"iPhone unreachable ({type(exc).__name__}). Connect it, unlock it, "
            "and accept the trust prompt if one appears."
        ) from exc
    return _text({
        "connected": True,
        "name": info.get("DeviceName"),
        "ios": info.get("ProductVersion"),
        "build": info.get("BuildVersion"),
        "model": info.get("ProductType"),
        "udid": info.get("UniqueDeviceID"),
        "transports": _run(_list_transports()),
        "tunnel_open": _rsd is not None,
    })


def tool_screenshot(arguments: dict) -> list[dict]:
    width = int(arguments.get("max_width", 900))
    data, mime = _to_jpeg(_run(_capture_png()), width)
    return [{
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime,
    }]


def tool_tap(arguments: dict) -> list[dict]:
    x = _to_digitizer(arguments["x"], "x")
    y = _to_digitizer(arguments["y"], "y")
    hold_s = float(arguments.get("hold_ms", 120)) / 1000.0
    _run(_touch_path([(x, y)], hold_s))
    return _text({"ok": True, "action": "tap"})


def tool_swipe(arguments: dict) -> list[dict]:
    x0, y0 = _to_digitizer(arguments["x"], "x"), _to_digitizer(arguments["y"], "y")
    x1, y1 = _to_digitizer(arguments["x1"], "x1"), _to_digitizer(arguments["y1"], "y1")
    duration_ms = max(120, int(arguments.get("ms", 300)))
    steps = max(6, duration_ms // 16)

    points = [(x0, y0)]
    for step in range(1, steps + 1):
        ratio = step / steps
        points.append((int(x0 + (x1 - x0) * ratio), int(y0 + (y1 - y0) * ratio)))

    _run(_touch_path(points, 0.05), timeout=CALL_TIMEOUT_S + duration_ms / 1000.0)
    return _text({"ok": True, "action": "swipe"})


def tool_type(arguments: dict) -> list[dict]:
    text = str(arguments["text"])
    typed = _run(_type_text(text), timeout=CALL_TIMEOUT_S + len(text) * 0.1)
    payload = {"ok": True, "action": "type", "characters": typed}
    if typed < len(text):
        payload["skipped"] = len(text) - typed
        payload["note"] = "Non-ASCII characters were skipped; the HID keyboard cannot emit them."
    return _text(payload)


def tool_button(arguments: dict) -> list[dict]:
    name = str(arguments.get("name", "home"))
    if name not in BUTTONS:
        raise DeviceError(f"Unknown button: {name}. Available: {', '.join(sorted(BUTTONS))}.")
    _run(_press_button(name))
    return _text({"ok": True, "action": "button", "name": name})


def _app_entries(apps):
    entries = apps.values() if isinstance(apps, dict) else apps
    for app in entries:
        bundle = app.get("bundleIdentifier") or app.get("bundleID") or ""
        name = app.get("name") or app.get("bundleName") or ""
        yield name, bundle


def tool_open_app(arguments: dict) -> list[dict]:
    from pymobiledevice3.remote.core_device.app_service import AppServiceService

    wanted = str(arguments["app"]).strip()
    needle = wanted.lower()

    async def run(service):
        apps = await _maybe(service.list_apps())
        fallback = None
        for name, bundle in _app_entries(apps):
            if needle in (bundle.lower(), name.lower()):
                fallback = (name, bundle)
                break
            if fallback is None and needle in name.lower():
                fallback = (name, bundle)
        if fallback is None:
            raise DeviceError(f"No installed app matches {wanted!r}.")
        await _maybe(service.launch_application(fallback[1]))
        return fallback

    name, bundle = _run(_with_service(AppServiceService, run))
    return _text({"ok": True, "action": "open_app", "app": name, "bundle": bundle})


def tool_list_apps(arguments: dict) -> list[dict]:
    from pymobiledevice3.remote.core_device.app_service import AppServiceService

    needle = str(arguments.get("contains", "")).lower()

    async def run(service):
        apps = await _maybe(service.list_apps())
        found = [
            {"name": name, "bundle": bundle}
            for name, bundle in _app_entries(apps)
            if not needle or needle in name.lower() or needle in bundle.lower()
        ]
        return sorted(found, key=lambda app: app["name"].lower())[:200]

    return _text({"apps": _run(_with_service(AppServiceService, run))})


def tool_wait(arguments: dict) -> list[dict]:
    milliseconds = max(0, min(10_000, int(arguments.get("ms", 500))))
    time.sleep(milliseconds / 1000.0)
    return _text({"ok": True, "action": "wait", "ms": milliseconds})


NORMALISED = {"type": "number", "minimum": 0, "maximum": 1}

TOOLS = [
    {
        "name": "iphone_status",
        "description": (
            "Device identity and link state: name, iOS version, model, and which "
            "transports are currently available."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_status,
    },
    {
        "name": "iphone_screenshot",
        "description": (
            "Capture the iPhone screen and return the image. Taken on demand at "
            "the native resolution, then downscaled to max_width for transport."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_width": {
                    "type": "integer", "minimum": 200, "maximum": 1400,
                    "description": "Output width in pixels (default 900).",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_screenshot,
    },
    {
        "name": "iphone_tap",
        "description": (
            "Tap the screen once. Coordinates are NORMALISED from 0.0 to 1.0 "
            "(x=0 is the left edge, y=0 the top), never pixels."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": NORMALISED,
                "y": NORMALISED,
                "hold_ms": {"type": "integer", "description": "Contact duration (default 120)."},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "handler": tool_tap,
    },
    {
        "name": "iphone_swipe",
        "description": (
            "Drag from (x,y) to (x1,y1), normalised 0.0–1.0. Avoid starting at the "
            "very bottom edge: iOS reads that as a system gesture and leaves the app."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": NORMALISED, "y": NORMALISED,
                "x1": NORMALISED, "y1": NORMALISED,
                "ms": {"type": "integer", "description": "Gesture duration (default 300)."},
            },
            "required": ["x", "y", "x1", "y1"],
            "additionalProperties": False,
        },
        "handler": tool_swipe,
    },
    {
        "name": "iphone_type",
        "description": "Type text into the focused field using a virtual keyboard. ASCII only.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": tool_type,
    },
    {
        "name": "iphone_button",
        "description": "Press a hardware button: home, lock, volume-up, volume-down, mute or siri.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": sorted(BUTTONS)}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "handler": tool_button,
    },
    {
        "name": "iphone_open_app",
        "description": (
            "Launch an app by display name or bundle identifier. Allow a couple of "
            "seconds before the next screenshot, as a cold start takes a moment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "\"Settings\" or com.apple.Preferences."},
            },
            "required": ["app"],
            "additionalProperties": False,
        },
        "handler": tool_open_app,
    },
    {
        "name": "iphone_list_apps",
        "description": "List installed apps with their bundle identifiers.",
        "inputSchema": {
            "type": "object",
            "properties": {"contains": {"type": "string", "description": "Filter on the name."}},
            "additionalProperties": False,
        },
        "handler": tool_list_apps,
    },
    {
        "name": "iphone_wait",
        "description": "Pause, to let an animation settle before the next screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {"ms": {"type": "integer", "description": "Default 500, capped at 10000."}},
            "additionalProperties": False,
        },
        "handler": tool_wait,
    },
]

HANDLERS = {tool["name"]: tool["handler"] for tool in TOOLS}
TOOL_SPECS = [{key: value for key, value in tool.items() if key != "handler"} for tool in TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC loop
# --------------------------------------------------------------------------

def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _reply(request_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _fail(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # Notifications expect no reply.
    if request_id is None:
        return

    if method == "initialize":
        _reply(request_id, {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        _reply(request_id, {})
    elif method == "tools/list":
        _reply(request_id, {"tools": TOOL_SPECS})
    elif method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            _fail(request_id, -32602, f"Unknown tool: {name}")
            return
        try:
            content = handler(params.get("arguments") or {})
            _reply(request_id, {"content": content, "isError": False})
        except DeviceError as exc:
            _reply(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (KeyError, TypeError, ValueError) as exc:
            _reply(request_id, {
                "content": [{"type": "text", "text": f"Invalid argument: {exc}"}],
                "isError": True,
            })
    else:
        _fail(request_id, -32601, f"Unsupported method: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            handle(message)
        except Exception as exc:  # a malformed request must not kill the loop
            if isinstance(message, dict) and message.get("id") is not None:
                _fail(message["id"], -32603, f"Internal error: {exc}")


if __name__ == "__main__":
    main()
