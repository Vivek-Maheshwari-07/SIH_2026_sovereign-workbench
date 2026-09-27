# Sovereign AI Workbench

An offline, on-premise AI workbench for confidential plant engineering work. Several local
open-weight models (served by Ollama on `127.0.0.1`) are routed per task and driven by an agent
that plans, calls local tools and produces real deliverables: Word notes and reports, PowerPoint
decks, Excel sheets and tested Python code. Nothing leaves the machine, and the Network screen
proves it.

## What it does

| Capability | How |
|---|---|
| Model auto-selection | 3-layer router (`backend/router.py`): rules (attachment type, keywords) → embedding similarity to examples → default. Models and rules live in `config/models.yaml`; add a model there, no code change. |
| Agent | `backend/agent.py`: route → JSON plan → one tool per step → finish, with step/time limits, repeat-call detection and a guided fallback for the three demo work orders. |
| Tools | `read_document` (PDF/scan/image/Word/Excel/text, OCR + vision fallback), `search_knowledge` (RAG), `answer_question` (grounded answer with `[n]` citations), `draft_approval_note` (Word), `extract_pid_tags` (P&ID → Excel), `run_code_task` (code + tests in the sandbox), `analyze_table` (CSV/Excel → pandas code in the sandbox → Excel), `inspect_image` (photo / handwriting with the vision model), `create_document` (Word report or PowerPoint deck). |
| Multimodal | Tesseract OCR with deskew, the vision model when OCR is poor, tiled P&ID reading, direct image questions. |
| Sandbox | Docker, `network_mode=none`, all capabilities dropped, memory/CPU/PID limits, non-root. |
| Knowledge base | ChromaDB + `bge-m3` embeddings from Ollama; Chroma's own (downloading) embedder is blocked by a guard. |
| Sovereign proof | Network monitor (NET-001 = core external connections, must be 0), firewall state, a "Try to reach Google" probe, audit log of every model call (all to `127.0.0.1`), `scripts/sovereign_proof.py`. |

## One-time setup (needs internet once, then never again)

1. Python 3.11, then `python -m venv .venv` and `.venv\Scripts\pip install -r requirements.lock.txt`.
2. [Ollama](https://ollama.com) and the models from `config/models.yaml`:
   `ollama pull qwen3.5:4b`, `ollama pull qwen2.5-coder:3b`, `ollama pull bge-m3`.
3. Tesseract OCR 5 at `C:\Program Files\Tesseract-OCR\tesseract.exe` (or set `TESSERACT_CMD`).
4. Docker Desktop, then build the sandbox image: `docker build -t wb-sandbox:1.0 sandbox`.
   For a machine that never goes online: `docker save wb-sandbox:1.0 -o offline_kit\wb-sandbox.tar`
   on a connected machine, then `docker load -i offline_kit\wb-sandbox.tar`.
5. `copy .env.example .env` and set `WB_API_URL=http://127.0.0.1:8000`.
6. Put your SOPs / manuals in `data/kb/` and run `.venv\Scripts\python scripts\ingest.py`.
7. Check everything: `.venv\Scripts\python scripts\doctor.py`.

## Run

One click (checks each dependency, starts backend + UI, prewarms models, opens the browser):

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_demo.ps1
```

Or by hand:

```
.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
.venv\Scripts\python -m streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501
```

Mock backend for UI work without models: `uvicorn mock.mock_server:app --host 127.0.0.1 --port 8001`
(and `WB_API_URL=http://127.0.0.1:8001`).

Before a sealed demo: `scripts\firewall_block.ps1` (blocks outbound traffic), afterwards
`scripts\firewall_restore.ps1`.

## Demo script (covers the problem statement's four demo points)

| Demo point | Click |
|---|---|
| (a) model auto-selection | Run WO-B (router → `qwen2.5-coder:3b`), then WO-A (→ `qwen3.5:4b`); the router card shows the rule and "Models used". |
| (b) end-to-end agentic task | Mode **Agent**, WO-A: scanned report → OCR → findings → SOP search → Word approval note. |
| (c) code verified in a sandbox | WO-B: code + tests written, run in the offline sandbox, `.py` files to download; or the "Analyse a table" example (CSV → pandas in the sandbox → Excel). |
| (d) multimodal | WO-C (P&ID drawing → Excel tag list) and the "Read handwriting" example (photo of a note → findings). |
| Proof of no external calls | Network screen: NET-001 = 0, press "Try to reach Google" (fails when sealed); Audit screen: every model call targets `127.0.0.1`. |

## Configuration and guards

All settings come from `.env` (see `.env.example`), read only through `backend/settings.py` and
`ui/config.py`. The backend refuses to start when `OLLAMA_HOST` or `WB_API_HOST` is not loopback,
and the UI refuses a non-loopback `WB_API_URL`. Only for a model server on an air-gapped plant LAN,
set `WB_ALLOW_NON_LOOPBACK=true`.

## Tests

`.venv\Scripts\python -m pytest -q` (fast suite; live-model tests need `--runslow`).
Contract tests: `pytest tests/contract --base-url http://127.0.0.1:8000` (or `:8001` for the mock).

## Docs

`docs/known_issues.md` (limitations), `docs/perf.md` (timings on the 16 GB CPU laptop),
`demo/SOURCES.md` (where every demo file comes from), `AGENTS.md` (rules for contributors).
