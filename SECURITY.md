# Security policy

## Supported versions

Security fixes target the latest version on `main`. Older versions do not have
a separate maintenance branch.

## Reporting a vulnerability

Do not publish credentials, pairing records, device identifiers, screenshots,
or exploit details in a public issue. Use GitHub's **Report a vulnerability**
button under this repository's Security tab when private reporting is available.
If the option is unavailable, open an issue asking the maintainer to enable a
private reporting channel without including sensitive details.

Include the affected version, prerequisites, impact, and a minimal reproduction
using synthetic data. Remove personal paths and device data from diagnostics.

## Trust boundary

The server is intended for a local, trusted MCP client on a Mac paired with an
iPhone. It does not implement authentication, an HTTP service, or remote access
control. Do not expose its stdio stream through a public network bridge.

Tools can capture sensitive screen content, type into any focused field, trigger
system gestures, and open applications. The client and its model provider may
retain tool inputs and results. The server cannot determine whether an on-screen
action sends a message, confirms a purchase, or deletes data. Enforce approvals
in the client for actions with consequences.

The status tool omits the device name and UDID. App lists and screenshots remain
sensitive. Unsupported characters are rejected before typing; state-changing
operations are not automatically retried. A failed action can still have partial
effects. Best-effort input release cannot guarantee recovery from a severed link.

Screenshots are written to a private temporary directory for conversion and
removed on normal completion. Forced termination or an OS crash can leave files.
Pairing records belong to the underlying device tooling and must remain private.
Dependency diagnostics are redirected to stderr, not scrubbed of all possible
personal information; inspect and redact logs before sharing them.

## Repository hygiene

Never commit `.env` files, credentials, signing keys, pairing records, device
backups, screenshots, or local client configurations. Ignore rules are a guardrail;
they do not protect already tracked files or remove data from history.

CI runs tests, a dependency vulnerability audit, and a secret scan. Automated
checks do not guarantee the absence of vulnerabilities or private data. If a
credential was committed, revoke it and review all reachable refs and published
artifacts; deleting the current file alone does not remove the exposure.
