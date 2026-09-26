"""
FastAPI app for Track A. Implements every endpoint in shared.contracts;
endpoints not yet built by their ticket are stubs marked with a TODO, but
they still return valid contract shapes. The app lifespan also starts and
stops the network monitor (monitor/net_monitor.py). Every response carries the
X-Contract-Version header. /docs and /redoc are disabled because they load
assets from a CDN, which would break the "no external calls" proof
(AGENTS.md rule 3); /openapi.json stays on since it is generated locally.
"""
from __future__ import annotations

import json
import logging
import subprocess
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import agent, prewarm
from backend.audit import read_audit_records, write_audit_record
from backend.net_probe import run_probe
from backend.file_store import file_store
from backend.registry import registry
from backend.llm_client import LLMError
from backend.tools import knowledge, office
from backend.tools.files import FileSafetyError
from backend.tools.sandbox import sandbox_available
from backend.router import route as run_router
from backend.settings import settings
from backend.task_store import task_store
from monitor import firewall
from monitor.net_monitor import monitor
from shared.contracts import (
    API_PREFIX,
    CONTRACT_VERSION,
    ERROR_CODES,
    ApiError,
    AuditRecord,
    ErrorInfo,
    EventsPage,
    FileRef,
    HealthResponse,
    KBSearchRequest,
    KBSearchResponse,
    KBStats,
    ModelInfo,
    NetworkStatus,
    PrewarmResult,
    ProbeRequest,
    ProbeResult,
    RouteDecision,
    RouteRequest,
    TaskCreate,
    TaskCreated,
    TaskState,
)

# Sovereign rule (AGENTS.md #3): never bind to anything but localhost. Checked
# at import time so the app refuses to start at all, not just log a warning.
if settings.WB_API_HOST == "0.0.0.0":
    raise RuntimeError(
        "Refusing to start: WB_API_HOST is '0.0.0.0' in .env. This server must bind to "
        "127.0.0.1 only (AGENTS.md rule 3: no external network exposure). "
        "Set WB_API_HOST=127.0.0.1 and restart."
    )

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


def _setup_logging() -> logging.Logger:
    log_dir = _resolve(settings.WB_LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("backend")
    log.setLevel(logging.INFO)
    if not log.handlers:  # avoid duplicate handlers if main.py is imported more than once
        handler = RotatingFileHandler(
            log_dir / "backend.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)
    return log


logger = _setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task_store.start(agent.run)
    logger.info("Task worker started")
    monitor.start()
    firewall.firewall_outbound_blocked()  # start the first (slow) firewall read in the background
    yield
    monitor.stop()
    task_store.stop()
    logger.info("Task worker stopped")


app = FastAPI(title="Sovereign AI Workbench API", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------------------------------------------------------------- error handling
def _error_response(status_code: int, code: str, message: Optional[str] = None, retryable: bool = False) -> JSONResponse:
    body = ApiError(error=ErrorInfo(code=code, message=message or ERROR_CODES.get(code, code), retryable=retryable))
    response = JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))
    response.headers["X-Contract-Version"] = CONTRACT_VERSION
    return response


def not_found(code: str, message: Optional[str] = None) -> HTTPException:
    """Raise this for a domain 404 (unknown task/file/artifact id)."""
    return HTTPException(status_code=404, detail={"code": code, "message": message or ERROR_CODES[code]})


@app.middleware("http")
async def _add_contract_version_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Contract-Version"] = CONTRACT_VERSION
    return response


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(422, "BAD_REQUEST", f"Request body failed validation: {exc.errors()}")


@app.exception_handler(StarletteHTTPException)
async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        code, message = detail["code"], detail.get("message")
    else:
        code = "INTERNAL" if exc.status_code >= 500 else "BAD_REQUEST"
        message = detail if isinstance(detail, str) else None
    return _error_response(exc.status_code, code, message)


@app.exception_handler(Exception)
async def _handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    return _error_response(500, "INTERNAL")


# ---------------------------------------------------------------- health / models
def _check_ollama() -> bool:
    try:
        resp = httpx.get(settings.OLLAMA_HOST, timeout=3.0)
        return resp.status_code < 500
    except Exception:
        return False


def _check_docker() -> bool:
    """Docker reachable AND the sandbox image present (same check the code flow relies on)."""
    try:
        return sandbox_available()
    except Exception:
        return False


def _check_tesseract() -> bool:
    try:
        proc = subprocess.run([str(settings.TESSERACT_CMD), "--version"], capture_output=True, timeout=5.0)
        return proc.returncode == 0
    except Exception:
        return False


def _kb_chunks() -> int:
    try:
        return knowledge.stats().chunks
    except Exception:
        logger.warning("knowledge base stats unavailable for /api/health", exc_info=True)
        return 0


@app.get(f"{API_PREFIX}/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    ollama_ok = _check_ollama()
    sandbox_ok = _check_docker()
    tesseract_ok = _check_tesseract()

    if ollama_ok and sandbox_ok and tesseract_ok:
        status = "ok"
    elif not ollama_ok and not sandbox_ok and not tesseract_ok:
        status = "down"
    else:
        status = "degraded"

    return HealthResponse(
        status=status,
        contract_version=CONTRACT_VERSION,
        mock=settings.WB_MOCK,
        ollama_ok=ollama_ok,
        sandbox_ok=sandbox_ok,
        tesseract_ok=tesseract_ok,
        kb_chunks=_kb_chunks(),
        models=registry.all_models(),
        time=datetime.now(timezone.utc),
    )


@app.get(f"{API_PREFIX}/models", response_model=list[ModelInfo])
async def get_models() -> list[ModelInfo]:
    return registry.all_models()


# ---------------------------------------------------------------- files
@app.post(f"{API_PREFIX}/files", response_model=FileRef)
async def post_files(file: UploadFile = File(...)) -> FileRef:
    content = await file.read()
    try:
        return file_store.save(file.filename or "upload", content, file.content_type)
    except FileSafetyError as exc:
        status_code = 413 if exc.code == "FILE_TOO_LARGE" else 415
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": str(exc)})


# ---------------------------------------------------------------- routing
@app.post(f"{API_PREFIX}/route", response_model=RouteDecision)
async def post_route(payload: RouteRequest) -> RouteDecision:
    return run_router(payload)


# ---------------------------------------------------------------- tasks
@app.post(f"{API_PREFIX}/tasks", response_model=TaskCreated, status_code=202)
async def post_tasks(payload: TaskCreate) -> TaskCreated:
    state = task_store.create(
        message=payload.message,
        file_ids=payload.file_ids,
        mode=payload.mode,
        scenario=payload.scenario,
    )
    return TaskCreated(task_id=state.task_id, status=state.status)


@app.get(f"{API_PREFIX}/tasks/{{task_id}}", response_model=TaskState)
async def get_task(task_id: str) -> TaskState:
    state = task_store.get(task_id)
    if state is None:
        raise not_found("TASK_NOT_FOUND")
    return state


@app.get(f"{API_PREFIX}/tasks/{{task_id}}/events", response_model=EventsPage)
async def get_task_events(task_id: str, after: int = Query(default=0, ge=0)) -> EventsPage:
    result = task_store.events(task_id, after)
    if result is None:
        raise not_found("TASK_NOT_FOUND")
    events, next_seq, done = result
    return EventsPage(task_id=task_id, events=events, next_seq=next_seq, done=done)


@app.post(f"{API_PREFIX}/tasks/{{task_id}}/cancel", response_model=TaskState)
async def post_task_cancel(task_id: str) -> TaskState:
    state = task_store.cancel(task_id)
    if state is None:
        raise not_found("TASK_NOT_FOUND")
    return state


# ---------------------------------------------------------------- stubs (finished by later tickets)
@app.get(f"{API_PREFIX}/artifacts/{{artifact_id}}")
async def get_artifact(artifact_id: str):
    if ".." in artifact_id or "/" in artifact_id or "\\" in artifact_id:
        raise HTTPException(
            status_code=400, detail={"code": "BAD_REQUEST", "message": "artifact_id must not contain path characters"}
        )
    found = office.artifact_path(artifact_id)
    if found is None:
        raise not_found("FILE_NOT_FOUND", f"no artifact with id {artifact_id!r}")
    path, artifact = found
    return FileResponse(path, media_type=office.media_type(artifact.kind), filename=artifact.filename)


# ---------------------------------------------------------------- network proof
def _net_headers(summary: dict) -> dict[str, str]:
    """Same facts as the optional NetworkStatus fields (contract 1.0.1); kept for older clients."""
    headers = {
        "X-Net-Since": summary["since"].isoformat(),
        "X-Net-Attempts-Since-Start": str(summary["attempts_since_start"]),
        "X-Net-Other-Apps-Since-Start": str(summary["other_apps_since_start"]),
        "X-Net-Probe-Since-Start": str(summary["probe_since_start"]),
    }
    if summary["error"]:
        headers["X-Net-Monitor-Error"] = summary["error"].encode("ascii", "replace").decode("ascii")[:300]
    return headers


@app.get(f"{API_PREFIX}/network/status", response_model=NetworkStatus)
def get_network_status(response: Response) -> NetworkStatus:
    response.headers.update(_net_headers(monitor.summary()))
    return monitor.status(firewall.firewall_outbound_blocked())


@app.post(f"{API_PREFIX}/network/probe", response_model=ProbeResult)
def post_network_probe(payload: ProbeRequest) -> ProbeResult:
    return run_probe(payload.target)


@app.get(f"{API_PREFIX}/kb/stats", response_model=KBStats)
def get_kb_stats() -> KBStats:
    return knowledge.stats()


@app.post(f"{API_PREFIX}/kb/search", response_model=KBSearchResponse)
def post_kb_search(payload: KBSearchRequest) -> KBSearchResponse:
    try:
        hits = knowledge.search(payload.query, payload.top_k)
    except LLMError as exc:
        status = 504 if exc.code == "MODEL_TIMEOUT" else 503
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc
    return KBSearchResponse(hits=hits)


@app.post(f"{API_PREFIX}/admin/prewarm", response_model=PrewarmResult)
def post_admin_prewarm(response: Response) -> PrewarmResult:
    """Loads the models and warms our caches (backend/prewarm.py). Never fails: bad items land in `failed`.
    Per-item times are in the warmed/failed labels; the X-Prewarm-* headers repeat them as JSON."""
    result, items, loaded = prewarm.run_prewarm()
    response.headers["X-Prewarm-Items"] = json.dumps([asdict(i) for i in items], ensure_ascii=True)
    response.headers["X-Prewarm-Loaded-Models"] = ",".join(loaded)
    write_audit_record(kind="system", name="prewarm", duration_ms=result.duration_ms, ok=not result.failed,
                       detail={"items": [asdict(i) for i in items], "loaded_models": loaded})
    return result


# ---------------------------------------------------------------- audit
@app.get(f"{API_PREFIX}/audit", response_model=list[AuditRecord])
async def get_audit(
    task_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[AuditRecord]:
    return read_audit_records(task_id=task_id, limit=limit)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.WB_API_HOST, port=settings.WB_API_PORT, reload=False)
