# Changelog

## Unreleased

### Fixed

- Require explicit selection with multiple devices; never fall back from a
  requested or previously selected phone to another device.
- Apply device selection to status and close its lockdown connection.
- Retry only read-only operations, avoiding duplicated input after a link error.
- Validate tool arguments, bound resource use, and reject unsupported text before
  sending any keyboard input.
- Attempt input release on failures, clean up failed services and tunnels, and
  shut down worker resources when the client disconnects.
- Reject ambiguous app names and avoid requesting termination of running apps.
- Return protocol errors for malformed input, negotiate a supported MCP version,
  and avoid disclosing raw dependency exceptions in responses.
- Bound screenshot conversion and retain a PNG fallback on conversion failure.
- Keep dependency output off the MCP protocol stream and omit device names and
  UDIDs from status responses.

### Added

- Regression tests for protocol validation, device selection, retries, cleanup,
  keyboard input, app resolution, and screenshot conversion.
- CI for linting, tests, packaging, dependency auditing, and history secret scans.
- English installation, security, architecture, contribution, and hardware
  validation documentation.

### Compatibility notes

- Inputs that were previously coerced, ignored, or silently truncated can now
  fail validation. Unsupported typing characters reject the entire request.
- App listing no longer silently truncates results to 200 entries.
- Status no longer returns `name` or `udid`.
- Input and app-launch failures require inspection before an explicit retry.

## 1.0.0

Initial implementation of nine stdio MCP tools for screenshots, touch, keyboard,
hardware buttons, app launching, app listing, status, and waiting.
