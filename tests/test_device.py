"""Regression tests with mocked device services; never send input to a phone."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ios_control_mcp import server


@pytest.fixture(autouse=True)
def reset_device_state(monkeypatch):
    monkeypatch.setattr(server, "UDID", "")
    monkeypatch.setattr(server, "TRANSPORT", "")
    monkeypatch.setattr(server, "_selected_serial", None)
    monkeypatch.setattr(server, "_rsd", None)
    monkeypatch.setattr(server, "_tunnel", None)
    monkeypatch.setattr(server, "_service_lock", None)


def devices(monkeypatch, rows):
    from pymobiledevice3 import usbmux

    monkeypatch.setattr(
        usbmux,
        "list_devices",
        AsyncMock(
            return_value=[
                SimpleNamespace(serial=serial, connection_type=transport)
                for serial, transport in rows
            ]
        ),
    )


async def test_pinned_device_never_falls_back(monkeypatch):
    devices(monkeypatch, [("other-device", "USB")])
    monkeypatch.setattr(server, "UDID", "requested-device")
    with pytest.raises(server.DeviceError, match="selected iPhone is unavailable"):
        await server._pick_device()


async def test_multiple_devices_require_selection(monkeypatch):
    devices(monkeypatch, [("one", "USB"), ("two", "USB")])
    with pytest.raises(server.DeviceError, match="Multiple devices"):
        await server._pick_device()


async def test_usb_preferred_for_same_phone(monkeypatch):
    devices(monkeypatch, [("one", "Network"), ("one", "USB")])
    assert await server._pick_device() == ("one", "USB")


async def test_selected_phone_stays_pinned(monkeypatch):
    devices(monkeypatch, [("two", "USB")])
    monkeypatch.setattr(server, "_selected_serial", "one")
    with pytest.raises(server.DeviceError):
        await server._pick_device()


@pytest.mark.parametrize("transport,expected", [("usb", "USB"), ("wifi", "Network")])
async def test_forced_transport(monkeypatch, transport, expected):
    devices(monkeypatch, [("one", "Network"), ("one", "USB")])
    monkeypatch.setattr(server, "TRANSPORT", transport)
    assert await server._pick_device() == ("one", expected)


async def test_status_uses_selected_device_and_closes_connection(monkeypatch):
    from pymobiledevice3 import lockdown

    devices(monkeypatch, [("one", "Network")])
    client = SimpleNamespace(all_values={"ProductType": "test"}, close=AsyncMock())
    create = AsyncMock(return_value=client)
    monkeypatch.setattr(lockdown, "create_using_usbmux", create)
    assert await server._device_info() == {"ProductType": "test"}
    create.assert_awaited_once_with(serial="one", connection_type="Network")
    client.close.assert_awaited_once()


def test_status_omits_personal_identifiers(monkeypatch):
    def run(coro):
        coro.close()
        return (
            {"DeviceName": "PRIVATE_NAME", "UniqueDeviceID": "PRIVATE_ID"}
            if coro.cr_code.co_name == "_device_info"
            else ["USB"]
        )

    monkeypatch.setattr(server, "_run", run)
    result = json.dumps(server.tool_status({}))
    assert "PRIVATE_NAME" not in result
    assert "PRIVATE_ID" not in result


@pytest.mark.parametrize("safe,expected_calls", [(False, 1), (True, 2)])
async def test_only_read_operations_can_retry(monkeypatch, safe, expected_calls):
    monkeypatch.setattr(server, "_open_tunnel", AsyncMock())
    close = AsyncMock()
    monkeypatch.setattr(server, "_close_tunnel", close)
    service = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    action = AsyncMock(side_effect=ConnectionError("private error"))
    with pytest.raises(server.DeviceError):
        await server._with_service(lambda _: service, action, retry_safe=safe)
    assert action.await_count == expected_calls
    assert service.close.await_count == expected_calls
    assert close.await_count == expected_calls


async def test_service_closed_when_connect_fails(monkeypatch):
    monkeypatch.setattr(server, "_open_tunnel", AsyncMock())
    service = SimpleNamespace(connect=AsyncMock(side_effect=OSError()), close=AsyncMock())
    with pytest.raises(server.DeviceError):
        await server._with_service(lambda _: service, AsyncMock())
    service.close.assert_awaited_once()


async def test_cancellation_invalidates_tunnel(monkeypatch):
    monkeypatch.setattr(server, "_open_tunnel", AsyncMock())
    close = AsyncMock()
    monkeypatch.setattr(server, "_close_tunnel", close)
    service = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await server._with_service(
            lambda _: service, AsyncMock(side_effect=asyncio.CancelledError())
        )
    close.assert_awaited_once()
    service.close.assert_awaited_once()


def fake_services(monkeypatch, pasteboard_text="previous text"):
    """Route _with_service to a fake pasteboard and keyboard; return their call logs."""
    from pymobiledevice3.remote.core_device.pasteboard_service import text_item

    pasted, chords = [], []
    pasteboard = SimpleNamespace(
        get=AsyncMock(
            return_value={"pasteboard": {"items": [text_item(pasteboard_text)]}}
            if pasteboard_text is not None
            else {"pasteboard": {"items": []}}
        ),
        set_text=AsyncMock(side_effect=pasted.append),
    )
    keyboard = SimpleNamespace(
        create_keyboard_service=AsyncMock(return_value=1),
        send_keyboard=AsyncMock(side_effect=lambda _, codes: chords.append(list(codes))),
    )

    async def with_service(factory, action, **_kwargs):
        name = factory.__name__
        return await action(pasteboard if name == "PasteboardService" else keyboard)

    monkeypatch.setattr(server, "_with_service", with_service)
    monkeypatch.setattr(server, "KEY_REPORT_DELAY_S", 0)
    monkeypatch.setattr(server, "PASTE_SETTLE_S", 0)
    return pasted, chords


async def test_ascii_text_is_typed_without_touching_pasteboard(monkeypatch):
    pasted, chords = fake_services(monkeypatch)
    assert await server._type_text("Hi") == (2, "keys")
    assert pasted == []
    assert [server.KEY_LEFT_SHIFT] in chords


async def test_unicode_text_is_pasted_and_pasteboard_restored(monkeypatch):
    from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID

    pasted, chords = fake_services(monkeypatch)
    assert await server._type_text("Merci, à demain ! 👋") == (19, "paste")
    assert pasted == ["Merci, à demain ! 👋", "previous text"]
    assert [server.KEY_LEFT_GUI, ASCII_TO_HID["v"][0]] in chords
    assert chords[-1] == []


async def test_pasteboard_without_text_is_not_restored(monkeypatch):
    pasted, _ = fake_services(monkeypatch, pasteboard_text=None)
    await server._type_text("é")
    assert pasted == ["é"]


async def test_keyboard_releases_shift_after_failure(monkeypatch):
    keyboard = SimpleNamespace(
        create_keyboard_service=AsyncMock(return_value=1),
        send_keyboard=AsyncMock(side_effect=[None, OSError(), None]),
    )

    async def with_service(factory, action):
        return await action(keyboard)

    monkeypatch.setattr(server, "_with_service", with_service)
    monkeypatch.setattr(server, "KEY_REPORT_DELAY_S", 0)
    with pytest.raises(OSError):
        await server._type_text("A")
    assert keyboard.send_keyboard.call_args.args == (1, [])


async def test_touch_releases_after_failure(monkeypatch):
    from pymobiledevice3.remote.core_device.hid_service import TOUCHSCREEN_STATE_RELEASE

    service = SimpleNamespace(send_touchscreen=AsyncMock(side_effect=[OSError(), None]))

    async def with_service(factory, action):
        return await action(service)

    monkeypatch.setattr(server, "_with_service", with_service)
    monkeypatch.setattr(server, "GESTURE_FLUSH_S", 0)
    with pytest.raises(OSError):
        await server._touch_path([(100, 200)], 0)
    assert service.send_touchscreen.call_args.args[0] == TOUCHSCREEN_STATE_RELEASE


async def test_button_releases_after_failure(monkeypatch):
    from pymobiledevice3.remote.core_device.hid_service import HID_BUTTON_STATE_UP

    service = SimpleNamespace(send_button=AsyncMock(side_effect=[OSError(), None]))

    async def with_service(factory, action):
        return await action(service)

    monkeypatch.setattr(server, "_with_service", with_service)
    with pytest.raises(OSError):
        await server._press_button("home")
    assert service.send_button.call_args.args[-1] == HID_BUTTON_STATE_UP


@pytest.mark.parametrize(
    "query,expected",
    [
        ("com.test.first", "com.test.first"),
        ("Second", "com.test.second"),
        ("Fir", "com.test.first"),
    ],
)
def test_app_resolution_does_not_kill_existing(monkeypatch, query, expected):
    service = SimpleNamespace(
        list_apps=AsyncMock(
            return_value=[
                {"name": "First", "bundleIdentifier": "com.test.first"},
                {"name": "Second", "bundleIdentifier": "com.test.second"},
            ]
        ),
        launch_application=AsyncMock(),
    )

    async def with_service(factory, action):
        return await action(service)

    monkeypatch.setattr(server, "_with_service", with_service)
    monkeypatch.setattr(server, "_run", asyncio.run)
    server.tool_open_app({"app": query})
    service.launch_application.assert_awaited_once_with(expected, kill_existing=False)


def test_ambiguous_app_does_not_launch(monkeypatch):
    service = SimpleNamespace(
        list_apps=AsyncMock(
            return_value=[
                {"name": "Test One", "bundleIdentifier": "com.test.one"},
                {"name": "Test Two", "bundleIdentifier": "com.test.two"},
            ]
        ),
        launch_application=AsyncMock(),
    )

    async def with_service(factory, action):
        return await action(service)

    monkeypatch.setattr(server, "_with_service", with_service)
    monkeypatch.setattr(server, "_run", asyncio.run)
    with pytest.raises(server.DeviceError, match="Ambiguous"):
        server.tool_open_app({"app": "test"})
    service.launch_application.assert_not_awaited()


def test_conversion_failure_returns_png_and_cleans_tempfiles(monkeypatch):
    paths = []

    def convert(command, **kwargs):
        from pathlib import Path

        paths.append(Path(command[-1]).parent)
        assert kwargs["timeout"] == 15
        raise OSError()

    monkeypatch.setattr(server.subprocess, "run", convert)
    assert server._to_jpeg(b"image bytes", 900) == (b"image bytes", "image/png")
    assert all(not path.exists() for path in paths)


def test_timeout_cancels_and_shutdown_releases_loop():
    finished = []

    async def slow():
        try:
            await asyncio.sleep(10)
        finally:
            finished.append(True)

    try:
        with pytest.raises(server.DeviceError, match="Timed out"):
            server._run(slow(), timeout=0.01)
    finally:
        server._shutdown()
    assert finished == [True]
    assert server._loop is None


async def test_release_failure_is_not_reported_as_success():
    release = AsyncMock(side_effect=OSError("release failed"))
    with pytest.raises(OSError, match="release failed"):
        async with server._release_input(release):
            pass
    release.assert_awaited_once()


async def test_release_failure_does_not_hide_action_error():
    release = AsyncMock(side_effect=OSError("release failed"))
    with pytest.raises(ValueError, match="action failed"):
        async with server._release_input(release):
            raise ValueError("action failed")
    release.assert_awaited_once()


async def test_failed_tunnel_handshake_restores_dependency(monkeypatch):
    from pymobiledevice3.remote import userspace_tunnel

    original = userspace_tunnel.create_using_usbmux
    tunnel = SimpleNamespace(aopen=AsyncMock(side_effect=OSError()), aclose=AsyncMock())
    monkeypatch.setattr(server, "_pick_device", AsyncMock(return_value=("one", "USB")))
    monkeypatch.setattr(userspace_tunnel, "UserspaceRsdTunnel", lambda **kwargs: tunnel)
    with pytest.raises(OSError):
        await server._open_tunnel()
    assert userspace_tunnel.create_using_usbmux is original
    tunnel.aclose.assert_awaited_once()
    assert server._rsd is None
    assert server._tunnel is None


@pytest.mark.parametrize("as_base64", [False, True])
async def test_capture_uses_only_png_image_field(monkeypatch, as_base64):
    import base64

    png = b"\x89PNG\r\n\x1a\nsynthetic image"
    service = SimpleNamespace(
        capture_screenshot=AsyncMock(
            return_value={
                "unrelated": b"private data",
                "image": base64.b64encode(png).decode() if as_base64 else png,
            }
        )
    )

    async def with_service(factory, action, *, retry_safe):
        assert retry_safe
        return await action(service)

    monkeypatch.setattr(server, "_with_service", with_service)
    assert await server._capture_png() == png


def test_real_macos_image_conversion():
    import io
    import sys

    from PIL import Image

    if sys.platform != "darwin":
        pytest.skip("sips is a macOS system utility")
    source = io.BytesIO()
    Image.new("RGB", (400, 800), color="white").save(source, format="PNG")
    data, mime = server._to_jpeg(source.getvalue(), 200)
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as converted:
        assert converted.size == (200, 400)
        assert converted.format == "JPEG"


async def test_devices_are_described_without_names(monkeypatch):
    from pymobiledevice3 import lockdown

    devices(
        monkeypatch, [("new", "USB"), ("new", "Network"), ("old", "Network"), ("locked", "USB")]
    )
    values = {
        "new": {"ProductVersion": "27.2", "ProductType": "iPhone99,1", "DeviceName": "PRIVATE"},
        "old": {"ProductVersion": "26.4", "ProductType": "iPad99,1"},
    }

    async def create(serial, connection_type):
        if serial == "locked":
            raise ConnectionRefusedError()
        return SimpleNamespace(
            all_values=values[serial],
            get_developer_mode_status=AsyncMock(return_value=True),
            get_enable_wifi_connections=AsyncMock(side_effect=OSError()),
            close=AsyncMock(),
        )

    monkeypatch.setattr(lockdown, "create_using_usbmux", create)
    described = {entry["udid"]: entry for entry in await server._describe_devices()}
    assert "PRIVATE" not in json.dumps(described)
    assert described["new"]["transports"] == ["Network", "USB"]
    assert described["new"]["controllable"] is True
    assert described["new"]["wifi_connections"] is None
    assert described["old"]["controllable"] is True
    assert described["locked"] == {
        "udid": "locked",
        "transports": ["USB"],
        "reachable": False,
        "problem": "ConnectionRefusedError",
        "controllable": False,
    }


async def test_wifi_connections_use_selected_device(monkeypatch):
    from pymobiledevice3 import lockdown

    devices(monkeypatch, [("one", "USB")])
    client = SimpleNamespace(set_enable_wifi_connections=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(lockdown, "create_using_usbmux", AsyncMock(return_value=client))
    await server._set_wifi_connections(True)
    client.set_enable_wifi_connections.assert_awaited_once_with(True)
    client.close.assert_awaited_once()
