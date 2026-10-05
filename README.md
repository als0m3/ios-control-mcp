# ios-control-mcp

An [MCP](https://modelcontextprotocol.io) server that lets a model see and drive
a real iPhone from macOS — screenshots, taps, swipes, typing, hardware buttons
and app launching — over the same CoreDevice transport Xcode uses.

No companion app on the Mac, nothing installed on the phone, and no live mirror
window. Works over USB or wirelessly.

```
iphone_screenshot  →  1320 × 2868, taken on demand
iphone_tap         →  normalised coordinates, 0.0 – 1.0
iphone_open_app    →  launched by bundle identifier
```

## Why screenshots instead of a live mirror

The usual way to see an iPhone screen on a Mac is to capture it as a continuous
video stream through CoreMediaIO — the device QuickTime offers as a camera.
That path has a defect worth knowing about before you build on it: after a few
minutes of sustained capture the device stops delivering frames, while the
session keeps reporting itself as healthy. Callers then act on a frozen picture
with no error to tell them apart.

The condition cannot be recovered from inside the process. Reattaching the
input, rebuilding the capture session from scratch and releasing every retained
buffer all leave it frozen, and a minimal consumer that retains nothing
reproduces it identically — so it is not a matter of how the frames are
handled. Only a new process restores capture.

Taking one screenshot at a time keeps no stream alive, so the failure cannot
occur. Two details come for free: the capture is at native resolution rather
than a downscaled video frame, and the status bar is the real one — screen
recording substitutes a placeholder.

## Requirements

- macOS, with the `sips` command (ships with the system)
- Python 3.10 or later
- An iPhone that is **paired**, **unlocked**, and has **Developer Mode** enabled
  (Settings → Privacy & Security → Developer Mode)

Developed and tested against iOS 27.2 on an iPhone 16 Pro Max. The CoreDevice
transport requires iOS 17 or later; earlier versions are untested.

## Install

```bash
git clone https://github.com/Als0m3/ios-control-mcp.git
cd ios-control-mcp
python3 -m venv .venv
.venv/bin/pip install .
```

## Configure your MCP client

Point the client at the installed entry point. For Claude Code, in
`~/.claude.json`:

```json
{
  "mcpServers": {
    "ios-control": {
      "type": "stdio",
      "command": "/absolute/path/to/ios-control-mcp/.venv/bin/ios-control-mcp",
      "args": []
    }
  }
}
```

Any MCP client works the same way — the server speaks JSON-RPC 2.0 over stdio.

## Tools

| Tool | Arguments | Notes |
| --- | --- | --- |
| `iphone_status` | — | Name, iOS version, model, available transports |
| `iphone_screenshot` | `max_width` | JPEG, downscaled from the native capture |
| `iphone_tap` | `x`, `y`, `hold_ms` | Coordinates 0.0 – 1.0 |
| `iphone_swipe` | `x`, `y`, `x1`, `y1`, `ms` | Coordinates 0.0 – 1.0 |
| `iphone_type` | `text` | ASCII only; types into the focused field |
| `iphone_button` | `name` | `home`, `lock`, `volume-up`, `volume-down`, `mute`, `siri` |
| `iphone_open_app` | `app` | Display name or bundle identifier |
| `iphone_list_apps` | `contains` | Installed apps with bundle identifiers |
| `iphone_wait` | `ms` | Let an animation settle before the next capture |

### Coordinates

Touch coordinates are **normalised from 0.0 to 1.0**, not pixels: `x = 0.5` is
the horizontal centre whatever the device. Passing pixel values is rejected with
an explicit message rather than silently tapping the corner.

### Gestures near the bottom edge

A swipe that starts at the very bottom of the screen is read by iOS as the home
gesture and leaves the current app. Start above `y = 0.9` to stay inside it.

## Wireless

Enable wireless connections once, with the device plugged in:

```bash
.venv/bin/python -m pymobiledevice3 lockdown wifi-connections on
```

This is the same setting as *Show this iPhone when on Wi-Fi* in the Finder, and
it is reversible with `off`. The phone then appears over both transports; USB is
preferred automatically and the network is used when no cable is attached.

Expect roughly 2.3 s per screenshot over the network against 1.6 s over USB. The
tunnel is opened once and reused, so the difference is image transfer alone.

Keep in mind that the phone must stay unlocked, which is more demanding
wirelessly since it is not charging.

## Configuration

| Variable | Effect |
| --- | --- |
| `IOS_CONTROL_UDID` | Pin a device when several are attached |
| `IOS_CONTROL_TRANSPORT` | `usb` or `wifi`; defaults to USB with network fallback |
| `IOS_CONTROL_TIMEOUT` | Per-call timeout in seconds (default 60) |

## Troubleshooting

**“No iPhone found.”** Plug the device in and unlock it. If it has never been
paired with this Mac, accept the trust prompt on the phone first.

**Taps return `ok` but nothing happens.** Check the coordinates are normalised.
A value above 1.0 is rejected, but a plausible-looking wrong value is not — take
a screenshot and measure against it.

**Nothing happens after `iphone_open_app`.** A cold start takes a moment; wait
two to three seconds before the next screenshot.

**Calls fail after the device has been idle.** The first call rebuilds the
tunnel, which can take a few seconds. The server retries once on its own.

## How it works

The server holds one `UserspaceRsdTunnel` to the device and opens a CoreDevice
service per call:

- `ScreenCaptureService` for screenshots
- `UniversalHIDServiceService` for touch and keyboard
- `IndigoHIDService` for hardware buttons
- `AppServiceService` for launching and listing apps

Two details matter for input to land reliably. A lone touch report is discarded
by the digitizer as a spurious reading, so the initial contact is sent twice;
and closing the HID service immediately after the release cancels the gesture
before it reaches the phone, so the service is held briefly open.

All device work runs on an asyncio loop in a daemon thread, since the MCP loop
itself is synchronous. A dropped link is retried once on a fresh tunnel.

## License

MIT — see [LICENSE](LICENSE).
