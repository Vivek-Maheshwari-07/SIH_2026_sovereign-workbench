"""
B2 tests for ui/api_client.py and ui/config.py.
Unit tests use httpx.MockTransport (no server). Live tests hit WB_API_URL and skip if it is down.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from shared.contracts import (
    CONTRACT_VERSION,
    ERROR_CODES,
    ApiError,
    ErrorInfo,
    HealthResponse,
    KBSearchRequest,
    ModelInfo,
    RouteRequest,
    Scenario,
    TaskCreate,
    TaskMode,
    TaskStatus,
)
from ui import api_client
from ui.api_client import ApiClient, ApiResult, version_warning
from ui.config import DEFAULT_TIMEOUT_S, LONG_TIMEOUT_S, UISettings, load_settings

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc).isoformat()
HDR = {"X-Contract-Version": CONTRACT_VERSION}


def make_client(handler) -> ApiClient:
    return ApiClient(base_url="http://127.0.0.1:9", transport=httpx.MockTransport(handler))


def health_json() -> dict:
    return {"status": "ok", "contract_version": CONTRACT_VERSION, "mock": True, "ollama_ok": True,
            "sandbox_ok": True, "tesseract_ok": True, "kb_chunks": 3,
            "models": [{"id": "general", "ollama_name": "m:1"}], "time": NOW}


def event_json(seq: int) -> dict:
    return {"seq": seq, "task_id": "t_1", "ts": NOW, "type": "log", "title": f"e{seq}", "data": {}}


def api_error_json(code: str, message: str = "nope") -> dict:
    return ApiError(error=ErrorInfo(code=code, message=message)).model_dump(mode="json")


# ---------------------------------------------------------------- config
def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("WB_API_URL", "http://127.0.0.1:1234")
    assert load_settings().WB_API_URL == "http://127.0.0.1:1234"


def test_config_timeouts():
    assert DEFAULT_TIMEOUT_S == 10.0 and LONG_TIMEOUT_S == 60.0
    assert isinstance(api_client.settings, UISettings)


# ---------------------------------------------------------------- success
def test_health_success():
    client = make_client(lambda req: httpx.Response(200, json=health_json(), headers=HDR))
    result = client.health()
    assert result.ok and isinstance(result.data, HealthResponse)
    assert result.status_code == 200 and result.contract_version == CONTRACT_VERSION
    assert result.version_warning() is None


def test_models_returns_list_of_models():
    client = make_client(lambda req: httpx.Response(200, json=[{"id": "coder", "ollama_name": "c:3b"}], headers=HDR))
    result = client.models()
    assert result.ok and isinstance(result.data[0], ModelInfo)


def test_create_task_sends_contract_body():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(202, json={"task_id": "t_abc", "status": "queued"}, headers=HDR)

    result = make_client(handler).create_task(
        TaskCreate(message="hi", mode=TaskMode.GUIDED, scenario=Scenario.CODE_CALC))
    assert result.ok and result.data.status == TaskStatus.QUEUED
    assert seen["path"] == "/api/tasks"
    assert seen["body"]["mode"] == "guided" and seen["body"]["scenario"] == "code_calc"


def test_route_and_kb_search_paths():
    paths = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path == "/api/route":
            return httpx.Response(200, headers=HDR, json={"task_type": "coding", "model_id": "coder",
                                  "ollama_name": "c", "reason": "r", "layer": "rule", "confidence": 1.0})
        return httpx.Response(200, headers=HDR, json={"hits": [{"text": "t", "source": "s.pdf", "score": 0.5}]})

    client = make_client(handler)
    assert client.route(RouteRequest(message="code")).ok
    assert client.kb_search(KBSearchRequest(query="q")).data.hits[0].source == "s.pdf"
    assert paths == ["/api/route", "/api/kb/search"]


def test_upload_uses_multipart_field_file_and_long_timeout():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["timeout"] = req.extensions["timeout"]["read"]
        seen["content"] = req.content
        return httpx.Response(200, headers=HDR, json={"file_id": "f_1", "filename": "a.txt", "mime_type": "text/plain",
                                                      "size_bytes": 2, "is_image": False})

    result = make_client(handler).upload_file("a.txt", b"hi", "text/plain")
    assert result.ok and result.data.file_id == "f_1"
    assert seen["timeout"] == LONG_TIMEOUT_S
    assert b'name="file"; filename="a.txt"' in seen["content"]


def test_normal_call_uses_default_timeout():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["timeout"] = req.extensions["timeout"]["read"]
        return httpx.Response(200, json=health_json(), headers=HDR)

    make_client(handler).health()
    assert seen["timeout"] == DEFAULT_TIMEOUT_S


def test_download_artifact():
    headers = {**HDR, "content-disposition": 'attachment; filename="calc.py"', "content-type": "text/x-python"}
    client = make_client(lambda req: httpx.Response(200, content=b"print(1)\n", headers=headers))
    result = client.download_artifact("a_1")
    assert result.ok and result.data.filename == "calc.py" and result.data.content == b"print(1)\n"


def test_audit_params():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[], headers=HDR)

    assert make_client(handler).audit(task_id="t_1", limit=5).data == []
    assert seen["params"] == {"task_id": "t_1", "limit": "5"}


# ---------------------------------------------------------------- failures never raise
def test_timeout_becomes_error():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    result = make_client(handler).health()
    assert not result.ok and result.data is None
    assert result.failure == "timeout" and result.error.retryable
    assert result.error.code in ERROR_CODES
    assert result.status_code is None and result.version_warning() is None


def test_connection_refused_becomes_error():
    def handler(req):
        raise httpx.ConnectError("[WinError 10061] refused", request=req)

    result = make_client(handler).get_task("t_1")
    assert result.failure == "unreachable" and result.error.retryable
    assert "Cannot reach backend" in result.error.message


def test_real_connection_refused():
    # nothing listens on port 9 (discard) on the dev laptop; no mock transport here
    result = ApiClient(base_url="http://127.0.0.1:9").health()
    assert not result.ok and result.failure in ("unreachable", "timeout")


def test_api_error_body_is_passed_through():
    client = make_client(lambda req: httpx.Response(404, json=api_error_json("TASK_NOT_FOUND"), headers=HDR))
    result = client.get_task("t_x")
    assert result.failure == "api" and result.status_code == 404
    assert result.error.code == "TASK_NOT_FOUND" and result.error.message == "nope"


def test_non_api_error_body():
    client = make_client(lambda req: httpx.Response(502, text="<html>Bad gateway</html>"))
    result = client.health()
    assert result.failure == "bad_response" and result.status_code == 502 and result.error.retryable


def test_bad_json_becomes_error():
    client = make_client(lambda req: httpx.Response(200, content=b"{not json", headers=HDR))
    result = client.health()
    assert result.failure == "bad_response" and result.data is None
    assert result.contract_version == CONTRACT_VERSION


def test_contract_mismatch_becomes_error():
    client = make_client(lambda req: httpx.Response(200, json={"status": "weird"}, headers=HDR))
    result = client.health()
    assert result.failure == "bad_response" and "does not match the contract" in result.error.message


def test_unexpected_exception_never_raises():
    def handler(req):
        raise RuntimeError("boom")

    assert make_client(handler).kb_stats().failure == "unreachable"


# ---------------------------------------------------------------- version check
def test_version_mismatch_warning():
    client = make_client(lambda req: httpx.Response(200, json=health_json(), headers={"X-Contract-Version": "0.9.0"}))
    result = client.health()
    assert result.ok
    assert "0.9.0" in result.version_warning() and CONTRACT_VERSION in result.version_warning()


def test_version_warning_on_error_response_and_missing_header():
    result = make_client(lambda req: httpx.Response(404, json=api_error_json("TASK_NOT_FOUND"))).get_task("t")
    assert "no X-Contract-Version" in result.version_warning()
    assert version_warning(CONTRACT_VERSION) is None
    assert version_warning(None, got_response=False) is None


def test_api_result_ok_property():
    assert ApiResult(data=1).ok and not ApiResult(error=ErrorInfo(code="INTERNAL", message="x")).ok


# ---------------------------------------------------------------- poll_events
def test_poll_events_success():
    seen = {}

    def handler(req):
        seen["after"] = req.url.params["after"]
        return httpx.Response(200, headers=HDR, json={"task_id": "t_1", "events": [event_json(3), event_json(4)],
                                                      "next_seq": 4, "done": True})

    poll = make_client(handler).poll_events("t_1", after=2)
    assert seen["after"] == "2"
    assert [e.seq for e in poll.events] == [3, 4] and poll.next_seq == 4 and poll.done and poll.error is None


def test_poll_events_error_keeps_cursor():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    poll = make_client(handler).poll_events("t_1", after=7)
    assert poll.events == [] and poll.next_seq == 7 and not poll.done and poll.error is not None


# ---------------------------------------------------------------- live (real backend at WB_API_URL)
@pytest.fixture(scope="module")
def live_client():
    client = ApiClient()
    result = client.health()
    if result.failure in ("unreachable", "timeout"):
        pytest.skip(f"backend not reachable at {client.base_url}: {result.error.message}")
    yield client
    client.close()


def test_live_health(live_client):
    result = live_client.health()
    assert result.ok, result.error
    assert result.version_warning() is None, result.version_warning()


def test_live_models_and_kb_stats(live_client):
    assert live_client.models().ok
    assert live_client.kb_stats().ok


def test_live_unknown_task(live_client):
    result = live_client.get_task("t_000000000000")
    assert result.failure == "api" and result.error.code == "TASK_NOT_FOUND"
    assert result.version_warning() is None


def test_live_poll_unknown_task(live_client):
    poll = live_client.poll_events("t_000000000000", after=0)
    assert poll.error is not None and poll.error.code == "TASK_NOT_FOUND" and poll.next_seq == 0


def test_live_bad_upload(live_client):
    result = live_client.upload_file("evil.exe", b"MZ\x90\x00")
    assert result.failure == "api" and result.error.code == "UNSUPPORTED_FILE"


def test_live_network_status(live_client):
    assert live_client.network_status().ok


def test_route_and_kb_search_use_model_timeout():
    from ui.config import MODEL_TIMEOUT_S
    timeouts = []

    def handler(req: httpx.Request) -> httpx.Response:
        timeouts.append(req.extensions["timeout"]["read"])
        return httpx.Response(500, json=api_error_json("INTERNAL"), headers=HDR)

    client = make_client(handler)
    client.route(RouteRequest(message="x"))
    client.kb_search(KBSearchRequest(query="q"))
    client.kb_stats()
    assert MODEL_TIMEOUT_S == 30.0
    assert timeouts == [MODEL_TIMEOUT_S, MODEL_TIMEOUT_S, DEFAULT_TIMEOUT_S]
