"""
SHARED CONTRACT  -  single source of truth between Track A (backend) and Track B (UI).

RULES
1. Nobody edits this file alone. A change needs a PR approved by BOTH devs.
2. Every change bumps CONTRACT_VERSION (patch = new optional field, minor = new endpoint,
   major = rename/remove anything).
3. Only ADD optional fields after the freeze. Never rename or delete.
4. Backend returns CONTRACT_VERSION in /api/health and in the X-Contract-Version header.
   The UI shows a red banner if versions differ.

HTTP ENDPOINTS (all JSON unless noted, all under API_PREFIX, server binds 127.0.0.1 only)

  Method  Path                              Request body        Response
  ------  --------------------------------  ------------------  -------------------------
  GET     /api/health                       -                   HealthResponse
  GET     /api/models                       -                   list[ModelInfo]
  POST    /api/files        (multipart, field name "file")      FileRef
  POST    /api/route                        RouteRequest        RouteDecision
  POST    /api/tasks                        TaskCreate          TaskCreated     (202)
  GET     /api/tasks/{task_id}              -                   TaskState
  GET     /api/tasks/{task_id}/events?after=<seq>  -            EventsPage
  POST    /api/tasks/{task_id}/cancel       -                   TaskState
  GET     /api/artifacts/{artifact_id}      -                   file bytes (Content-Disposition: attachment)
  GET     /api/network/status               -                   NetworkStatus
  POST    /api/network/probe                ProbeRequest        ProbeResult
  GET     /api/kb/stats                     -                   KBStats
  POST    /api/kb/search                    KBSearchRequest     KBSearchResponse
  GET     /api/audit?task_id=<id>&limit=<n> -                   list[AuditRecord]
  POST    /api/admin/prewarm                -                   PrewarmResult

ERRORS: every non-2xx response has body ApiError. Codes are listed in ERROR_CODES.

EVENT FLOW (UI polls, no websockets):
  POST /api/tasks -> task_id
  loop every 1s: GET /api/tasks/{id}/events?after=<last next_seq>
  stop when EventsPage.done == true, then GET /api/tasks/{id} once for the final state.
  seq starts at 1 and increases by 1 per event. after=0 returns everything.

AgentEvent.data payload per EventType (keys are fixed):
  route        {"decision": RouteDecision-dict}
  plan         {"steps": [PlanStep-dict, ...]}
  step_start   {"index": int, "title": str}
  llm_call     {"model_id": str, "purpose": str, "duration_ms": int, "tokens_out": int|None}
  tool_call    {"tool": str, "args": dict}
  tool_result  {"tool": str, "ok": bool, "summary": str, "duration_ms": int}
  artifact     {"artifact": Artifact-dict}
  log          {"level": "info"|"warn", "text": str}
  final        {"answer": str}
  error        {"error": ErrorInfo-dict}
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

CONTRACT_VERSION = "1.0.2"
API_PREFIX = "/api"

ERROR_CODES = {
    "BAD_REQUEST": "Request body failed validation.",
    "FILE_NOT_FOUND": "file_id does not exist in this workspace.",
    "FILE_TOO_LARGE": "Upload above WB_MAX_UPLOAD_MB.",
    "UNSUPPORTED_FILE": "File type not in pdf/png/jpg/jpeg/txt/md/py/csv/xlsx/docx.",
    "TASK_NOT_FOUND": "task_id does not exist.",
    "TASK_BUSY": "Another task is running; new task was queued (not an error for UI, info only).",
    "MODEL_UNAVAILABLE": "Ollama not reachable or model not pulled.",
    "MODEL_TIMEOUT": "Model call exceeded timeout.",
    "BAD_MODEL_OUTPUT": "Model output failed schema validation after retries.",
    "SANDBOX_UNAVAILABLE": "Docker not running or sandbox image missing.",
    "SANDBOX_TIMEOUT": "Code exceeded WB_SANDBOX_TIMEOUT_S.",
    "AGENT_STEP_LIMIT": "Agent hit WB_AGENT_MAX_STEPS without finishing.",
    "AGENT_TIMEOUT": "Task exceeded WB_AGENT_TIMEOUT_S.",
    "CANCELLED": "Task cancelled by user.",
    "INTERNAL": "Unexpected server error (see logs/backend.log).",
}


# ---------------------------------------------------------------- enums
class TaskType(str, Enum):
    DOCUMENT = "document"
    CODING = "coding"
    VISION = "vision"
    GENERAL = "general"


class TaskMode(str, Enum):
    AGENT = "agent"      # free agent loop
    GUIDED = "guided"    # fixed pipeline for a known scenario (demo safety net)


class Scenario(str, Enum):
    INSPECTION_NOTE = "inspection_note"  # scanned inspection report -> approval note .docx
    CODE_CALC = "code_calc"              # engineering calc code + tests in sandbox
    PID_TAGS = "pid_tags"                # P&ID image -> tag list .xlsx


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class EventType(str, Enum):
    ROUTE = "route"
    PLAN = "plan"
    STEP_START = "step_start"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    LOG = "log"
    FINAL = "final"
    ERROR = "error"


class ArtifactKind(str, Enum):
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    PY = "py"
    TXT = "txt"
    MD = "md"
    PNG = "png"
    JSON = "json"


# ---------------------------------------------------------------- errors
class ErrorInfo(BaseModel):
    code: str                      # one of ERROR_CODES keys
    message: str
    retryable: bool = False


class ApiError(BaseModel):
    error: ErrorInfo


# ---------------------------------------------------------------- health / models
class ModelInfo(BaseModel):
    id: str                        # stable id used in code: "general" | "coder" | "embed"
    ollama_name: str               # e.g. "qwen3.5:4b"
    tasks: list[TaskType] = []
    supports_tools: bool = False
    supports_vision: bool = False
    loaded: bool = False           # currently in RAM (from `ollama ps`)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    contract_version: str
    mock: bool                     # true when served by mock server
    ollama_ok: bool
    sandbox_ok: bool
    tesseract_ok: bool
    kb_chunks: int
    models: list[ModelInfo]
    time: datetime


class PrewarmResult(BaseModel):
    warmed: list[str]              # model ids loaded
    failed: list[str] = []
    duration_ms: int


# ---------------------------------------------------------------- files
class FileRef(BaseModel):
    file_id: str                   # "f_" + 12 hex chars
    filename: str
    mime_type: str
    size_bytes: int
    is_image: bool
    pages: Optional[int] = None            # PDFs only
    has_text_layer: Optional[bool] = None  # PDFs only; False = scanned


# ---------------------------------------------------------------- routing
class RouteRequest(BaseModel):
    message: str
    file_ids: list[str] = []


class RouteDecision(BaseModel):
    task_type: TaskType
    model_id: str
    ollama_name: str
    reason: str                    # human readable, shown in UI badge
    layer: Literal["rule", "similarity", "default", "forced"]
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------- tasks
class TaskCreate(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    file_ids: list[str] = []
    mode: TaskMode = TaskMode.AGENT
    scenario: Optional[Scenario] = None    # REQUIRED when mode == guided


class TaskCreated(BaseModel):
    task_id: str                   # "t_" + 12 hex chars
    status: TaskStatus


class PlanStep(BaseModel):
    index: int                     # starts at 1
    title: str
    tool: Optional[str] = None


class Artifact(BaseModel):
    artifact_id: str               # "a_" + 12 hex chars
    task_id: str
    filename: str
    kind: ArtifactKind
    size_bytes: int
    created_at: datetime
    download_url: str              # always f"{API_PREFIX}/artifacts/{artifact_id}"
    preview: Optional[str] = None  # <= 500 chars plain text for UI card


class AgentEvent(BaseModel):
    seq: int
    task_id: str
    ts: datetime
    type: EventType
    step: Optional[int] = None
    title: str                     # one short line for the timeline
    data: dict[str, Any] = {}      # see module docstring for keys per type


class EventsPage(BaseModel):
    task_id: str
    events: list[AgentEvent]
    next_seq: int                  # pass as ?after= next time
    done: bool                     # true once task is in a terminal status and all events sent


class TaskState(BaseModel):
    task_id: str
    status: TaskStatus
    mode: TaskMode
    scenario: Optional[Scenario] = None
    message: str
    file_ids: list[str] = []
    route: Optional[RouteDecision] = None
    plan: list[PlanStep] = []
    final_answer: Optional[str] = None
    artifacts: list[Artifact] = []
    error: Optional[ErrorInfo] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    elapsed_s: Optional[float] = None


# ---------------------------------------------------------------- network proof
class Connection(BaseModel):
    pid: Optional[int] = None
    process: Optional[str] = None
    local: str                     # "ip:port"
    remote: str                    # "ip:port"
    status: str                    # psutil status, e.g. ESTABLISHED
    external: bool                 # True if remote is not loopback. Strict mode for the laptop demo:
                                   # only loopback is local; LAN addresses count as external.
    # 1.0.1 (optional):
    group: Optional[Literal["established", "attempt"]] = None  # attempt = SYN_SENT/SYN_RECV, never connected
    origin: Optional[Literal["ours", "other_app", "probe"]] = None  # ours = backend/Ollama/UI + platform
    first_seen: Optional[datetime] = None  # when the monitor first saw this (pid, remote, group)
    # 1.0.2 (optional), set only when origin == "ours":
    #   core     = backend pid tree (backend + all its children), Ollama model server (ollama.exe,
    #              ollama_llama_server), Streamlit UI -> the sovereign proof
    #   platform = Docker Desktop / WSL / Ollama tray app: com.docker.*, vpnkit, wsl*, vmmem*,
    #              "ollama app.exe" (update checks) -> counted apart
    component: Optional[Literal["core", "platform"]] = None


class NetworkStatus(BaseModel):
    checked_at: datetime
    external_count: int            # CORE external connections right now (group established;
                                   # see Connection.component). Platform, other apps, probe excluded.
    external_seen_since_start: int # unique CORE established external connections since backend start
                                   # (the headline number). Platform, other apps, probe, attempts excluded.
    total_connections: int
    firewall_outbound_blocked: Optional[bool] = None  # None = could not read
    connections: list[Connection]  # non-listening, non-loopback only
    # 1.0.1 (optional).
    since: Optional[datetime] = None               # monitor start = start of the *_since_start counts
    attempts_since_start: Optional[int] = None     # unique attempts (not probe), never counted as leaks
    other_apps_since_start: Optional[int] = None   # unique established connections of other apps (info only)
    probe_since_start: Optional[int] = None        # unique connections made by /network/probe
    monitor_error: Optional[str] = None            # last psutil error; None = monitor healthy
    # 1.0.2 (optional). Docker Desktop / WSL / Ollama tray app (Connection.component "platform"),
    # counted apart from core.
    platform_seen_since_start: Optional[int] = None      # unique established platform connections
    platform_attempts_since_start: Optional[int] = None  # unique platform attempts (also inside attempts_since_start)


class ProbeRequest(BaseModel):
    target: str = "https://www.google.com"


class ProbeResult(BaseModel):
    target: str
    reachable: bool                # expected False in demo
    error: Optional[str] = None
    duration_ms: int


# ---------------------------------------------------------------- knowledge base
class KBStats(BaseModel):
    documents: int
    chunks: int
    embed_model: str


class KBSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=4, ge=1, le=10)


class KBHit(BaseModel):
    text: str
    source: str                    # file name
    page: Optional[int] = None
    score: float                   # higher = better, 0..1


class KBSearchResponse(BaseModel):
    hits: list[KBHit]


# ---------------------------------------------------------------- audit
class AuditRecord(BaseModel):
    ts: datetime
    task_id: Optional[str] = None
    kind: Literal["llm", "tool", "http", "network", "system"]
    name: str                      # model id, tool name, endpoint, ...
    target: Optional[str] = None   # for llm/http: the URL called (must be 127.0.0.1)
    duration_ms: int = 0
    ok: bool = True
    detail: dict[str, Any] = {}


# ---------------------------------------------------------------- deliverable schemas
# The LLM fills these (Ollama `format=` JSON schema). Code renders them to Office files.
class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Finding(BaseModel):
    item: str                      # e.g. "Shell course 2, north side"
    observation: str               # e.g. "Wall thinning to 6.1 mm, nominal 8 mm"
    severity: Severity
    source_page: Optional[int] = None


class ApprovalNote(BaseModel):
    ref_no: str                    # filled by code, not by LLM
    date: str                      # filled by code (YYYY-MM-DD)
    subject: str
    background: str
    findings: list[Finding] = Field(min_length=1)
    sop_references: list[str] = []  # e.g. "SOP-INSP-012, p.4"
    recommendation: str
    cost_implication: Optional[str] = None
    prepared_by: str = "Sovereign AI Workbench (draft for human review)"


class PidTag(BaseModel):
    tag: str                       # e.g. "P-101A"
    equipment_type: str            # e.g. "Pump", "Valve", "Vessel", "Instrument"
    description: Optional[str] = None
    tile: Optional[int] = None     # which image tile it was found in (1..4)


class PidTagList(BaseModel):
    drawing_title: Optional[str] = None
    tags: list[PidTag]
    notes: Optional[str] = None


class CodeResult(BaseModel):
    code: str
    tests: str
    passed: int
    failed: int
    attempts: int                  # 1..3
    stdout_tail: str               # last 2000 chars
