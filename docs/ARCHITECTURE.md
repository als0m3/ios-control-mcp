# Architecture

The executable accepts newline-delimited JSON-RPC 2.0 on stdin and writes only
protocol responses to stdout. Third-party diagnostic output goes to stderr.
The server negotiates MCP protocol version `2024-11-05`; it returns that supported
version when a client requests a different version, rather than claiming
support for an unknown revision. It exposes tools only: no resources, prompts,
HTTP transport, background tasks, or streaming progress.

Requests execute sequentially. Incoming messages are limited to 64 KiB. Invalid
JSON, malformed requests, unsupported methods, and unknown tools return protocol
errors. Tool validation and device failures return `isError: true`. The schema
validator implements only the flat types, ranges, lengths, required properties,
and enumerations used by this project's tool definitions. Extend it and its
tests if the schema shape changes.

## Device lifecycle

`pymobiledevice3` is asynchronous. A dedicated event loop in a daemon thread
owns the tunnel and device services. The stdio thread waits for each operation
with a bounded timeout. A lock serializes service operations while a cancelled
call finishes cleaning up.

Device discovery requires explicit selection if multiple distinct devices are
attached. Once selected, the serial stays pinned for the process lifetime;
transport preference is applied only to that device. Status uses the same
selection rules and closes its lockdown connection after reading values.

A `UserspaceRsdTunnel` is reused between calls. During the handshake, the
upstream module's `create_using_usbmux` reference is temporarily wrapped to
forward the selected connection type, because the tunnel constructor does not
accept it. The reference is restored in `finally`. This is an integration detail
to recheck when upgrading pymobiledevice3; the server is designed to run as its
own process, not as a general-purpose concurrent device library.

Each operation connects a service, runs its action, and closes the service even
when connection or action fails. Read-only screenshot and app-list operations
may retry once using a new tunnel. Touch, keyboard, buttons, and application
launches do not retry automatically. Failures and cancellation invalidate the
tunnel; EOF or interruption triggers bounded cleanup of worker tasks and the loop.

| Operation | Upstream service |
| --- | --- |
| Screenshot | `ScreenCaptureService` |
| Touch and keyboard | `UniversalHIDServiceService` |
| Buttons | `IndigoHIDService` |
| App listing and launch | `AppServiceService` |
| Status | Lockdown over usbmux |

## Screenshots and input

A screenshot is captured on demand as PNG, then converted with `/usr/bin/sips`
in a temporary directory. Conversion has a 15-second timeout and falls back to
PNG on failure. No continuous capture stream is maintained. Resizing requests
an exact width, so a source narrower than the requested width can be upscaled.

Touch uses repeated initial contact and a short flush interval. Keyboard input
uses separate modifier reports and a US ASCII mapping. Other text is set on
the device pasteboard (one request per pasteboard connection, as the daemon
answers only one) and pasted with Command-V; text previously on the pasteboard
is put back afterwards. Touch, keyboard, and
button releases are attempted in `finally` blocks. These timings are device
integration heuristics, not platform guarantees; hardware validation remains
necessary. A disconnected device may never receive a final release report.

## Limits

The synchronous dispatcher does not process client cancellation while waiting
for a tool. Device timeouts cancel scheduled work; cleanup can take additional
time, and client timeouts should allow for connection setup and input duration.
Screenshot conversion is a separate bounded subprocess. UI actions cannot be
rolled back, and success does not prove the intended UI state was reached.

Protocol references:
[initialization and negotiation](https://modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle),
[tools and errors](https://modelcontextprotocol.io/specification/2024-11-05/server/tools).
