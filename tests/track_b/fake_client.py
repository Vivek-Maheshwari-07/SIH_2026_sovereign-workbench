"""
Fake ApiClient for the Track B AppTest suites: same method names and ApiResult/PollResult returns,
records every call, and plays a scripted event stream back one poll at a time.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from shared.contracts import (
    CONTRACT_VERSION,
    AgentEvent,
    Artifact,
    ArtifactKind,
    ErrorInfo,
    EventType,
    FileRef,
    HealthResponse,
    NetworkStatus,
    PrewarmResult,
    RouteDecision,
    TaskCreate,
    TaskCreated,
    TaskMode,
    TaskState,
    TaskStatus,
)
from ui.api_client import ApiResult, DownloadedFile, PollResult

BASE_URL = "http://127.0.0.1:8000"
TASK_ID = "t_0123456789ab"
T0 = datetime(2026, 9, 26, 8, 0, 0, tzinfo=timezone.utc)
V = CONTRACT_VERSION

ROUTE = RouteDecision(task_type="coding", model_id="coder", ollama_name="qwen2.5-coder:3b",
                      reason="Rule: Coding keywords -> code specialist model (matched 'function')",
                      layer="rule", confidence=1.0)
PLAN = [{"index": 1, "title": "Write, test and run the calculation code", "tool": "run_code_task"},
        {"index": 2, "title": "Report the calculation steps", "tool": "finish"}]


def artifact(kind: ArtifactKind, name: str, size: int, n: int = 1) -> Artifact:
    aid = f"a_{n:012x}"
    return Artifact(artifact_id=aid, task_id=TASK_ID, filename=name, kind=kind, size_bytes=size, created_at=T0,
                    download_url=f"/api/artifacts/{aid}", preview=f"preview of {name}")


PY_ART = artifact(ArtifactKind.PY, "solution.py", 30, 1)


def ev(seq: int, type_: EventType, title: str, data: dict, step: Optional[int] = None) -> AgentEvent:
    return AgentEvent(seq=seq, task_id=TASK_ID, ts=T0 + timedelta(seconds=seq), type=type_, step=step,
                      title=title, data=data)


def all_event_pages() -> list[list[AgentEvent]]:
    """A code_calc run touching every event type, with a retryable error in the middle."""
    return [
        [ev(1, EventType.ROUTE, "Routed to coding", {"decision": ROUTE.model_dump(mode="json")}),
         ev(2, EventType.PLAN, "Guided plan", {"steps": PLAN})],
        [ev(3, EventType.STEP_START, "Step 1", {"index": 1, "title": "run_code_task"}, 1),
         ev(4, EventType.TOOL_CALL, "run_code_task", {"tool": "run_code_task", "args": {"request": "x"}}, 1),
         ev(5, EventType.LLM_CALL, "coder: write code", {"model_id": "coder", "purpose": "write code",
                                                         "duration_ms": 50000, "tokens_out": 500}, 1),
         ev(6, EventType.ERROR, "Model hiccup", {"error": {"code": "MODEL_TIMEOUT", "message": "slow model",
                                                           "retryable": True}}, 1)],
        [ev(7, EventType.TOOL_CALL, "Run tests (attempt 1)", {"tool": "sandbox", "args": {"command": ["pytest"]}}, 1),
         ev(8, EventType.TOOL_RESULT, "Run tests", {"tool": "sandbox", "ok": False, "summary": "1 failed",
                                                   "duration_ms": 400}, 1),
         ev(9, EventType.LOG, "retry", {"level": "warn", "text": "Attempt 1 failed, retrying"}, 1),
         ev(10, EventType.LOG, "note", {"level": "info", "text": "Attempt 2 passed"}, 1),
         ev(11, EventType.ARTIFACT, "solution.py", {"artifact": PY_ART.model_dump(mode="json")}, 1),
         ev(12, EventType.TOOL_RESULT, "run_code_task: ok", {"tool": "run_code_task", "ok": True,
                                                            "summary": "Code passed its tests", "duration_ms": 61000}, 1)],
        [ev(13, EventType.STEP_START, "Step 2", {"index": 2, "title": "finish"}, 2),
         ev(14, EventType.TOOL_CALL, "finish", {"tool": "finish", "args": {"answer": "t = 7.246 mm"}}, 2),
         ev(15, EventType.TOOL_RESULT, "finish: ok", {"tool": "finish", "ok": True, "summary": "Finished.",
                                                     "duration_ms": 0}, 2),
         ev(16, EventType.FINAL, "Done", {"answer": "t = 7.246 mm"})],
    ]


def health(ok: bool = True, chunks: int = 185) -> HealthResponse:
    return HealthResponse(status="ok" if ok else "down", contract_version=V, mock=False, ollama_ok=ok,
                          sandbox_ok=ok, tesseract_ok=ok, kb_chunks=chunks, models=[], time=T0)


def ok(data: Any, status: int = 200) -> ApiResult:
    return ApiResult(data=data, status_code=status, contract_version=V)


def net_status(seen: int = 0, now: int = 0, firewall: Optional[bool] = True) -> NetworkStatus:
    return NetworkStatus(checked_at=T0, external_count=now, external_seen_since_start=seen, total_connections=3,
                         firewall_outbound_blocked=firewall, connections=[])


class FakeClient:
    def __init__(self, health_result: Optional[ApiResult] = None, pages: Optional[list[list[AgentEvent]]] = None,
                 final_status: TaskStatus = TaskStatus.SUCCEEDED, artifacts: Optional[list[Artifact]] = None,
                 files: Optional[dict[str, bytes]] = None) -> None:
        self.base_url = BASE_URL
        self.health_result = health_result or ok(health())
        self.network_result: ApiResult = ok(net_status())
        self.pages = pages if pages is not None else []
        self.final_status = final_status
        self.final_artifacts = artifacts if artifacts is not None else [PY_ART]
        self.files = files if files is not None else {PY_ART.artifact_id: b"def f():\n    return 1\n"}
        self.download_error: Optional[ErrorInfo] = None
        self.uploads: list[tuple[str, int, str]] = []
        self.created: list[TaskCreate] = []
        self.polls: list[int] = []
        self.task_gets = 0
        self.cancelled: list[str] = []
        self.downloads: list[str] = []
        self.prewarm_calls = 0

    # ---- health / network / admin
    def health(self) -> ApiResult:
        return self.health_result

    def network_status(self) -> ApiResult:
        return self.network_result

    def prewarm(self) -> ApiResult:
        self.prewarm_calls += 1
        return ok(PrewarmResult(warmed=["general (9.1 s)", "coder (4.0 s)"], failed=[], duration_ms=13100))

    # ---- files / tasks
    def upload_file(self, filename: str, content: bytes, mime_type: str = "") -> ApiResult:
        self.uploads.append((filename, len(content), mime_type))
        return ok(FileRef(file_id=f"f_{len(self.uploads):012x}", filename=filename, mime_type=mime_type,
                          size_bytes=len(content), is_image=mime_type.startswith("image/")))

    def create_task(self, request: TaskCreate) -> ApiResult:
        self.created.append(request)
        return ok(TaskCreated(task_id=TASK_ID, status=TaskStatus.QUEUED), 202)

    def poll_events(self, task_id: str, after: int = 0) -> PollResult:
        self.polls.append(after)
        sent = [e for page in self.pages for e in page if e.seq <= after]
        index = sum(1 for page in self.pages if page and page[-1].seq <= after)
        events = self.pages[index] if index < len(self.pages) else []
        done = index >= len(self.pages) - 1
        next_seq = events[-1].seq if events else (sent[-1].seq if sent else after)
        return PollResult(events=events, next_seq=next_seq, done=done, contract_version=V)

    def get_task(self, task_id: str) -> ApiResult:
        self.task_gets += 1
        last = self.created[-1] if self.created else TaskCreate(message="resumed")
        error = ErrorInfo(code="AGENT_TIMEOUT", message="Task exceeded 600 s") \
            if self.final_status == TaskStatus.FAILED else None
        return ok(TaskState(task_id=task_id, status=self.final_status, mode=last.mode or TaskMode.GUIDED,
                            scenario=last.scenario, message=last.message, route=ROUTE,
                            final_answer="t = 7.246 mm" if self.final_status == TaskStatus.SUCCEEDED else None,
                            artifacts=self.final_artifacts, error=error, created_at=T0, elapsed_s=66.4))

    def cancel_task(self, task_id: str) -> ApiResult:
        self.cancelled.append(task_id)
        last = self.created[-1] if self.created else TaskCreate(message="x")
        return ok(TaskState(task_id=task_id, status=TaskStatus.RUNNING, mode=last.mode or TaskMode.GUIDED,
                            message=last.message, created_at=T0))

    def download_artifact(self, artifact_id: str) -> ApiResult:
        self.downloads.append(artifact_id)
        if self.download_error is not None:
            return ApiResult(error=self.download_error, failure="api", status_code=404, contract_version=V)
        return ok(DownloadedFile(filename=artifact_id, content=self.files[artifact_id],
                                 media_type="application/octet-stream"))


def docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def xlsx_bytes(rows: list[dict]) -> bytes:
    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()
