"""
FastAPI app for Track A. Implements every endpoint in shared.contracts;
endpoints not yet built by their ticket are stubs marked with a TODO, but
they still return valid contract shapes. Every response carries the
X-Contract-Version header. /docs and /redoc are disabled because they load
assets from a CDN, which would break the "no external calls" proof
(AGENTS.md rule 3); /openapi.json stays on since it is generated locally.
"""
from __future__ import annotations

import logging
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import echo_agent
from backend.file_store import file_store
from backend.registry import registry
from backend.tools.files import FileSafetyError
from backend.tools.sandbox import sandbox_available
from backend.router import route as run_router
from backend.settings import settings
from backend.task_store import task_store
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
    task_store.start(echo_agent.run)
    logger.info("Task worker started")
    yield
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
        kb_chunks=0,  # TODO(A6): real chunk count once the knowledge base exists
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
    # TODO(A7): real artifact storage. Every id is unknown until that ticket lands.
    raise not_found("FILE_NOT_FOUND", "Artifact storage is not implemented yet (ticket A7).")


@app.get(f"{API_PREFIX}/network/status", response_model=NetworkStatus)
async def get_network_status() -> NetworkStatus:
    # TODO(A9): real connection snapshot via psutil + firewall-rule check.
    return NetworkStatus(
        checked_at=datetime.now(timezone.utc),
        external_count=0,
        external_seen_since_start=0,
        total_connections=0,
        firewall_outbound_blocked=None,  # None = "could not read", which is honest here: nothing checked it
        connections=[],
    )


@app.post(f"{API_PREFIX}/network/probe", response_model=ProbeResult)
async def post_network_probe(payload: ProbeRequest) -> ProbeResult:
    # TODO(A9): real outbound probe + firewall proof.
    #
    # Design choice: this endpoint does NOT attempt any network call at all
    # (reachable is always False, duration_ms is always 0) rather than faking
    # a "we tried and it failed" probe. The contract's ProbeResult has an
    # `error: Optional[str]` field, so "not implemented yet" is expressed
    # there in plain text — that's the signal the UI (or a human) should read
    # as "no real probe ran" rather than "we probed and confirmed isolation".
    return ProbeResult(
        target=payload.target,
        reachable=False,
        error="Not implemented yet (ticket A9): no network probe was attempted.",
        duration_ms=0,
    )


@app.get(f"{API_PREFIX}/kb/stats", response_model=KBStats)
async def get_kb_stats() -> KBStats:
    # TODO(A6): real ChromaDB-backed stats.
    return KBStats(documents=0, chunks=0, embed_model=registry.embedding_model().ollama_name)


@app.post(f"{API_PREFIX}/kb/search", response_model=KBSearchResponse)
async def post_kb_search(payload: KBSearchRequest) -> KBSearchResponse:
    # TODO(A6): real ChromaDB similarity search.
    return KBSearchResponse(hits=[])


@app.post(f"{API_PREFIX}/admin/prewarm", response_model=PrewarmResult)
async def post_admin_prewarm() -> PrewarmResult:
    # TODO(A10): real prewarm (load every model into RAM via a tiny call each).
    return PrewarmResult(warmed=[], failed=[], duration_ms=0)


# ---------------------------------------------------------------- audit
def _read_audit_log(*, task_id: Optional[str], limit: int) -> list[AuditRecord]:
    log_path = _resolve(settings.WB_LOG_DIR) / "audit.jsonl"
    if not log_path.exists():
        return []

    records: list[AuditRecord] = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(AuditRecord.model_validate_json(line))
            except Exception:
                continue  # skip a malformed line rather than failing the whole request

    if task_id is not None:
        records = [r for r in records if r.task_id == task_id]

    return list(reversed(records[-limit:]))  # most recent first


@app.get(f"{API_PREFIX}/audit", response_model=list[AuditRecord])
async def get_audit(
    task_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[AuditRecord]:
    return _read_audit_log(task_id=task_id, limit=limit)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.WB_API_HOST, port=settings.WB_API_PORT, reload=False)
