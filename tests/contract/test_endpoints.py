"""
B1 contract tests: every endpoint in shared.contracts, every body validated with its
Pydantic model, X-Contract-Version checked on success and error responses.
/api/network/probe is only called with an invalid body (a valid one reaches the internet).
"""
from __future__ import annotations

import re
import time

import httpx
import pytest
from pydantic import TypeAdapter

from shared.contracts import (
    API_PREFIX,
    CONTRACT_VERSION,
    ERROR_CODES,
    TERMINAL_STATUSES,
    ApiError,
    AuditRecord,
    EventsPage,
    EventType,
    FileRef,
    HealthResponse,
    KBSearchResponse,
    KBStats,
    ModelInfo,
    NetworkStatus,
    PrewarmResult,
    RouteDecision,
    Scenario,
    TaskCreated,
    TaskMode,
    TaskState,
    TaskStatus,
)

UNKNOWN_TASK = "t_000000000000"
UNKNOWN_ARTIFACT = "a_000000000000"
SLOW_TIMEOUT_S = 180.0          # prewarm / first embed call can load a model
FLOW_TIMEOUT_S = 600.0          # WB_AGENT_TIMEOUT_S default
TXT_BYTES = b"Contract test upload.\nPipe wall thickness t = P*D / (2*S).\n"
# Same text as the demo request (backend/flows/code_flow.py), copied: contract tests stay black-box.
CODE_CALC_MESSAGE = (
    "Write a function for pipe wall thickness from design pressure, outside diameter "
    "and allowable stress, using t = P*D / (2*S), with tests, and print the calculation steps. "
    "The function must be exactly `def pipe_wall_thickness(P: float, D: float, S: float) -> float` "
    "(P in MPa, D in mm, S in MPa, returns t in mm). "
    "In the `if __name__ == \"__main__\":` block use exactly P = 10 MPa, D = 200 mm, S = 138 MPa "
    "and print each step; the result is t = 10 * 200 / (2 * 138) = 7.246 mm."
)


# ---------------------------------------------------------------- helpers
def check_header(resp: httpx.Response) -> None:
    assert resp.headers.get("X-Contract-Version") == CONTRACT_VERSION, (
        f"{resp.request.method} {resp.request.url.path} -> X-Contract-Version "
        f"{resp.headers.get('X-Contract-Version')!r}, expected {CONTRACT_VERSION!r}"
    )


def ok_body(resp: httpx.Response, model, status: int = 200):
    """Assert status + header, then validate the JSON body with `model` (type or list[...])."""
    assert resp.status_code == status, f"{resp.request.url.path}: HTTP {resp.status_code}: {resp.text[:500]}"
    check_header(resp)
    return TypeAdapter(model).validate_python(resp.json())


def api_error(resp: httpx.Response, status: int, code: str | None = None) -> ApiError:
    assert resp.status_code == status, f"{resp.request.url.path}: HTTP {resp.status_code}: {resp.text[:500]}"
    check_header(resp)
    err = ApiError.model_validate(resp.json())
    assert err.error.code in ERROR_CODES, f"unknown error code {err.error.code!r}"
    if code is not None:
        assert err.error.code == code
    assert err.error.message
    return err


def upload_txt(api: httpx.Client, name: str = "contract_note.txt") -> FileRef:
    resp = api.post(f"{API_PREFIX}/files", files={"file": (name, TXT_BYTES, "text/plain")}, timeout=60.0)
    return ok_body(resp, FileRef)


# ---------------------------------------------------------------- health / models / prewarm
def test_health(api):
    health = ok_body(api.get(f"{API_PREFIX}/health"), HealthResponse)
    assert health.contract_version == CONTRACT_VERSION
    assert health.kb_chunks >= 0


def test_models(api):
    models = ok_body(api.get(f"{API_PREFIX}/models"), list[ModelInfo])
    assert models, "model registry is empty"
    assert len({m.id for m in models}) == len(models)


def test_prewarm(api):
    result = ok_body(api.post(f"{API_PREFIX}/admin/prewarm", timeout=SLOW_TIMEOUT_S), PrewarmResult)
    assert result.duration_ms >= 0


# ---------------------------------------------------------------- files
def test_upload_txt(api):
    ref = upload_txt(api)
    assert re.fullmatch(r"f_[0-9a-f]{12}", ref.file_id), ref.file_id
    assert ref.size_bytes == len(TXT_BYTES)
    assert ref.is_image is False


def test_upload_bad_type(api):
    resp = api.post(f"{API_PREFIX}/files", files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")})
    api_error(resp, 415, "UNSUPPORTED_FILE")


def test_upload_missing_field(api):
    resp = api.post(f"{API_PREFIX}/files", files={"wrong_name": ("a.txt", b"x", "text/plain")})
    api_error(resp, 422, "BAD_REQUEST")


# ---------------------------------------------------------------- routing
def test_route(api):
    body = {"message": "Write Python code to compute pipe wall thickness", "file_ids": []}
    decision = ok_body(api.post(f"{API_PREFIX}/route", json=body, timeout=SLOW_TIMEOUT_S), RouteDecision)
    assert decision.reason


def test_route_bad_body(api):
    api_error(api.post(f"{API_PREFIX}/route", json={"file_ids": []}), 422, "BAD_REQUEST")


# ---------------------------------------------------------------- tasks: errors
def test_create_task_bad_body(api):
    api_error(api.post(f"{API_PREFIX}/tasks", json={"message": ""}), 422, "BAD_REQUEST")


def test_unknown_task(api):
    api_error(api.get(f"{API_PREFIX}/tasks/{UNKNOWN_TASK}"), 404, "TASK_NOT_FOUND")


def test_unknown_task_events(api):
    api_error(api.get(f"{API_PREFIX}/tasks/{UNKNOWN_TASK}/events", params={"after": 0}), 404, "TASK_NOT_FOUND")


def test_unknown_task_cancel(api):
    api_error(api.post(f"{API_PREFIX}/tasks/{UNKNOWN_TASK}/cancel"), 404, "TASK_NOT_FOUND")


def test_events_negative_after(api):
    # query validation runs before the id lookup, so an unknown id is enough here
    api_error(api.get(f"{API_PREFIX}/tasks/{UNKNOWN_TASK}/events", params={"after": -1}), 422, "BAD_REQUEST")


# ---------------------------------------------------------------- tasks: create + cancel (no full run)
def _create_code_calc(api: httpx.Client, file_ids: list[str] | None = None) -> TaskCreated:
    body = {"message": CODE_CALC_MESSAGE, "file_ids": file_ids or [], "mode": TaskMode.GUIDED.value,
            "scenario": Scenario.CODE_CALC.value}
    created = ok_body(api.post(f"{API_PREFIX}/tasks", json=body), TaskCreated, status=202)
    assert re.fullmatch(r"t_[0-9a-f]{12}", created.task_id), created.task_id
    return created


def test_create_get_events_cancel(api):
    created = _create_code_calc(api)
    state = ok_body(api.get(f"{API_PREFIX}/tasks/{created.task_id}"), TaskState)
    assert state.task_id == created.task_id and state.mode == TaskMode.GUIDED
    page = ok_body(api.get(f"{API_PREFIX}/tasks/{created.task_id}/events", params={"after": 0}), EventsPage)
    assert page.task_id == created.task_id

    cancelled = ok_body(api.post(f"{API_PREFIX}/tasks/{created.task_id}/cancel"), TaskState)
    assert cancelled.task_id == created.task_id

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = ok_body(api.get(f"{API_PREFIX}/tasks/{created.task_id}"), TaskState)
        if state.status in TERMINAL_STATUSES:
            break
        time.sleep(1)
    assert state.status in TERMINAL_STATUSES, f"task still {state.status} 120 s after cancel"
    if state.status == TaskStatus.CANCELLED:
        assert state.error is not None and state.error.code == "CANCELLED"


# ---------------------------------------------------------------- artifacts
def test_unknown_artifact(api):
    api_error(api.get(f"{API_PREFIX}/artifacts/{UNKNOWN_ARTIFACT}"), 404, "FILE_NOT_FOUND")


def test_artifact_path_characters(api):
    api_error(api.get(f"{API_PREFIX}/artifacts/a_..secret"), 400, "BAD_REQUEST")


# ---------------------------------------------------------------- network
def test_network_status(api):
    status = ok_body(api.get(f"{API_PREFIX}/network/status"), NetworkStatus)
    assert status.external_count >= 0 and status.external_seen_since_start >= 0
    assert all(not c.remote.startswith("127.") for c in status.connections)


def test_network_probe_bad_body(api):
    # Invalid target type: must be rejected before any probe runs.
    api_error(api.post(f"{API_PREFIX}/network/probe", json={"target": ["not", "a", "string"]}), 422, "BAD_REQUEST")


# ---------------------------------------------------------------- knowledge base
def test_kb_stats(api):
    stats = ok_body(api.get(f"{API_PREFIX}/kb/stats"), KBStats)
    assert stats.documents >= 0 and stats.chunks >= 0 and stats.embed_model


def test_kb_search(api):
    body = {"query": "shell wall thickness inspection", "top_k": 2}
    result = ok_body(api.post(f"{API_PREFIX}/kb/search", json=body, timeout=SLOW_TIMEOUT_S), KBSearchResponse)
    assert len(result.hits) <= 2
    assert all(0.0 <= h.score <= 1.0 for h in result.hits)


def test_kb_search_bad_top_k(api):
    api_error(api.post(f"{API_PREFIX}/kb/search", json={"query": "x", "top_k": 11}), 422, "BAD_REQUEST")


# ---------------------------------------------------------------- audit
def test_audit(api):
    records = ok_body(api.get(f"{API_PREFIX}/audit", params={"limit": 5}), list[AuditRecord])
    assert len(records) <= 5


def test_audit_bad_limit(api):
    api_error(api.get(f"{API_PREFIX}/audit", params={"limit": 0}), 422, "BAD_REQUEST")


# ---------------------------------------------------------------- full flow
@pytest.mark.flow
def test_full_flow_code_calc(api):
    ref = upload_txt(api, "flow_input.txt")
    created = _create_code_calc(api, [ref.file_id])
    task_id = created.task_id

    seqs: list[int] = []
    after, done = 0, False
    deadline = time.monotonic() + FLOW_TIMEOUT_S
    while not done:
        assert time.monotonic() < deadline, f"task {task_id} not done after {FLOW_TIMEOUT_S:.0f} s (events {seqs})"
        page = ok_body(api.get(f"{API_PREFIX}/tasks/{task_id}/events", params={"after": after}), EventsPage)
        assert page.task_id == task_id
        for event in page.events:
            assert event.task_id == task_id
            assert event.seq > after, f"event seq {event.seq} not after cursor {after}"
        if page.events:
            assert page.next_seq == page.events[-1].seq
        else:
            assert page.next_seq == after
        seqs.extend(e.seq for e in page.events)
        after, done = page.next_seq, page.done
        if not done:
            time.sleep(1)

    assert seqs, "task finished without any events"
    assert seqs == list(range(1, len(seqs) + 1)), f"seq gaps or duplicates: {seqs}"

    # after done: a replay from 0 returns the same events, and polling past the end is empty
    replay = ok_body(api.get(f"{API_PREFIX}/tasks/{task_id}/events", params={"after": 0}), EventsPage)
    assert [e.seq for e in replay.events] == seqs and replay.done
    tail = ok_body(api.get(f"{API_PREFIX}/tasks/{task_id}/events", params={"after": after}), EventsPage)
    assert tail.events == [] and tail.done

    state = ok_body(api.get(f"{API_PREFIX}/tasks/{task_id}"), TaskState)
    assert state.status == TaskStatus.SUCCEEDED, f"task ended {state.status}: {state.error}"
    assert state.final_answer
    assert state.scenario == Scenario.CODE_CALC
    assert any(e.type == EventType.FINAL for e in replay.events)
    assert state.artifacts, "succeeded code_calc task produced no artifacts"

    for art in state.artifacts:
        assert art.task_id == task_id
        assert re.fullmatch(r"a_[0-9a-f]{12}", art.artifact_id), art.artifact_id
        assert art.download_url == f"{API_PREFIX}/artifacts/{art.artifact_id}"
        resp = api.get(art.download_url, timeout=60.0)
        assert resp.status_code == 200, f"{art.download_url}: HTTP {resp.status_code}"
        check_header(resp)
        assert resp.content, f"artifact {art.filename} is empty"
        assert len(resp.content) == art.size_bytes
        assert "attachment" in resp.headers.get("content-disposition", "")

    # cancel on a finished task returns its (unchanged) state
    after_cancel = ok_body(api.post(f"{API_PREFIX}/tasks/{task_id}/cancel"), TaskState)
    assert after_cancel.status == TaskStatus.SUCCEEDED

    records = ok_body(api.get(f"{API_PREFIX}/audit", params={"task_id": task_id, "limit": 100}), list[AuditRecord])
    assert records and all(r.task_id == task_id for r in records)
