# Hardware validation

Automated tests use fake device services. They do not prove that developer
services, HID timing, orientation, or wireless pairing work on a real device.
Perform this checklist on a test phone with non-sensitive content before a
release. Do not run it unattended against a personal or production device.

Record the commit, macOS version, Python version, pymobiledevice3 version, iOS
version, device model, and transport. Do not record device names, serials, UDIDs,
pairing records, or screenshots of personal data.

1. Connect over USB, unlock, trust the Mac, and enable Developer Mode.
2. Initialize through an MCP client and confirm all nine tools are listed.
3. Read status and verify it matches the intended phone without exposing its name
   or UDID. With two devices, confirm selection requires `IOS_CONTROL_UDID`.
4. Capture the screen at widths 200, 900, and 1400. Check MIME type, readable
   content, orientation, and absence of persistent image artifacts.
5. In a harmless test app, tap a known target and scroll in both directions.
   Verify a long press and inspect the screen after every input action.
6. Type `Aa1! zZ9?` in a disposable text field. Verify case, punctuation, and that
   Shift is released. Test tab and newline only where they cannot submit data.
   Confirm unsupported Unicode is rejected without typing a partial prefix.
7. Check Home and volume buttons. Test lock, mute, and Siri only when their effects
   are appropriate, then unlock and restore the original state.
8. List apps and open a test app by bundle ID. Verify an ambiguous name fails and
   an already running app is not deliberately terminated.
9. Disconnect during a read, then reconnect. Confirm recovery. Disconnect during
   input in a disposable field and confirm the action is not replayed.
10. Enable wireless connections over USB, remove the cable, and repeat status,
    capture, tap, and typing on the same trusted network.
11. Lock the phone or make it unavailable and verify a bounded, understandable
    failure. Check the screen before retrying any action.
12. Close the MCP client and check that the server exits without a lingering
    process or open tunnel.

Mark each item passed, failed, or not run, with a short explanation. Hardware
validation is not claimed by the automated CI suite.
