"""
Tests for backend.main (the FastAPI app). Uses TestClient, so no real
network/port binding happens. Must pass even if Ollama is down, since
backend.router falls back to its default layer in that case.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shared.contracts import (
    API_PREFIX,
    CONTRACT_VERSION,
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
    ProbeResult,
    RouteDecision,
    TaskCreated,
    TaskState,
    TaskStatus,
    TaskType,
)


_STEP_DELAY_S = 0.5


def scripted_agent(handle) -> None:
    """Stand-in for backend.agent.run: 5 fixed events, no model calls (the real agent is tested in test_agent.py)."""
    from backend.router import route
    from shared.contracts import PlanStep, RouteRequest

    decision = route(RouteRequest(message=handle.message, file_ids=handle.file_ids))
    handle.set_route(decision)
    handle.emit(EventType.ROUTE, "Routed", {"decision": decision.model_dump(mode="json")})
    plan = [PlanStep(index=1, title="Echo the request back")]
    handle.set_plan(plan)
    handle.emit(EventType.PLAN, "Planned 1 step", {"steps": [s.model_dump(mode="json") for s in plan]})
    time.sleep(_STEP_DELAY_S)
    handle.emit(EventType.STEP_START, plan[0].title, {"index": 1, "title": plan[0].title}, step=1)
    time.sleep(_STEP_DELAY_S)
    handle.emit(EventType.LOG, "Echoing", {"level": "info", "text": handle.message})
    answer = f"Echo: {handle.message}"
    handle.set_final_answer(answer)
    handle.emit(EventType.FINAL, "Done", {"answer": answer})


@pytest.fixture(scope="module")
def client():
    from backend import agent
    from backend.main import app

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agent, "run", scripted_agent)
        with TestClient(app) as c:
            yield c


def _wait_for_task_done(client: TestClient, task_id: str, timeout_s: float = 15.0) -> list:
    after = 0
    last_seq = 0
    all_events: list = []
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        resp = client.get(f"{API_PREFIX}/tasks/{task_id}/events", params={"after": after})
        assert resp.status_code == 200
        page = EventsPage.model_validate(resp.json())
        for event in page.events:
            assert event.seq == last_seq + 1, f"seq gap: expected {last_seq + 1}, got {event.seq}"
            last_seq = event.seq
            all_events.append(event)
        after = page.next_seq
        if page.done:
            return all_events
        time.sleep(0.2)

    raise AssertionError(f"task {task_id} did not finish within {timeout_s}s")


# ---------------------------------------------------------------- simple endpoints
def test_health_returns_valid_shape_and_header(client: TestClient):
    resp = client.get(f"{API_PREFIX}/health")
    assert resp.status_code == 200
    assert resp.headers["X-Contract-Version"] == CONTRACT_VERSION
    health = HealthResponse.model_validate(resp.json())
    assert health.contract_version == CONTRACT_VERSION
    assert {m.id for m in health.models} == {"general", "coder", "embed"}


def test_models_returns_valid_shape(client: TestClient):
    resp = client.get(f"{API_PREFIX}/models")
    assert resp.status_code == 200
    models = [ModelInfo.model_validate(m) for m in resp.json()]
    assert {m.id for m in models} == {"general", "coder", "embed"}


def test_route_endpoint_returns_valid_decision(client: TestClient):
    resp = client.post(f"{API_PREFIX}/route", json={"message": "There's a bug in my python code"})
    assert resp.status_code == 200
    decision = RouteDecision.model_validate(resp.json())
    assert decision.task_type == TaskType.CODING


def test_file_upload_returns_valid_file_ref(client: TestClient):
    resp = client.post(f"{API_PREFIX}/files", files={"file": ("hello.txt", b"hello", "text/plain")})
    assert resp.status_code == 200
    ref = FileRef.model_validate(resp.json())
    assert ref.filename == "hello.txt"
    assert ref.is_image is False
    assert ref.size_bytes == len(b"hello")


def test_file_upload_rejects_unsupported_type(client: TestClient):
    resp = client.post(f"{API_PREFIX}/files", files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
    assert resp.status_code == 415
    body = ApiError.model_validate(resp.json())
    assert body.error.code == "UNSUPPORTED_FILE"
    assert resp.headers["X-Contract-Version"] == CONTRACT_VERSION


def test_kb_stats_stub_returns_valid_shape(client: TestClient):
    resp = client.get(f"{API_PREFIX}/kb/stats")
    assert resp.status_code == 200
    KBStats.model_validate(resp.json())


def test_kb_search_stub_returns_empty_hits(client: TestClient):
    resp = client.post(f"{API_PREFIX}/kb/search", json={"query": "hot work permit"})
    assert resp.status_code == 200
    result = KBSearchResponse.model_validate(resp.json())
    assert result.hits == []


def test_network_status_stub_returns_valid_shape(client: TestClient):
    resp = client.get(f"{API_PREFIX}/network/status")
    assert resp.status_code == 200
    NetworkStatus.model_validate(resp.json())


def test_network_probe_stub_does_not_claim_a_real_probe(client: TestClient):
    resp = client.post(f"{API_PREFIX}/network/probe", json={"target": "https://example.com"})
    assert resp.status_code == 200
    result = ProbeResult.model_validate(resp.json())
    assert result.reachable is False
    assert "not implemented" in (result.error or "").lower()


def test_admin_prewarm_stub_returns_valid_shape(client: TestClient):
    resp = client.post(f"{API_PREFIX}/admin/prewarm")
    assert resp.status_code == 200
    PrewarmResult.model_validate(resp.json())


def test_artifact_unknown_id_returns_404(client: TestClient):
    resp = client.get(f"{API_PREFIX}/artifacts/a_doesnotexist")
    assert resp.status_code == 404
    ApiError.model_validate(resp.json())
    assert resp.headers["X-Contract-Version"] == CONTRACT_VERSION


def test_audit_endpoint_returns_valid_shape(client: TestClient):
    resp = client.get(f"{API_PREFIX}/audit", params={"limit": 5})
    assert resp.status_code == 200
    [AuditRecord.model_validate(r) for r in resp.json()]


# ---------------------------------------------------------------- errors / contract-version header
def test_unknown_task_id_returns_404_api_error(client: TestClient):
    resp = client.get(f"{API_PREFIX}/tasks/t_doesnotexist")
    assert resp.status_code == 404
    body = ApiError.model_validate(resp.json())
    assert body.error.code == "TASK_NOT_FOUND"
    assert resp.headers["X-Contract-Version"] == CONTRACT_VERSION


def test_unknown_task_id_events_returns_404(client: TestClient):
    resp = client.get(f"{API_PREFIX}/tasks/t_doesnotexist/events")
    assert resp.status_code == 404
    body = ApiError.model_validate(resp.json())
    assert body.error.code == "TASK_NOT_FOUND"


def test_unknown_task_id_cancel_returns_404(client: TestClient):
    resp = client.post(f"{API_PREFIX}/tasks/t_doesnotexist/cancel")
    assert resp.status_code == 404
    body = ApiError.model_validate(resp.json())
    assert body.error.code == "TASK_NOT_FOUND"


def test_validation_error_returns_api_error_with_header(client: TestClient):
    resp = client.post(f"{API_PREFIX}/tasks", json={"message": ""})  # min_length=1
    assert resp.status_code == 422
    body = ApiError.model_validate(resp.json())
    assert body.error.code == "BAD_REQUEST"
    assert resp.headers["X-Contract-Version"] == CONTRACT_VERSION


def test_docs_and_redoc_are_disabled(client: TestClient):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_openapi_json_is_still_served(client: TestClient):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200


# ---------------------------------------------------------------- full task flow
def test_full_task_flow_upload_route_events_final_state(client: TestClient):
    resp = client.post(f"{API_PREFIX}/files", files={"file": ("note.txt", b"hello world", "text/plain")})
    assert resp.status_code == 200
    file_ref = FileRef.model_validate(resp.json())

    resp = client.post(
        f"{API_PREFIX}/tasks",
        json={"message": "Please summarize this note", "file_ids": [file_ref.file_id]},
    )
    assert resp.status_code == 202
    created = TaskCreated.model_validate(resp.json())
    assert created.status == TaskStatus.QUEUED

    events = _wait_for_task_done(client, created.task_id)
    assert [e.type for e in events] == [
        EventType.ROUTE,
        EventType.PLAN,
        EventType.STEP_START,
        EventType.LOG,
        EventType.FINAL,
    ]

    resp = client.get(f"{API_PREFIX}/tasks/{created.task_id}")
    assert resp.status_code == 200
    state = TaskState.model_validate(resp.json())
    assert state.status == TaskStatus.SUCCEEDED
    assert state.route is not None
    assert len(state.plan) == 1
    assert state.final_answer is not None and state.final_answer.startswith("Echo:")
    assert state.elapsed_s is not None and state.elapsed_s >= 0


def test_events_after_zero_returns_all_five_events(client: TestClient):
    resp = client.post(f"{API_PREFIX}/tasks", json={"message": "Explain what a pressure relief valve does"})
    assert resp.status_code == 202
    created = TaskCreated.model_validate(resp.json())
    _wait_for_task_done(client, created.task_id)

    resp = client.get(f"{API_PREFIX}/tasks/{created.task_id}/events", params={"after": 0})
    assert resp.status_code == 200
    page = EventsPage.model_validate(resp.json())
    assert len(page.events) == 5
    assert page.done is True
    assert [e.seq for e in page.events] == [1, 2, 3, 4, 5]


def test_second_task_created_quickly_starts_queued(client: TestClient):
    resp1 = client.post(f"{API_PREFIX}/tasks", json={"message": "first task keeps the worker busy"})
    created1 = TaskCreated.model_validate(resp1.json())

    resp2 = client.post(f"{API_PREFIX}/tasks", json={"message": "second task should be queued"})
    created2 = TaskCreated.model_validate(resp2.json())
    assert created2.status == TaskStatus.QUEUED

    resp = client.get(f"{API_PREFIX}/tasks/{created2.task_id}")
    state2 = TaskState.model_validate(resp.json())
    assert state2.status == TaskStatus.QUEUED

    _wait_for_task_done(client, created1.task_id)
    _wait_for_task_done(client, created2.task_id)


def test_cancel_queued_task_sets_cancelled(client: TestClient):
    resp1 = client.post(f"{API_PREFIX}/tasks", json={"message": "occupy the worker for a bit"})
    created1 = TaskCreated.model_validate(resp1.json())

    resp2 = client.post(f"{API_PREFIX}/tasks", json={"message": "this one gets cancelled while queued"})
    created2 = TaskCreated.model_validate(resp2.json())

    resp = client.post(f"{API_PREFIX}/tasks/{created2.task_id}/cancel")
    assert resp.status_code == 200
    state2 = TaskState.model_validate(resp.json())
    assert state2.status == TaskStatus.CANCELLED
    assert state2.error is not None and state2.error.code == "CANCELLED"

    _wait_for_task_done(client, created1.task_id)

    # the cancelled task must never have run
    resp = client.get(f"{API_PREFIX}/tasks/{created2.task_id}/events", params={"after": 0})
    page = EventsPage.model_validate(resp.json())
    assert page.events == []
    assert page.done is True


def test_health_reports_sandbox_down_when_unavailable(client: TestClient, monkeypatch):
    from backend import main

    monkeypatch.setattr(main, "sandbox_available", lambda: False)
    resp = client.get(f"{API_PREFIX}/health")
    assert resp.status_code == 200
    health = HealthResponse.model_validate(resp.json())
    assert health.sandbox_ok is False
    assert health.status in ("degraded", "down")
