"""
App shell tests: ui/app.py rendered with Streamlit's AppTest and a fake ApiClient (no backend),
plus checks on ui/scenarios.py and ui/config.py.
"""
from __future__ import annotations

import html
from pathlib import Path

import pytest
from fake_client import BASE_URL, TASK_ID, FakeClient, health, net_status, ok
from streamlit.testing.v1 import AppTest

from shared.contracts import CONTRACT_VERSION, ERROR_CODES, ErrorInfo, Scenario, TaskMode
from ui import api_client
from ui.api_client import ApiResult
from ui.components.net_faceplate import faceplate_html
from ui.config import ALLOWED_UPLOAD_TYPES, MAX_MESSAGE_CHARS
from ui.scenarios import SCENARIOS, get_scenario

APP = str(Path(__file__).resolve().parents[2] / "ui" / "app.py")


def run_app(monkeypatch, fake: FakeClient, query: dict | None = None) -> AppTest:
    monkeypatch.setattr(api_client, "get_client", lambda: fake)
    at = AppTest.from_file(APP, default_timeout=30)
    for k, v in (query or {}).items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def page_html(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)  # includes the sidebar


# ---------------------------------------------------------------- rendering
def test_renders_with_healthy_backend(monkeypatch):
    at = run_app(monkeypatch, FakeClient())
    text = page_html(at)
    assert "Sovereign AI Workbench" in text and "Runs 100% offline on this machine." in text
    for label in ("Ollama", "Sandbox", "Tesseract", "Knowledge base", "185 chunks"):
        assert label in text
    assert text.count('<i class="b-ok">') == 4 and 'class="b-alarm"' not in text
    assert [b.key for b in at.sidebar.button if b.key.startswith("wo_")] == [f"wo_{s.key}" for s in SCENARIOS]
    for scn in SCENARIOS:
        assert scn.work_order in at.sidebar.button(key=f"wo_{scn.key}").label
        assert html.escape(f"{scn.input_type} → {scn.output_type}") in text
    assert at.sidebar.radio(key="mode_label").value == "Guided"
    assert "No job running. Pick a work order on the left or describe the job below." in text
    assert "Deliverables" in text and "Filled in" not in text
    assert not at.error


def test_renders_with_unhealthy_backend(monkeypatch):
    at = run_app(monkeypatch, FakeClient(ok(health(ok=False, chunks=0))))
    text = page_html(at)
    assert text.count('<i class="b-alarm">') == 4
    assert "Docker not running or image missing" in text and "run scripts/ingest.py" in text


def test_backend_down_banner(monkeypatch):
    down = ApiResult(error=ErrorInfo(code="INTERNAL", message="refused", retryable=True), failure="unreachable")
    at = run_app(monkeypatch, FakeClient(down))
    assert any(f"Backend not reachable at {BASE_URL}" in e.value for e in at.error)
    assert len(at.sidebar.button) == 0 and len(at.columns) == 0  # rest of the page not shown


def test_version_mismatch_shows_red_warning(monkeypatch):
    at = run_app(monkeypatch, FakeClient(ApiResult(data=health(), status_code=200, contract_version="0.9.0")))
    assert any("Contract version mismatch" in e.value for e in at.sidebar.error)


def test_health_api_error_is_friendly(monkeypatch):
    result = ApiResult(error=ErrorInfo(code="INTERNAL", message="Unexpected server error"), failure="api",
                       status_code=500, contract_version=CONTRACT_VERSION)
    at = run_app(monkeypatch, FakeClient(result))
    assert any("Health check failed: Unexpected server error" in e.value for e in at.sidebar.error)


# ---------------------------------------------------------------- NET-001 faceplate
def test_faceplate_green_when_zero():
    html = faceplate_html(net_status(seen=0, now=0, firewall=True))
    assert "NET-001" in html and 'val wb-num c-ok">0<' in html and "Blocked" in html


def test_faceplate_red_when_external_connections():
    html = faceplate_html(net_status(seen=2, now=1, firewall=False))
    assert 'val wb-num c-alarm">2<' in html and "open now: 1" in html and "Open" in html


def test_faceplate_without_data():
    assert "No network data" in faceplate_html(None, "refused")


def test_faceplate_red_in_app(monkeypatch):
    fake = FakeClient()
    fake.network_result = ok(net_status(seen=3, now=0, firewall=None))
    text = page_html(run_app(monkeypatch, fake))
    assert 'c-alarm">3<' in text and "Unknown" in text


# ---------------------------------------------------------------- actions
@pytest.mark.parametrize("scn", SCENARIOS, ids=lambda s: s.key)
def test_work_order_creates_task(monkeypatch, scn):
    fake = FakeClient()
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key=f"wo_{scn.key}").click().run()
    assert not at.exception
    assert len(fake.created) == 1
    req = fake.created[0]
    assert req.message == scn.prompt and req.scenario == scn.scenario and req.mode == TaskMode.GUIDED
    if scn.demo_file:
        assert fake.uploads == [(scn.demo_file.name, scn.demo_file.stat().st_size, scn.mime_type)]
        assert req.file_ids == ["f_000000000001"]
    else:
        assert fake.uploads == [] and req.file_ids == []
    job = at.session_state["job"]
    assert job.task_id == TASK_ID and job.work_order == scn.work_order
    assert at.query_params["task"] == [TASK_ID]
    assert f"Job {TASK_ID}" in page_html(at)


def test_work_order_uses_agent_mode(monkeypatch):
    fake = FakeClient()
    at = run_app(monkeypatch, fake)
    at.sidebar.radio(key="mode_label").set_value("Agent").run()
    at.sidebar.button(key="wo_code_calc").click().run()
    assert fake.created[0].mode == TaskMode.AGENT and fake.created[0].scenario == Scenario.CODE_CALC


def test_chat_creates_task_without_scenario(monkeypatch):
    fake = FakeClient()
    at = run_app(monkeypatch, fake)
    at.chat_input(key="chat").set_value("Summarise the SOP on confined spaces").run()
    assert not at.exception
    req = fake.created[0]
    assert req.message == "Summarise the SOP on confined spaces" and req.scenario is None
    assert req.mode == TaskMode.GUIDED and req.file_ids == []
    assert at.session_state["job"].message == "Summarise the SOP on confined spaces"
    assert "Summarise the SOP on confined spaces" in page_html(at)  # the user's message is shown


def test_create_task_error_is_friendly(monkeypatch):
    fake = FakeClient()
    fake.create_task = lambda req: ApiResult(error=ErrorInfo(code="BAD_REQUEST", message="scenario missing"),
                                             failure="api", status_code=422)
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="wo_code_calc").click().run()
    assert not at.exception
    assert any("Could not start the job: scenario missing" in e.value for e in at.sidebar.error)
    assert "job" not in at.session_state


def test_prewarm_shows_items(monkeypatch):
    fake = FakeClient()
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="prewarm").click().run()
    assert fake.prewarm_calls == 1
    assert any("general (9.1 s)" in s.value and "13.1 s" in s.value for s in at.sidebar.success)


def test_reset_clears_state(monkeypatch):
    fake = FakeClient()
    at = run_app(monkeypatch, fake)
    at.sidebar.radio(key="mode_label").set_value("Agent").run()
    at.sidebar.button(key="wo_code_calc").click().run()
    assert "job" in at.session_state
    at.sidebar.button(key="reset").click().run()
    assert not at.exception
    assert "job" not in at.session_state and "task" not in at.query_params
    assert at.sidebar.radio(key="mode_label").value == "Guided"


def test_reload_resumes_job_from_url(monkeypatch):
    from fake_client import all_event_pages

    fake = FakeClient(pages=all_event_pages())
    at = run_app(monkeypatch, fake, query={"task": TASK_ID})
    assert at.session_state["job"].task_id == TASK_ID
    assert fake.polls[0] == 0  # a new session starts from after=0


# ---------------------------------------------------------------- scenarios / config data
@pytest.mark.parametrize("scn", SCENARIOS, ids=lambda s: s.key)
def test_scenario_data(scn):
    assert scn.prompt.strip() and scn.title.strip() and scn.description.strip()
    assert scn.work_order.startswith("WO-") and scn.input_type and scn.output_type
    assert len(scn.prompt) <= MAX_MESSAGE_CHARS
    assert scn.default_mode == TaskMode.GUIDED
    if scn.demo_file is not None:
        assert scn.demo_file.is_file() and scn.demo_file.stat().st_size > 0, scn.demo_file
        assert scn.demo_file.suffix.lstrip(".") in ALLOWED_UPLOAD_TYPES and scn.mime_type


def test_scenarios_cover_contract():
    assert {s.scenario for s in SCENARIOS} == set(Scenario)
    assert [s.work_order for s in SCENARIOS] == ["WO-A", "WO-B", "WO-C"]
    assert get_scenario(Scenario.PID_TAGS).demo_file is not None
    assert get_scenario(Scenario.CODE_CALC).demo_file is None


def test_allowed_upload_types_match_contract():
    assert "/".join(ALLOWED_UPLOAD_TYPES) in ERROR_CODES["UNSUPPORTED_FILE"]
