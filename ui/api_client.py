"""
Typed HTTP client for the Workbench API (Track B, ticket B2).

One method per endpoint in shared.contracts. Every method returns an `ApiResult`
and NEVER raises into the UI.

ApiResult in simple words
-------------------------
Think of it as an envelope that always comes back:
  * `ok` is True  -> `data` holds the contract model (e.g. a TaskState) and `error` is None.
  * `ok` is False -> `data` is None and `error` is a contract `ErrorInfo`
    (code + message + retryable) that the UI can show as-is.
`failure` says WHERE it went wrong, so the UI can pick the right message:
  "api"          the backend answered with an ApiError body (code comes from the server)
  "unreachable"  connection refused / network error (backend not started?)
  "timeout"      no answer in time
  "bad_response" the answer was not valid JSON or did not match the contract
For the client-side failures the code is "INTERNAL" (the only fitting key in ERROR_CODES).
Every result also carries the HTTP status and the X-Contract-Version header, so the
UI can call `version_warning()` on any result to decide whether to show the red banner.
"""
from __future__ import annotations

import re
from urllib.parse import unquote
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Literal, Optional, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from shared.contracts import (
    API_PREFIX,
    CONTRACT_VERSION,
    AgentEvent,
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
from ui.config import DEFAULT_TIMEOUT_S, LONG_TIMEOUT_S, MODEL_TIMEOUT_S, settings

T = TypeVar("T")

VERSION_HEADER = "X-Contract-Version"
Failure = Literal["api", "unreachable", "timeout", "bad_response"]
_CLIENT_ERROR_CODE = "INTERNAL"


@dataclass
class ApiResult(Generic[T]):
    """Always returned by the client; see the module docstring."""
    data: Optional[T] = None
    error: Optional[ErrorInfo] = None
    failure: Optional[Failure] = None
    status_code: Optional[int] = None      # None when no HTTP response arrived
    contract_version: Optional[str] = None  # X-Contract-Version header of the response

    @property
    def ok(self) -> bool:
        return self.error is None

    def version_warning(self) -> Optional[str]:
        return version_warning(self.contract_version, got_response=self.status_code is not None)


@dataclass
class DownloadedFile:
    """Artifact bytes (the artifact endpoint returns a file, not a JSON model)."""
    filename: str
    content: bytes
    media_type: str


@dataclass
class PollResult:
    """One events poll. On error `next_seq` stays at the `after` you passed, so just retry."""
    events: list[AgentEvent] = field(default_factory=list)
    next_seq: int = 0
    done: bool = False
    error: Optional[ErrorInfo] = None
    contract_version: Optional[str] = None


def version_warning(server_version: Optional[str], got_response: bool = True) -> Optional[str]:
    """Banner text if the server's contract version differs from ours, else None.
    Without any HTTP response there is nothing to compare, so no warning."""
    if not got_response:
        return None
    if server_version is None:
        return f"Backend sent no {VERSION_HEADER} header (UI expects {CONTRACT_VERSION})."
    if server_version != CONTRACT_VERSION:
        return f"Contract version mismatch: backend {server_version}, UI {CONTRACT_VERSION}."
    return None


def _client_error(failure: Failure, message: str, retryable: bool, status_code: Optional[int] = None,
                  contract_version: Optional[str] = None) -> ApiResult[Any]:
    return ApiResult(error=ErrorInfo(code=_CLIENT_ERROR_CODE, message=message, retryable=retryable),
                     failure=failure, status_code=status_code, contract_version=contract_version)


def _error_from_response(resp: httpx.Response) -> ApiResult[Any]:
    version = resp.headers.get(VERSION_HEADER)
    try:
        body = ApiError.model_validate(resp.json())
    except (ValueError, ValidationError):
        return _client_error("bad_response", f"HTTP {resp.status_code} without an ApiError body: {resp.text[:200]}",
                             retryable=resp.status_code >= 500, status_code=resp.status_code,
                             contract_version=version)
    return ApiResult(error=body.error, failure="api", status_code=resp.status_code, contract_version=version)


def _filename_from_disposition(header: str, fallback: str) -> str:
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", header) or re.search(r'filename="?([^";]+)"?', header)
    if not match:
        return fallback
    return unquote(match.group(1).strip()) or fallback


class ApiClient:
    """Holds one httpx.Client. Pass `transport` (e.g. httpx.MockTransport) in tests."""

    def __init__(self, base_url: Optional[str] = None, transport: Optional[httpx.BaseTransport] = None) -> None:
        self.base_url = (base_url or settings.WB_API_URL).rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=DEFAULT_TIMEOUT_S)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ core
    def _send(self, method: str, path: str, parse: Callable[[httpx.Response], T],
              timeout: float = DEFAULT_TIMEOUT_S, **kwargs: Any) -> ApiResult[T]:
        try:
            resp = self._http.request(method, f"{API_PREFIX}{path}", timeout=timeout, **kwargs)
        except httpx.TimeoutException:
            return _client_error("timeout", f"Backend did not answer within {timeout:g} s ({method} {path}).",
                                 retryable=True)
        except httpx.HTTPError as exc:  # ConnectError, RemoteProtocolError, ...
            return _client_error("unreachable", f"Cannot reach backend at {self.base_url}: {exc}", retryable=True)
        except Exception as exc:  # never raise into the UI
            return _client_error("unreachable", f"Request failed: {exc!r}", retryable=True)

        version = resp.headers.get(VERSION_HEADER)
        if not resp.is_success:
            return _error_from_response(resp)
        try:
            data = parse(resp)
        except (ValueError, ValidationError) as exc:  # bad JSON or contract mismatch
            return _client_error("bad_response", f"Response of {method} {path} does not match the contract: {exc}",
                                 retryable=False, status_code=resp.status_code, contract_version=version)
        except Exception as exc:
            return _client_error("bad_response", f"Could not read response of {method} {path}: {exc!r}",
                                 retryable=False, status_code=resp.status_code, contract_version=version)
        return ApiResult(data=data, status_code=resp.status_code, contract_version=version)

    def _json(self, method: str, path: str, model: Any, timeout: float = DEFAULT_TIMEOUT_S,
              **kwargs: Any) -> ApiResult[Any]:
        adapter = TypeAdapter(model)
        return self._send(method, path, lambda r: adapter.validate_python(r.json()), timeout, **kwargs)

    # ------------------------------------------------------------ health / models
    def health(self) -> ApiResult[HealthResponse]:
        return self._json("GET", "/health", HealthResponse)

    def models(self) -> ApiResult[list[ModelInfo]]:
        return self._json("GET", "/models", list[ModelInfo])

    def prewarm(self) -> ApiResult[PrewarmResult]:
        return self._json("POST", "/admin/prewarm", PrewarmResult, timeout=LONG_TIMEOUT_S)

    # ------------------------------------------------------------ files / routing
    def upload_file(self, filename: str, content: bytes,
                    mime_type: str = "application/octet-stream") -> ApiResult[FileRef]:
        return self._json("POST", "/files", FileRef, timeout=LONG_TIMEOUT_S,
                          files={"file": (filename, content, mime_type)})

    def route(self, request: RouteRequest) -> ApiResult[RouteDecision]:
        return self._json("POST", "/route", RouteDecision, timeout=MODEL_TIMEOUT_S,
                          json=request.model_dump(mode="json"))

    # ------------------------------------------------------------ tasks
    def create_task(self, request: TaskCreate) -> ApiResult[TaskCreated]:
        return self._json("POST", "/tasks", TaskCreated, json=request.model_dump(mode="json"))

    def get_task(self, task_id: str) -> ApiResult[TaskState]:
        return self._json("GET", f"/tasks/{task_id}", TaskState)

    def get_events(self, task_id: str, after: int = 0) -> ApiResult[EventsPage]:
        return self._json("GET", f"/tasks/{task_id}/events", EventsPage, params={"after": after})

    def poll_events(self, task_id: str, after: int = 0) -> PollResult:
        """One poll for the UI loop: pass the returned next_seq as `after` next time."""
        result = self.get_events(task_id, after)
        if result.data is None:
            return PollResult(next_seq=after, error=result.error, contract_version=result.contract_version)
        page = result.data
        return PollResult(events=page.events, next_seq=page.next_seq, done=page.done,
                          contract_version=result.contract_version)

    def cancel_task(self, task_id: str) -> ApiResult[TaskState]:
        return self._json("POST", f"/tasks/{task_id}/cancel", TaskState)

    def download_artifact(self, artifact_id: str) -> ApiResult[DownloadedFile]:
        def parse(resp: httpx.Response) -> DownloadedFile:
            filename = _filename_from_disposition(resp.headers.get("content-disposition", ""), artifact_id)
            media_type = resp.headers.get("content-type", "application/octet-stream")
            return DownloadedFile(filename=filename, content=resp.content, media_type=media_type)
        return self._send("GET", f"/artifacts/{artifact_id}", parse, timeout=LONG_TIMEOUT_S)

    # ------------------------------------------------------------ network proof
    def network_status(self) -> ApiResult[NetworkStatus]:
        return self._json("GET", "/network/status", NetworkStatus)

    def network_probe(self, request: Optional[ProbeRequest] = None) -> ApiResult[ProbeResult]:
        body = (request or ProbeRequest()).model_dump(mode="json")
        return self._json("POST", "/network/probe", ProbeResult, json=body)

    # ------------------------------------------------------------ knowledge base / audit
    def kb_stats(self) -> ApiResult[KBStats]:
        return self._json("GET", "/kb/stats", KBStats)

    def kb_search(self, request: KBSearchRequest) -> ApiResult[KBSearchResponse]:
        return self._json("POST", "/kb/search", KBSearchResponse, timeout=MODEL_TIMEOUT_S,
                          json=request.model_dump(mode="json"))

    def audit(self, task_id: Optional[str] = None, limit: int = 100) -> ApiResult[list[AuditRecord]]:
        params: dict[str, Any] = {"limit": limit}
        if task_id:
            params["task_id"] = task_id
        return self._json("GET", "/audit", list[AuditRecord], params=params)


_default_client: Optional[ApiClient] = None


def get_client() -> ApiClient:
    """Shared client for the Streamlit app (one connection pool per process)."""
    global _default_client
    if _default_client is None:
        _default_client = ApiClient()
    return _default_client
