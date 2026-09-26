# Sovereign network proof: **PASS**

| Item | Value |
|---|---|
| Started | 2026-09-26 12:25:39 India Standard Time |
| Finished | 2026-09-26 12:31:30 India Standard Time |
| Computer | Vivek |
| Wi-Fi | disconnected |
| Backend | http://127.0.0.1:8000 |
| Firewall outbound blocked | before True, after True |

## Checks

| Check | Result | Detail |
|---|---|---|
| Firewall outbound blocked | PASS | before=True, after=True |
| Probe https://www.google.com unreachable | PASS | reachable=False, error=OSError: DNS lookup for www.google.com failed: [Errno 11001] getaddrinfo failed |
| Probe https://8.8.8.8 unreachable | PASS | reachable=False, error=OSError: [WinError 10065] A socket operation was attempted to an unreachable host |
| Scenario inspection_note succeeded | PASS | status=succeeded |
| Scenario code_calc succeeded | PASS | status=succeeded |
| Scenario pid_tags succeeded | PASS | status=succeeded |
| Core external_seen_since_start = 0 (before) | PASS | 0 |
| Core external_seen_since_start = 0 (after) | PASS | 0 |

## Probes (the backend tries to reach the internet on purpose)

| Target | Reachable | Error | Duration ms |
|---|---|---|---|
| https://www.google.com | False | OSError: DNS lookup for www.google.com failed: [Errno 11001] getaddrinfo failed | 12 |
| https://8.8.8.8 | False | OSError: [WinError 10065] A socket operation was attempted to an unreachable host | 22 |

## Guided scenarios

| Scenario | Task | Status | Time (s) | Artifacts | Error |
|---|---|---|---|---|---|
| inspection_note | t_7a42816e383e | succeeded | 133.1 | inspection_note__INSP-2026-003_approval_note.docx | - |
| code_calc | t_c2bb8e2e278b | succeeded | 78.4 | code_calc__solution_a64c5d.py, code_calc__test_solution_a64c5d.py | - |
| pid_tags | t_fb0e128030e1 | succeeded | 136.5 | pid_tags__pid_tags_d968a009.xlsx | - |

## Network counters (before / after)

| Counter | Before | After | Meaning |
|---|---|---|---|
| external_count | 0 | 0 | Core external connections open right now (backend + its children, Ollama, UI). |
| external_seen_since_start | 0 | 0 | Unique core external connections since the backend started. The headline number: must be 0. |
| attempts_since_start | 0 | 0 | Unique outbound attempts that never connected (SYN_SENT). Not leaks. |
| platform_seen_since_start | 1 | 1 | Docker Desktop / WSL host services that connected out. Not workbench code; counted apart from core. |
| platform_attempts_since_start | 0 | 0 | Docker Desktop / WSL attempts that never connected (also inside attempts_since_start). |
| other_apps_since_start | 19 | 19 | Other programs on this laptop (browser, updates). Info only. |
| probe_since_start | 0 | 0 | Connections made on purpose by /network/probe (the 'try to reach the internet' button). |
| monitor_error | - | - | Last psutil error; '-' = monitor healthy. |

Only the **core** counters decide the proof. Platform (Docker Desktop / WSL), other apps, attempts and probe connections are shown for transparency but are not workbench traffic.

## Network audit records since the run started

| Time | Name | Target | OK | Detail |
|---|---|---|---|---|
| 2026-09-26T06:55:39.447443Z | probe | https://www.google.com | True | `{"reachable": false, "error": "OSError: DNS lookup for www.google.com failed: [Errno 11001] getaddrinfo failed", "resolved": [], "port": 443}` |
| 2026-09-26T06:55:39.486604Z | probe | https://8.8.8.8 | True | `{"reachable": false, "error": "OSError: [WinError 10065] A socket operation was attempted to an unreachable host", "resolved": ["8.8.8.8"], "port": 443}` |

Raw API responses: `raw.json`. Evidence card: `summary.png`. Screen: `screenshot.png`.
