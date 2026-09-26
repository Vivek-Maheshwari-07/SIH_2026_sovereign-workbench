"""
B3 tests: ui/app.py rendered with Streamlit's AppTest and a fake ApiClient (no backend),
plus checks on ui/scenarios.py and ui/config.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from shared.contracts import (
    CONTRACT_VERSION,
    ERROR_CODES,
    ErrorInfo,
    FileRef,
    HealthResponse,
    PrewarmResult,
    Scenario,
    TaskCreate,
    TaskCreated,
    TaskMode,
    TaskState,
    TaskStatus,
)
from ui import api_client
from ui.api_client import ApiResult
from ui.config import ALLOWED_UPLOAD_TYPES, MAX_MESSAGE_CHARS
from ui.scenarios import SCENARIOS, get_scenario

APP = str(Path(__file__).resolve().parents[2] / "ui" / "app.py")
BASE_URL = "http://127.0.0.1:8000"
NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def health(ok: bool = True, chunks: int = 185) -> HealthResponse:
    return HealthResponse(status="ok" if ok else "down", contract_version=CONTRACT_VERSION, mock=False,
                          ollama_ok=ok, sandbox_ok=ok, tesseract_ok=ok, kb_chunks=chunks, models=[], time=NOW)


class FakeClient:
    """Stands in for ApiClient; records calls. Same method names and ApiResult returns."""

    def __init__(self, health_result: ApiResult) -> None:
        self.base_url = BASE_URL
        self.health_result = health_result
        self.uploads: list[tuple[str, int, str]] = []
        self.created: list[TaskCreate] = []
        self.prewarm_calls = 0

    def health(self) -> ApiResult[HealthResponse]:
        return self.health_result

    def upload_file(self, filename: str, content: bytes, mime_type: str = "") -> ApiResult[FileRef]:
        self.uploads.append((filename, len(content), mime_type))
        return ApiResult(data=FileRef(file_id=f"f_{len(self.uploads):012x}", filename=filename, mime_type=mime_type,
                                      size_bytes=len(content), is_image=mime_type.startswith("image/")),
                         status_code=200, contract_version=CONTRACT_VERSION)

    def create_task(self, request: TaskCreate) -> ApiResult[TaskCreated]:
        self.created.append(request)
        return ApiResult(data=TaskCreated(task_id="t_0123456789ab", status=TaskStatus.QUEUED), status_code=202,
                         contract_version=CONTRACT_VERSION)

    def get_task(self, task_id: str) -> ApiResult[TaskState]:
        last = self.created[-1]
        return ApiResult(data=TaskState(task_id=task_id, status=TaskStatus.RUNNING, mode=last.mode,
                                        scenario=last.scenario, message=last.message, created_at=NOW),
                         status_code=200, contract_version=CONTRACT_VERSION)

    def prewarm(self) -> ApiResult[PrewarmResult]:
        self.prewarm_calls += 1
        return ApiResult(data=PrewarmResult(warmed=["general (9.1 s)", "coder (4.0 s)"], failed=[], duration_ms=13100),
                         status_code=200, contract_version=CONTRACT_VERSION)


def ok_health(**kw) -> ApiResult:
    return ApiResult(data=health(**kw), status_code=200, contract_version=CONTRACT_VERSION)


def run_app(monkeypatch, fake: FakeClient) -> AppTest:
    monkeypatch.setattr(api_client, "get_client", lambda: fake)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def all_markdown(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)  # includes the sidebar


# ---------------------------------------------------------------- rendering
def test_renders_with_healthy_backend(monkeypatch):
    at = run_app(monkeypatch, FakeClient(ok_health()))
    text = all_markdown(at)
    assert "Sovereign AI Workbench" in text and "Runs 100% offline on this machine." in text
    for label in ("Ollama", "Sandbox", "Tesseract", "Knowledge base", "185 chunks"):
        assert label in text
    assert "wb-dot wb-bad" not in text and text.count("wb-dot wb-ok") == 4
    assert [b.key for b in at.sidebar.button if b.key.startswith("scn_")] == [f"scn_{s.key}" for s in SCENARIOS]
    assert at.sidebar.radio(key="mode_label").value == "Guided"
    for section in ("Router", "Plan", "Timeline", "Files", "Network"):
        assert f">{section}<" in text
    assert not at.error


def test_renders_with_unhealthy_backend(monkeypatch):
    at = run_app(monkeypatch, FakeClient(ok_health(ok=False, chunks=0)))
    text = all_markdown(at)
    assert text.count("wb-dot wb-bad") == 4
    assert "Docker not running or image missing" in text and "run scripts/ingest.py" in text


def test_backend_down_banner(monkeypatch):
    down = ApiResult(error=ErrorInfo(code="INTERNAL", message="refused", retryable=True), failure="unreachable")
    at = run_app(monkeypatch, FakeClient(down))
    assert any(f"Backend not reachable at {BASE_URL}" in e.value for e in at.error)
    assert len(at.sidebar.button) == 0 and len(at.columns) == 0  # rest of the page not shown


def test_version_mismatch_shows_red_warning(monkeypatch):
    result = ApiResult(data=health(), status_code=200, contract_version="0.9.0")
    at = run_app(monkeypatch, FakeClient(result))
    assert any("Contract version mismatch" in e.value for e in at.sidebar.error)


def test_health_api_error_is_friendly(monkeypatch):
    result = ApiResult(error=ErrorInfo(code="INTERNAL", message="Unexpected server error"), failure="api",
                       status_code=500, contract_version=CONTRACT_VERSION)
    at = run_app(monkeypatch, FakeClient(result))
    assert any("Health check failed: Unexpected server error" in e.value for e in at.sidebar.error)


# ---------------------------------------------------------------- actions
@pytest.mark.parametrize("scn", SCENARIOS, ids=lambda s: s.key)
def test_scenario_button_creates_task(monkeypatch, scn):
    fake = FakeClient(ok_health())
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key=f"scn_{scn.key}").click().run()
    assert not at.exception
    assert len(fake.created) == 1
    req = fake.created[0]
    assert req.message == scn.prompt and req.scenario == scn.scenario and req.mode == TaskMode.GUIDED
    if scn.demo_file:
        assert fake.uploads == [(scn.demo_file.name, scn.demo_file.stat().st_size, scn.mime_type)]
        assert req.file_ids == ["f_000000000001"]
    else:
        assert fake.uploads == [] and req.file_ids == []
    assert at.session_state["task_id"] == "t_0123456789ab"
    assert any("t_0123456789ab" in c.value for c in at.code)


def test_scenario_button_uses_agent_mode(monkeypatch):
    fake = FakeClient(ok_health())
    at = run_app(monkeypatch, fake)
    at.sidebar.radio(key="mode_label").set_value("Agent").run()
    at.sidebar.button(key="scn_code_calc").click().run()
    assert fake.created[0].mode == TaskMode.AGENT and fake.created[0].scenario == Scenario.CODE_CALC


def test_chat_creates_task_without_scenario(monkeypatch):
    fake = FakeClient(ok_health())
    at = run_app(monkeypatch, fake)
    at.chat_input(key="chat").set_value("Summarise the SOP on confined spaces").run()
    assert not at.exception
    req = fake.created[0]
    assert req.message == "Summarise the SOP on confined spaces" and req.scenario is None
    assert req.mode == TaskMode.GUIDED and req.file_ids == []
    assert at.session_state["messages"][0] == {"role": "user", "text": "Summarise the SOP on confined spaces"}


def test_create_task_error_is_friendly(monkeypatch):
    fake = FakeClient(ok_health())
    fake.create_task = lambda req: ApiResult(error=ErrorInfo(code="BAD_REQUEST", message="scenario missing"),
                                             failure="api", status_code=422)
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="scn_code_calc").click().run()
    assert not at.exception
    assert any("Could not start the task: scenario missing" in e.value for e in at.sidebar.error)
    assert "task_id" not in at.session_state


def test_prewarm_shows_items(monkeypatch):
    fake = FakeClient(ok_health())
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="prewarm").click().run()
    assert fake.prewarm_calls == 1
    assert any("general (9.1 s)" in s.value and "13.1 s" in s.value for s in at.sidebar.success)


def test_reset_clears_state(monkeypatch):
    fake = FakeClient(ok_health())
    at = run_app(monkeypatch, fake)
    at.sidebar.radio(key="mode_label").set_value("Agent").run()
    at.sidebar.button(key="scn_code_calc").click().run()
    assert "task_id" in at.session_state
    at.sidebar.button(key="reset").click().run()
    assert not at.exception
    assert "task_id" not in at.session_state and "messages" not in at.session_state
    assert at.sidebar.radio(key="mode_label").value == "Guided"


# ---------------------------------------------------------------- scenarios / config data
@pytest.mark.parametrize("scn", SCENARIOS, ids=lambda s: s.key)
def test_scenario_data(scn):
    assert scn.prompt.strip() and scn.title.strip() and scn.description.strip()
    assert len(scn.prompt) <= MAX_MESSAGE_CHARS
    assert scn.default_mode == TaskMode.GUIDED
    if scn.demo_file is not None:
        assert scn.demo_file.is_file() and scn.demo_file.stat().st_size > 0, scn.demo_file
        assert scn.demo_file.suffix.lstrip(".") in ALLOWED_UPLOAD_TYPES and scn.mime_type


def test_scenarios_cover_contract():
    assert {s.scenario for s in SCENARIOS} == set(Scenario)
    assert get_scenario(Scenario.PID_TAGS).demo_file is not None
    assert get_scenario(Scenario.CODE_CALC).demo_file is None


def test_allowed_upload_types_match_contract():
    message = ERROR_CODES["UNSUPPORTED_FILE"]
    assert "/".join(ALLOWED_UPLOAD_TYPES) in message
