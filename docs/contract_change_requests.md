# Contract change requests

Per AGENTS.md rule 1: `shared/contracts.py` is never edited directly. Changes
are proposed here first and need agreement from both devs before a shared PR
touches the contract.

## Open requests

### Add tif/tiff to allowed uploads

**Requested by:** Track A (ticket A4 follow-up)
**Status:** open, needs Dev B agreement

Request: add tif/tiff to allowed uploads, because scanners often save TIFF.
Needs agreement from Dev B.

## Approved / applied

### 1.0.1: network proof fields on NetworkStatus and Connection (after A9)

**Requested by:** Track A (ticket A9)
**Status:** approved by the owner of both tracks, applied 2026-09-26. CONTRACT_VERSION 1.0.0 -> 1.0.1.

**Change (only NEW OPTIONAL fields; nothing renamed or removed):**

- `NetworkStatus.since: Optional[datetime]`: when the network monitor started; the start of every `*_since_start` count.
- `NetworkStatus.attempts_since_start: Optional[int]`: unique connection attempts (SYN_SENT / SYN_RECV) that never connected, e.g. blocked by the firewall. Shown, never counted as leaks.
- `NetworkStatus.other_apps_since_start: Optional[int]`: unique established external connections of other apps on the laptop (browser, OneDrive, ...). Information only.
- `NetworkStatus.probe_since_start: Optional[int]`: unique connections made by `POST /network/probe` itself.
- `NetworkStatus.monitor_error: Optional[str]`: last psutil error (e.g. permissions); `None` = monitor healthy.
- `Connection.group: Optional["established" | "attempt"]`.
- `Connection.origin: Optional["ours" | "other_app" | "probe"]`: "ours" = backend and its children, Ollama, Streamlit, Docker Desktop / WSL.
- `Connection.first_seen: Optional[datetime]`: when the monitor first saw this (pid, remote ip, remote port, group).
- Comment on `Connection.external` updated: only loopback is local; LAN addresses count as external (strict mode for the laptop demo). Code already worked this way.

**Why:** A9's monitor tells apart real leaks (our processes, established), attempts that the firewall blocked, other apps and the probe's own connection. In 1.0.0 the headline `external_seen_since_start` could only be explained through `X-Net-*` response headers and a "[ours]" / "[other app]" / "[probe]" suffix in `Connection.process`. The UI needs these as typed fields to show why the headline is 0 while the connection list is not empty.

**Compatibility:** every new field defaults to `None`, so a 1.0.0 payload (e.g. the mock) still validates. The backend keeps the `X-Net-*` headers and the process-name suffix for now. The UI shows its version banner until it is updated to 1.0.1.

**Version:** patch bump (1.0.0 -> 1.0.1), per rule 2 in `shared/contracts.py`: new optional fields only.

### 1.0.2: split "ours" into core and platform (after the A9 leak investigation)

**Requested by:** Track A (A9 follow-up)
**Status:** approved by the owner of both tracks, applied 2026-09-26. CONTRACT_VERSION 1.0.1 -> 1.0.2
(patch bump per rule 2: new optional fields only).

**Investigation result:** the discovery run showed 5 "leaks" in `external_seen_since_start`. All 5 came from
Docker Desktop's own background service (`com.docker.backend.exe`): update checks and usage statistics to
AWS and Cloudflare. It was counted as "ours" only because its process NAME was on the workbench list. Our
backend, Ollama and the sandbox made 0 external connections. Docker Desktop is not workbench code, but it
runs our sandbox, so it must be shown honestly rather than hidden or silently dropped.

**Change (only NEW OPTIONAL fields; nothing renamed or removed):**

- `Connection.component: Optional["core" | "platform"]`, set only when `origin == "ours"`:
  - `core`: the backend process and its whole child pid tree, Ollama (`ollama.exe`, `ollama app.exe`,
    `ollama_llama_server`), and the Streamlit UI. This is the sovereign proof.
  - `platform`: Docker Desktop and the WSL host services (`com.docker.*`, `Docker Desktop`, `vpnkit`,
    `wsl*`, `vmmem*`). Shown and counted separately.
  - A `python.exe` outside the backend's pid tree (for example an IDE language server) is `other_app`.
- `NetworkStatus.platform_seen_since_start: Optional[int]`: unique established platform connections.
- `NetworkStatus.platform_attempts_since_start: Optional[int]`: unique platform attempts (these are also
  inside `attempts_since_start`).
- Meaning of existing fields, comments updated: `external_count` and `external_seen_since_start` now
  count CORE connections only (group "established"). Platform, other apps, probe and attempts are excluded.

**Audit:** a new core external connection is written as `name="external_connection"`, `ok=false` (LEAK).
A new platform connection is written as `name="platform_connection"`, `ok=false` when established, with
`detail.component="platform"` and label "ours: platform", so both stay visible and are easy to filter.

**Compatibility:** 1.0.0 and 1.0.1 payloads still validate (new fields default to `None`). The backend keeps
the `X-Net-*` headers. `Connection.process` now ends in "[ours: core]" / "[ours: platform]" instead of "[ours]".
