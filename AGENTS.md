# Rules for AI coding agents (Claude Code, Antigravity, others)

Project: Sovereign On-Premise Agentic AI Workbench (offline, Windows 11, CPU-only, 16 GB RAM).

## Hard rules (never break)
1. NEVER edit `shared/contracts.py`, `shared/fixtures/`, `.env.example` or `requirements.lock.txt`.
   If a change is needed, STOP and write the proposal in `docs/contract_change_requests.md`.
2. Only edit files inside your track's folders (see Ownership). Do not "fix" the other track's code.
3. NO network calls to anything except 127.0.0.1 / localhost. No CDN links, no pip installs at
   runtime, no model downloads, no telemetry. Never use ChromaDB's default embedding function.
4. Do not add dependencies. If one is truly needed, write it in `docs/dependency_requests.md`.
5. Import all API types from `shared.contracts`. Never redefine request/response models.
6. Read config only through `backend/settings.py` (Track A) or `ui/config.py` (Track B),
   which load `.env`. No hard-coded ports, paths or model names.
7. Every new function gets a pytest test in `tests/`. Run `pytest -q` before finishing.
8. Windows paths: use `pathlib.Path`, never string-join paths.
9. Keep functions small and typed. No async magic in the agent loop; plain threads only.

## Ownership
| Folder / file                         | Owner   |
|---------------------------------------|---------|
| backend/, sandbox/, config/, monitor/ | Track A |
| scripts/doctor.py, scripts/ingest.py, scripts/firewall_*.ps1 | Track A |
| templates/                            | Track A |
| ui/, mock/, demo/, docs/ppt/          | Track B |
| scripts/start_demo.ps1, scripts/e2e_run.py | Track B |
| shared/, tests/contract/, AGENTS.md   | BOTH (PR + both approve) |
| tests/track_a/ | Track A    tests/track_b/ | Track B |

## Commands
- Backend:  `uvicorn backend.main:app --host 127.0.0.1 --port 8000`
- Mock:     `uvicorn mock.mock_server:app --host 127.0.0.1 --port 8001`
- UI:       `streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501`
- Tests:    `pytest -q`
- Contract tests vs mock: `pytest tests/contract --base-url http://127.0.0.1:8001`
- Contract tests vs real: `pytest tests/contract --base-url http://127.0.0.1:8000`
- Health:   `python scripts/doctor.py`
