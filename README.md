# ios-control-mcp

Control a real iPhone from an MCP client on macOS. Capture screenshots, tap and
swipe, type text, press buttons, and open apps through Apple's CoreDevice
services, using [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

The server runs locally over **stdio**. It uses on-demand screenshots rather
than a continuous mirror, and requires no companion iOS app. USB is preferred;
Wi-Fi is available after pairing and enabling wireless connections.

**Status: beta.** Device services can change between iOS releases. Automated
tests cover protocol handling and mocked device behavior; they do not establish
compatibility with every iPhone or iOS version. See the
[hardware validation checklist](docs/VALIDATION.md) before relying on a new setup.

## Requirements

- **macOS**, including the built-in `/usr/bin/sips` image converter.
- **Python 3.10 or newer**. CI targets Python 3.10, 3.12, and 3.13.
- An iPhone with **iOS 17 or newer**, paired with this Mac, unlocked, and with
  **Developer Mode** enabled under Settings → Privacy & Security.
- Working developer services on the device. Depending on iOS and the host setup,
  developer disk image preparation may be necessary; consult the
  [upstream device setup documentation](https://github.com/doronz88/pymobiledevice3).

The iOS version is a transport prerequisite, not a promise that all tools work
on every supported OS release. Linux and Windows device control are not supported
by this project.

## Install

```bash
git clone https://github.com/als0m3/ios-control-mcp.git
cd ios-control-mcp
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

Connect the iPhone with a cable, unlock it, and accept the trust prompt if shown.
Enable Developer Mode and complete any restart or confirmation requested by iOS.

## Connect an MCP client

Add a stdio server to your client's MCP configuration, using the **absolute**
path to the installed executable:

```json
{
  "mcpServers": {
    "ios-control": {
      "command": "/absolute/path/to/ios-control-mcp/.venv/bin/ios-control-mcp",
      "args": [],
      "env": {
        "IOS_CONTROL_TRANSPORT": "usb"
      }
    }
  }
}
```

Configuration formats differ between clients; some also require `"type": "stdio"`.
Restart or reload the client after editing its configuration. The equivalent
module entry point is `.venv/bin/python -m ios_control_mcp`.

Start with `iphone_status`, then `iphone_screenshot`. The process waits for
newline-delimited JSON-RPC input when launched directly in a terminal; it does
not open a window or provide an interactive command prompt.

## Tools

All coordinates are **normalized**, from `0.0` to `1.0`: the top-left corner is
`(0, 0)` and the center is `(0.5, 0.5)`. Measure against a recent screenshot.
Pixel coordinates, incorrect types, unknown arguments, and values outside the
published limits are rejected before any device operation.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `iphone_status` | None | iOS version, build, model, available transports, tunnel state; omits the device name and UDID |
| `iphone_screenshot` | `max_width`: 200–1400, default 900 | Returns a JPEG resized to the requested width; falls back to PNG if conversion fails |
| `iphone_tap` | Required `x`, `y`; `hold_ms`: 0–10000, default 120 | Touch and release; contact lasts at least 100 ms |
| `iphone_swipe` | Required `x`, `y`, `x1`, `y1`; `ms`: 120–10000, default 300 | Interpolated drag; requested duration excludes contact and flush delays |
| `iphone_type` | Required `text`: 1–1000 characters | Printable ASCII, tab, and newline are typed with a US keyboard mapping. Any other text (accents, emoji, other scripts) is pasted: it is placed on the device pasteboard, Command-V is pressed, and the previous pasteboard is restored if it held text |
| `iphone_button` | Required `name` | `home`, `lock`, `volume-up`, `volume-down`, `mute`, or `siri` |
| `iphone_open_app` | Required `app`: 1–255 characters | Exact bundle ID, exact display name, or unique partial name; ambiguous matches are rejected |
| `iphone_list_apps` | Optional `contains`: up to 255 characters | Lists app names and bundle IDs; filter matches either field |
| `iphone_list_devices` | None | Every attached device: UDID, transports, iOS version, model, Developer Mode, wireless connections, and whether it meets the preconditions to be driven; opens no tunnel and omits device names |
| `iphone_wifi_connections` | `enabled`: boolean, default true | Allows or forbids network connections to the selected device; run once over USB |
| `iphone_wait` | `ms`: 0–10000, default 500 | Waits before the next operation |

Example arguments:

```json
{"x": 0.5, "y": 0.5}
```

```json
{"x": 0.5, "y": 0.75, "x1": 0.5, "y1": 0.25, "ms": 400}
```

```json
{"app": "com.apple.Preferences"}
```

A swipe beginning at the bottom edge can trigger the Home gesture. For scrolling
inside an app, start at `y < 0.9`. Keyboard layout, orientation, lock state, and
system dialogs can affect input. Newline may submit a form; tab may move focus.

Opening an app does not request termination of an already running instance.
Allow time for launch animations, then take another screenshot to verify the
result. A successful tool response confirms the request completed, not that the
screen reached the intended state.

## Device selection and configuration

| Environment variable | Meaning |
| --- | --- |
| `IOS_CONTROL_UDID` | Select one specific device. If it is absent, the call fails instead of controlling another device. |
| `IOS_CONTROL_TRANSPORT` | `usb` or `wifi`. If unset, prefer USB and use the network when USB is unavailable. |
| `IOS_CONTROL_TIMEOUT` | Base device-call timeout in seconds, from 1 to 300; default 60. Typing and swiping add a bounded duration allowance. |

With multiple devices attached, set `IOS_CONTROL_UDID`. With one device, the
server remembers its selection until restart. Keep actual UDIDs in local client
configuration, not in committed files or public issue reports.

Requests run sequentially. Screenshots and app listing can reconnect and retry
once after a connection failure. Input and app launching are **never automatically
replayed**. A timeout or connection error may mean an action partially completed;
inspect the screen before retrying it. Client cancellation notifications are not
processed during an in-flight call; configured timeouts bound device operations.

## Wi-Fi

With only the intended iPhone attached by USB, enable wireless connections:

```bash
.venv/bin/python -m pymobiledevice3 lockdown wifi-connections on
```

Keep the Mac and iPhone on the same trusted network. Unset
`IOS_CONTROL_TRANSPORT` for USB preference with network fallback, or set it to
`wifi` to require a network connection. The phone must remain unlocked and
reachable. To disable wireless connections, reconnect over USB and run the
command with `off` instead of `on`.

## Privacy and security

This server can read the visible screen and send input to the foreground app.
Only connect trusted MCP clients and keep their tool approval controls enabled
for consequential actions. Screenshots can include messages, credentials, and
other private content; the MCP client may send tool results to its model provider.

The server has no telemetry or HTTP listener. It sends tool results over local
stdio and communicates with the paired device. Image conversion uses temporary
files removed when conversion finishes normally; abrupt process termination can
leave files behind. The dependency manages pairing records and may download
developer support assets. Client logs and dependency diagnostics can contain
private information. See [SECURITY.md](SECURITY.md) before sharing a report.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No iPhone found | Cable, unlock state, trust pairing, and Developer Mode. For Wi-Fi, check wireless pairing and the network. |
| Selected device unavailable | Check `IOS_CONTROL_UDID` and transport. Restart the server to deliberately select a different phone. |
| Multiple devices found | Set `IOS_CONTROL_UDID` explicitly. |
| Developer service or tunnel error | Confirm developer services are ready, check upstream setup requirements, and restart the client after fixing the connection. |
| Input completes but nothing changes | Confirm focus, normalized coordinates, keyboard layout, and absence of a blocking system dialog. |
| Pasted text does not appear | The focused field must accept pasting. Fields that block it, such as some password or code fields, stay empty. |
| App name is ambiguous | Use the exact bundle ID returned by `iphone_list_apps`. |
| Device action times out | Inspect the current screen before retrying; the action may already have partly completed. |
| Client cannot start the server | Check its absolute executable path and environment variables. Inspect stderr locally; redact before sharing. |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks,
[architecture](docs/ARCHITECTURE.md) for the transport design, and
[CHANGELOG.md](CHANGELOG.md) for behavior changes.

## License

The code in this repository is licensed under the [MIT License](LICENSE).
Dependencies retain their own licenses. In particular,
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3/blob/master/LICENSE)
is GPL-3.0-or-later; the MIT license here does not relicense that dependency.
Review third-party license obligations when redistributing a combined package.

This is an independent project, not affiliated with or endorsed by Apple.
