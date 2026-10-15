"""Local stdio MCP server for iPhone control through CoreDevice."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import inspect
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time

__all__ = ["main"]

SERVER_NAME = "ios-control"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL = "2024-11-05"

#: Select a device explicitly when more than one is attached.
UDID = os.environ.get("IOS_CONTROL_UDID", "")
#: ``usb``, ``wifi`` or empty to prefer USB and fall back to the network.
TRANSPORT = os.environ.get("IOS_CONTROL_TRANSPORT", "").strip().lower()
CALL_TIMEOUT_S = 60.0
MAX_REQUEST_BYTES = 65_536
_protocol_output = None


def _configure() -> None:
    """Validate configuration before accepting any device requests."""
    global CALL_TIMEOUT_S
    if TRANSPORT not in ("", "usb", "wifi"):
        raise DeviceError("IOS_CONTROL_TRANSPORT must be usb or wifi, or unset.")
    try:
        timeout = float(os.environ.get("IOS_CONTROL_TIMEOUT", "60"))
    except ValueError:
        raise DeviceError("IOS_CONTROL_TIMEOUT must be a number from 1 to 300.") from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise DeviceError("IOS_CONTROL_TIMEOUT must be a number from 1 to 300.")
    CALL_TIMEOUT_S = timeout


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
KEY_LEFT_GUI = 0xE3
DIGITIZER_MAX = 65535

#: A lone touch report is discarded by the digitizer as a spurious reading, so
#: the initial contact is always sent twice.
CONTACT_REPEAT_DELAY_S = 0.045
#: Closing the HID service immediately after the release cancels the gesture
#: before it reaches the phone.
GESTURE_FLUSH_S = 0.25
#: Spacing between keyboard reports. Below roughly 20 ms the device starts
#: dropping them, which strands modifiers in the pressed state.
KEY_REPORT_DELAY_S = 0.025
#: Time for the focused app to read the pasteboard before it is restored.
PASTE_SETTLE_S = 0.4


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
_loop_thread: threading.Thread | None = None
_service_lock: asyncio.Lock | None = None
_tunnel = None
_rsd = None
_selected_serial: str | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
            _loop_thread.start()
        return _loop


def _run(coro, timeout: float | None = None):
    timeout = CALL_TIMEOUT_S if timeout is None else timeout
    future = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise DeviceError(
            f"Timed out after {timeout:.0f}s. The action may have partially completed; check the screen before retrying."
        ) from None


async def _maybe(value):
    """Await a value when the installed pymobiledevice3 returns a coroutine."""
    return await value if inspect.isawaitable(value) else value


def _connection_kind(device) -> str:
    return "Network" if getattr(device, "connection_type", "USB") != "USB" else "USB"


async def _list_transports() -> list[str]:
    from pymobiledevice3.usbmux import list_devices

    devices = await _maybe(list_devices())
    return sorted(
        {_connection_kind(device) for device in devices if device.serial == _selected_serial}
    )


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

    target = UDID or _selected_serial
    if target:
        devices = [d for d in devices if d.serial == target]
        if not devices:
            raise DeviceError("The selected iPhone is unavailable. No other device was selected.")
    elif len({d.serial for d in devices}) > 1:
        raise DeviceError("Multiple devices found. Set IOS_CONTROL_UDID to select one explicitly.")

    if TRANSPORT == "wifi":
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
    global _tunnel, _rsd, _selected_serial
    import pymobiledevice3.remote.userspace_tunnel as ut

    serial, connection_type = await _pick_device()
    _selected_serial = serial

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
    except BaseException:
        await _close_tunnel()
        raise
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


async def _with_service(factory, action, *, retry_safe: bool = False):
    """Serialize tunnel access and retry only explicitly read-only operations."""
    global _service_lock
    if _service_lock is None:
        _service_lock = asyncio.Lock()
    async with _service_lock:
        for attempt in range(2 if retry_safe else 1):
            try:
                if _rsd is None:
                    await _open_tunnel()
                service = factory(_rsd)
                try:
                    await _maybe(service.connect())
                    return await action(service)
                finally:
                    with contextlib.suppress(Exception):
                        await _maybe(service.close())
            except asyncio.CancelledError:
                await _close_tunnel()
                raise
            except DeviceError:
                raise
            except Exception as exc:
                await _close_tunnel()
                if not retry_safe or attempt == 1:
                    raise DeviceError(
                        f"Device operation failed ({type(exc).__name__}). "
                        "Check the connection and screen before retrying; "
                        "the action may have partially completed."
                    ) from None
        raise DeviceError("Unexpected device failure.")


def _shutdown() -> None:
    """Release the tunnel and worker loop on EOF or interruption."""
    global _loop, _loop_thread, _service_lock, _selected_serial
    if _loop is None:
        return
    loop = _loop

    async def cleanup():
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await _close_tunnel()
        await loop.shutdown_asyncgens()

    future = asyncio.run_coroutine_threadsafe(cleanup(), loop)
    with contextlib.suppress(Exception):
        future.result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    if _loop_thread is not None:
        _loop_thread.join(timeout=2)
    if not loop.is_running():
        loop.close()
    _loop = _loop_thread = _service_lock = _selected_serial = None


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
        try:
            result = subprocess.run(
                [
                    "/usr/bin/sips",
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    "80",
                    "--resampleWidth",
                    str(width),
                    source,
                    "--out",
                    target,
                ],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return png, "image/png"
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
        value = payload.get("image") if isinstance(payload, dict) else None
        if isinstance(value, str):
            try:
                value = base64.b64decode(value, validate=True)
            except ValueError:
                value = None
        if isinstance(value, (bytes, bytearray)) and value.startswith(b"\x89PNG\r\n\x1a\n"):
            return bytes(value)
        raise DeviceError("The device returned an unexpected screenshot payload.")

    return await _with_service(ScreenCaptureService, grab, retry_safe=True)


# --------------------------------------------------------------------------
# Touch, keyboard and buttons
# --------------------------------------------------------------------------


def _to_digitizer(value: float, name: str) -> int:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise DeviceError(
            f"{name}={number} is out of range: coordinates run from 0.0 to 1.0, not in pixels."
        )
    return int(number * DIGITIZER_MAX)


@contextlib.asynccontextmanager
async def _release_input(release):
    """Preserve action errors, but report a failed release after a successful action."""
    try:
        yield
    except BaseException:
        with contextlib.suppress(Exception):
            await release()
        raise
    else:
        await release()


async def _touch_path(points: list[tuple[int, int]], hold_s: float) -> None:
    from pymobiledevice3.remote.core_device.hid_service import (
        DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
        TOUCHSCREEN_STATE_CONTACT,
        TOUCHSCREEN_STATE_RELEASE,
        UniversalHIDServiceService,
    )

    async def play(service):
        async def contact(x: int, y: int) -> None:
            await _maybe(
                service.send_touchscreen(
                    TOUCHSCREEN_STATE_CONTACT, x, y, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN
                )
            )

        last_x, last_y = points[0]

        async def release():
            await _maybe(
                service.send_touchscreen(
                    TOUCHSCREEN_STATE_RELEASE,
                    last_x,
                    last_y,
                    service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
                )
            )
            await asyncio.sleep(GESTURE_FLUSH_S)

        async with _release_input(release):
            await contact(last_x, last_y)
            await asyncio.sleep(CONTACT_REPEAT_DELAY_S)
            await contact(last_x, last_y)
            await asyncio.sleep(max(hold_s, 0.10))
            for last_x, last_y in points[1:]:
                await contact(last_x, last_y)
                await asyncio.sleep(0.016)

    await _with_service(UniversalHIDServiceService, play)


async def _send_keys(chords: list[list[int]]) -> None:
    """Press each chord in turn, releasing every key between chords."""
    from pymobiledevice3.remote.core_device.hid_service import UniversalHIDServiceService

    async def play(service):
        keyboard = await _maybe(service.create_keyboard_service())

        async def report(codes) -> None:
            await _maybe(service.send_keyboard(keyboard, codes))
            await asyncio.sleep(KEY_REPORT_DELAY_S)

        try:
            for chord in chords:
                # Modifiers go down alone first, in separate reports, so that
                # none of them stays latched.
                modifiers = [code for code in chord if code >= 0xE0]
                if modifiers:
                    await report(modifiers)
                await report(chord)
                if modifiers:
                    await report(modifiers)
                await report([])
        finally:
            # Best effort, including on cancellation or a failed key report.
            with contextlib.suppress(Exception):
                await report([])

    await _with_service(UniversalHIDServiceService, play)


async def _type_text(text: str) -> tuple[int, str]:
    """Type ``text`` into the focused field; return ``(characters, method)``.

    Printable ASCII is typed key by key. Anything else (accents, emoji, other
    scripts) cannot be expressed as US keyboard usages, so it is pasted.
    """
    from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID

    if all(char in ASCII_TO_HID for char in text):
        chords = []
        for char in text:
            usage, needs_shift = ASCII_TO_HID[char]
            chords.append([KEY_LEFT_SHIFT, usage] if needs_shift else [usage])
        await _send_keys(chords)
        return len(text), "keys"
    await _paste_text(text)
    return len(text), "paste"


async def _paste_text(text: str) -> None:
    """Paste ``text`` with the device pasteboard and Command-V.

    The pasteboard is restored afterwards when it held text. Other content
    (an image, for instance) cannot be restored and is replaced.
    """
    from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID
    from pymobiledevice3.remote.core_device.pasteboard_service import (
        POLICY_PROMISE_SECONDARY,
        PasteboardService,
        snapshot_text,
    )

    async def pull(service):
        return snapshot_text(await _maybe(service.get(data_policy=POLICY_PROMISE_SECONDARY)))

    def setter(value: str):
        async def put(service):
            await _maybe(service.set_text(value))

        return put

    # The pasteboard daemon answers one request per connection, which
    # _with_service provides by opening a service for each call.
    try:
        previous = await _with_service(PasteboardService, pull, retry_safe=True)
    except DeviceError:
        previous = None
    await _with_service(PasteboardService, setter(text))
    try:
        await _send_keys([[KEY_LEFT_GUI, ASCII_TO_HID["v"][0]]])
        await asyncio.sleep(PASTE_SETTLE_S)
    finally:
        if previous is not None:
            with contextlib.suppress(DeviceError):
                await _with_service(PasteboardService, setter(previous))


async def _press_button(name: str) -> None:
    from pymobiledevice3.remote.core_device.hid_service import (
        HID_BUTTON_STATE_DOWN,
        HID_BUTTON_STATE_UP,
        IndigoHIDService,
    )

    page, code = BUTTONS[name]
    hold = BUTTON_HOLD_S.get(name, DEFAULT_BUTTON_HOLD_S)

    async def play(service):
        async def release():
            await _maybe(service.send_button(page, code, HID_BUTTON_STATE_UP))

        async with _release_input(release):
            await _maybe(service.send_button(page, code, HID_BUTTON_STATE_DOWN))
            await asyncio.sleep(hold)

    await _with_service(IndigoHIDService, play)


async def _device_info() -> dict:
    from pymobiledevice3.lockdown import create_using_usbmux

    global _selected_serial
    serial, connection_type = await _pick_device()
    _selected_serial = serial
    lockdown = await _maybe(create_using_usbmux(serial=serial, connection_type=connection_type))
    try:
        return await _maybe(lockdown.all_values)
    finally:
        await _maybe(lockdown.close())


async def _describe_devices() -> list[dict]:
    """Every attached device with what decides whether it can be driven."""
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices

    transports: dict[str, set[str]] = {}
    for device in await _maybe(list_devices()):
        transports.setdefault(device.serial, set()).add(_connection_kind(device))

    described = []
    for serial, kinds in transports.items():
        entry = {"udid": serial, "transports": sorted(kinds), "reachable": False}
        try:
            lockdown = await _maybe(
                create_using_usbmux(
                    serial=serial, connection_type="USB" if "USB" in kinds else "Network"
                )
            )
        except Exception as exc:
            # Typically an unpaired device or a declined trust prompt.
            entry["problem"] = type(exc).__name__
        else:
            try:
                info = await _maybe(lockdown.all_values)
                entry.update(
                    reachable=True,
                    ios=info.get("ProductVersion"),
                    model=info.get("ProductType"),
                    device_class=info.get("DeviceClass"),
                )
                for key, getter in (
                    ("developer_mode", "get_developer_mode_status"),
                    ("wifi_connections", "get_enable_wifi_connections"),
                ):
                    try:
                        entry[key] = bool(await _maybe(getattr(lockdown, getter)()))
                    except Exception:
                        entry[key] = None
            finally:
                with contextlib.suppress(Exception):
                    await _maybe(lockdown.close())
        # Whether the input services are advertised is only known once a tunnel
        # is open, so this is the precondition, not a guarantee.
        entry["controllable"] = bool(entry["reachable"] and entry.get("developer_mode"))
        described.append(entry)
    return sorted(described, key=lambda entry: entry["udid"])


async def _set_wifi_connections(enabled: bool) -> None:
    from pymobiledevice3.lockdown import create_using_usbmux

    global _selected_serial
    serial, connection_type = await _pick_device()
    _selected_serial = serial
    lockdown = await _maybe(create_using_usbmux(serial=serial, connection_type=connection_type))
    try:
        await _maybe(lockdown.set_enable_wifi_connections(enabled))
    finally:
        await _maybe(lockdown.close())


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
    return _text(
        {
            "connected": True,
            "ios": info.get("ProductVersion"),
            "build": info.get("BuildVersion"),
            "model": info.get("ProductType"),
            "transports": _run(_list_transports()),
            "tunnel_open": _rsd is not None,
        }
    )


def tool_screenshot(arguments: dict) -> list[dict]:
    width = int(arguments.get("max_width", 900))
    data, mime = _to_jpeg(_run(_capture_png()), width)
    return [
        {
            "type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mimeType": mime,
        }
    ]


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
    typed, method = _run(_type_text(text), timeout=CALL_TIMEOUT_S + len(text) * 0.1)
    return _text({"ok": True, "action": "type", "characters": typed, "method": method})


def tool_list_devices(_arguments: dict) -> list[dict]:
    return _text({"devices": _run(_describe_devices())})


def tool_wifi(arguments: dict) -> list[dict]:
    enabled = bool(arguments.get("enabled", True))
    _run(_set_wifi_connections(enabled))
    return _text({"ok": True, "action": "wifi_connections", "enabled": enabled})


def tool_button(arguments: dict) -> list[dict]:
    name = str(arguments.get("name", "home"))
    if name not in BUTTONS:
        raise DeviceError(f"Unknown button: {name}. Available: {', '.join(sorted(BUTTONS))}.")
    _run(_press_button(name))
    return _text({"ok": True, "action": "button", "name": name})


def _app_entries(apps):
    if isinstance(apps, dict) and "apps" in apps:
        apps = apps["apps"]
    entries = apps.values() if isinstance(apps, dict) else apps
    for app in entries:
        if not isinstance(app, dict):
            continue
        bundle = app.get("bundleIdentifier") or app.get("bundleID") or ""
        name = app.get("name") or app.get("bundleName") or ""
        if isinstance(name, str) and isinstance(bundle, str) and bundle:
            yield name, bundle


def tool_open_app(arguments: dict) -> list[dict]:
    from pymobiledevice3.remote.core_device.app_service import AppServiceService

    wanted = str(arguments["app"]).strip()
    needle = wanted.lower()

    async def run(service):
        apps = await _maybe(service.list_apps())
        entries = list(_app_entries(apps))
        exact_bundle = [entry for entry in entries if needle == entry[1].lower()]
        exact_name = [entry for entry in entries if needle == entry[0].lower()]
        partial = [entry for entry in entries if needle in entry[0].lower()]
        matches = exact_bundle or exact_name or partial
        if not matches:
            raise DeviceError("No installed app matches the query.")
        if len(matches) != 1:
            raise DeviceError(
                "Ambiguous app name. Use an exact bundle identifier from iphone_list_apps."
            )
        selected = matches[0]
        await _maybe(service.launch_application(selected[1], kill_existing=False))
        return selected

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
        return sorted(found, key=lambda app: app["name"].lower())

    return _text({"apps": _run(_with_service(AppServiceService, run, retry_safe=True))})


def tool_wait(arguments: dict) -> list[dict]:
    milliseconds = max(0, min(10_000, int(arguments.get("ms", 500))))
    time.sleep(milliseconds / 1000.0)
    return _text({"ok": True, "action": "wait", "ms": milliseconds})


NORMALISED = {"type": "number", "minimum": 0, "maximum": 1}

TOOLS = [
    {
        "name": "iphone_status",
        "description": (
            "Device information and link state: iOS version, model, and which "
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
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 1400,
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
                "hold_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000,
                    "description": "Contact duration (default 120).",
                },
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
                "x": NORMALISED,
                "y": NORMALISED,
                "x1": NORMALISED,
                "y1": NORMALISED,
                "ms": {
                    "type": "integer",
                    "minimum": 120,
                    "maximum": 10000,
                    "description": "Gesture duration (default 300).",
                },
            },
            "required": ["x", "y", "x1", "y1"],
            "additionalProperties": False,
        },
        "handler": tool_swipe,
    },
    {
        "name": "iphone_type",
        "description": (
            "Type text into the focused field. Printable ASCII is typed with a virtual "
            "keyboard; any other text (accents, emoji) is pasted through the device "
            "pasteboard, which is restored afterwards when it held text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 1000}},
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
                "app": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "description": '"Settings" or com.apple.Preferences.',
                },
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
            "properties": {
                "contains": {
                    "type": "string",
                    "maxLength": 255,
                    "description": "Filter on the name.",
                }
            },
            "additionalProperties": False,
        },
        "handler": tool_list_apps,
    },
    {
        "name": "iphone_list_devices",
        "description": (
            "List every attached device with its UDID, transports, iOS version, model "
            "and Developer Mode, and whether it meets the preconditions to be driven. "
            "Does not open a tunnel or select a device."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_list_devices,
    },
    {
        "name": "iphone_wifi_connections",
        "description": (
            "Allow (or forbid) connecting to the selected device over the network. "
            "Run once while it is plugged in over USB."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean", "description": "Default true."}},
            "additionalProperties": False,
        },
        "handler": tool_wifi,
    },
    {
        "name": "iphone_wait",
        "description": "Pause, to let an animation settle before the next screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000,
                    "description": "Default 500; maximum 10000.",
                }
            },
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
    output = _protocol_output if _protocol_output is not None else sys.stdout
    output.write(json.dumps(message, allow_nan=False) + "\n")
    output.flush()


def _reply(request_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _fail(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _validate_arguments(name: str, arguments) -> dict:
    """Validate the small, flat JSON Schema subset used by this server."""
    schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
    if not isinstance(arguments, dict):
        raise DeviceError("Tool arguments must be an object.")
    if set(arguments) - set(schema["properties"]):
        raise DeviceError("Unexpected tool argument.")
    if set(schema.get("required", [])) - set(arguments):
        raise DeviceError("Missing required tool argument.")
    for key, value in arguments.items():
        spec = schema["properties"][key]
        kind = spec["type"]
        valid = (
            (kind == "string" and isinstance(value, str))
            or (kind == "integer" and type(value) is int)
            or (kind == "number" and type(value) in (int, float))
            or (kind == "boolean" and type(value) is bool)
        )
        if not valid:
            raise DeviceError(f"Invalid type for {key}; expected {kind}.")
        if kind in ("number", "integer"):
            if (isinstance(value, float) and not math.isfinite(value)) or not spec.get(
                "minimum", -math.inf
            ) <= value <= spec.get("maximum", math.inf):
                raise DeviceError(f"{key} is outside the supported range.")
        elif kind == "string":
            if (
                not spec.get("minLength", 0)
                <= len(value)
                <= spec.get("maxLength", MAX_REQUEST_BYTES)
            ):
                raise DeviceError(f"Invalid length for {key}.")
            if key == "app" and not value.strip():
                raise DeviceError("App query must not be blank.")
        if "enum" in spec and value not in spec["enum"]:
            raise DeviceError(f"Unsupported value for {key}.")
    return arguments


def handle(message) -> None:
    if not isinstance(message, dict):
        _fail(None, -32600, "Invalid Request")
        return
    request_id = message.get("id")
    if request_id is not None and type(request_id) not in (str, int):
        _fail(None, -32600, "Invalid Request")
        return
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        _fail(request_id, -32600, "Invalid Request")
        return
    # Never execute tools from notifications or reply to valid notifications.
    if "id" not in message:
        return
    if request_id is None:
        _fail(None, -32600, "MCP request IDs must be strings or integers.")
        return
    method = message["method"]
    params = message.get("params", {})
    if not isinstance(params, dict):
        _fail(request_id, -32602, "Parameters must be an object.")
        return

    if method == "initialize":
        if (
            not isinstance(params.get("protocolVersion"), str)
            or not isinstance(params.get("capabilities"), dict)
            or not isinstance(params.get("clientInfo"), dict)
        ):
            _fail(request_id, -32602, "Missing or invalid initialization parameters.")
            return
        _reply(
            request_id,
            {
                "protocolVersion": DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    elif method == "ping":
        _reply(request_id, {})
    elif method == "tools/list":
        _reply(request_id, {"tools": TOOL_SPECS})
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in HANDLERS:
            _fail(request_id, -32602, "Unknown tool.")
            return
        try:
            arguments = _validate_arguments(name, params.get("arguments", {}))
            content = HANDLERS[name](arguments)
            _reply(request_id, {"content": content, "isError": False})
        except DeviceError as exc:
            _reply(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception:
            # Do not disclose text, local paths or device metadata from dependencies.
            _reply(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "Device operation failed. Check the device and connection.",
                        }
                    ],
                    "isError": True,
                },
            )
    else:
        _fail(request_id, -32601, "Unsupported method.")


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _serve() -> None:
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_REQUEST_BYTES:
            _fail(None, -32600, "Request exceeds the 64 KiB limit; closing the connection.")
            break
        if not line.strip():
            continue
        try:
            message = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            _fail(None, -32700, "Parse error")
            continue
        try:
            handle(message)
        except Exception:
            _fail(None, -32603, "Internal error")


def main() -> None:
    global _protocol_output
    try:
        _configure()
    except DeviceError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    _protocol_output = sys.stdout
    try:
        # Keep dependency diagnostics off the JSON-RPC stream, including worker output.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                _serve()
            except (KeyboardInterrupt, BrokenPipeError):
                pass
            finally:
                _shutdown()
    finally:
        _protocol_output = None


if __name__ == "__main__":
    main()
