"""B8 tests: backend-down banner and recovery, version banner, lost jobs, lamp fix hints, friendly errors."""
from __future__ import annotations

from pathlib import Path

from fake_client import BASE_URL, TASK_ID, FakeClient, all_event_pages, health, ok
from streamlit.testing.v1 import AppTest

from shared.contracts import CONTRACT_VERSION, ErrorInfo, Scenario, TaskStatus
from ui import api_client, messages
from ui.api_client import ApiResult, PollResult

APP = str(Path(__file__).resolve().parents[2] / "ui" / "app.py")
DOWN = ApiResult(error=ErrorInfo(code="INTERNAL", message="refused", retryable=True), failure="unreachable")


def run_app(monkeypatch, fake: FakeClient) -> AppTest:
    monkeypatch.setattr(api_client, "get_client", lambda: fake)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def html(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def expire_health_cache(at: AppTest) -> None:
    at.session_state["health_cache"] = (0.0, at.session_state["health_cache"][1])


# ---------------------------------------------------------------- backend down / recovery
def test_backend_down_banner_with_retry(monkeypatch):
    at = run_app(monkeypatch, FakeClient(DOWN))
    text = html(at)
    assert f"Backend not reachable at {BASE_URL}" in text
    assert "checks again every 3 s and comes back by itself" in text and "Checks so far: 1" in text
    assert len(at.sidebar.button) == 0


def test_page_recovers_by_itself(monkeypatch):
    fake = FakeClient(DOWN)
    at = run_app(monkeypatch, fake)
    fake.health_result = ok(health())       # backend is back
    at.run()                                # the retry fragment re-checks and reloads the page
    assert not at.exception
    assert "Backend not reachable" not in html(at)
    assert [b.key for b in at.sidebar.button if b.key.startswith("wo_")]
    assert "down_checks" not in at.session_state


def test_backend_dying_mid_session_shows_banner(monkeypatch):
    fake = FakeClient()
    at = run_app(monkeypatch, fake)
    fake.health_result = DOWN
    expire_health_cache(at)
    at.run()
    assert "Backend not reachable" in html(at)


# ---------------------------------------------------------------- version banner
def test_version_mismatch_banner_from_health_body(monkeypatch):
    body = health().model_copy(update={"contract_version": "1.1.0"})
    at = run_app(monkeypatch, FakeClient(ApiResult(data=body, status_code=200, contract_version=CONTRACT_VERSION)))
    text = html(at)
    assert "Contract version mismatch" in text and "1.1.0" in text and "git pull" in text


def test_no_version_banner_when_equal(monkeypatch):
    assert "Contract version mismatch" not in html(run_app(monkeypatch, FakeClient()))


# ---------------------------------------------------------------- lamp fix hints
def test_only_red_lamps_get_a_fix_hint(monkeypatch):
    body = health().model_copy(update={"ollama_ok": False, "sandbox_ok": False, "status": "degraded"})
    text = html(run_app(monkeypatch, FakeClient(ok(body))))
    assert '<span class="fix">Start Ollama</span>' in text
    assert '<span class="fix">Open Docker Desktop</span>' in text
    assert "Install Tesseract" not in text and "scripts/ingest.py" not in text


# ---------------------------------------------------------------- lost job after a backend restart
def gone(task_id, after=0):
    return PollResult(next_seq=after, error=ErrorInfo(code="TASK_NOT_FOUND", message="task_id does not exist."))


def test_task_not_found_offers_run_again(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="wo_code_calc").click().run()
    fake.poll_events = gone                               # backend restarted
    at.run()
    assert "The backend restarted, this job was lost. Run it again." in html(at)
    assert not at.session_state["job"].active             # polling stopped
    fake.poll_events = FakeClient(pages=all_event_pages()).poll_events
    at.button(key="rerun_job").click().run()
    assert not at.exception
    assert len(fake.created) == 2 and fake.created[1].scenario == Scenario.CODE_CALC
    assert fake.created[1].message == fake.created[0].message
    assert not at.session_state["job"].lost


def test_lost_chat_job_with_files_asks_to_attach_again(monkeypatch):
    from ui.components import timeline

    fake = FakeClient()
    fake.poll_events = gone
    monkeypatch.setattr(api_client, "get_client", lambda: fake)
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["job"] = timeline.Job(task_id=TASK_ID, message="Summarise this", had_files=True)
    at.run()
    assert "this job was lost" in html(at)
    assert not any(b.key == "rerun_job" for b in at.button)
    assert any("Attach them again" in c.value for c in at.caption)


def test_failed_job_shows_what_to_do(monkeypatch):
    fake = FakeClient(pages=all_event_pages(), final_status=TaskStatus.FAILED)
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="wo_code_calc").click().run()
    for _ in range(3):
        at.run()
    assert "What to do: The job took too long. Press Prewarm models, then run it again." in html(at)


# ---------------------------------------------------------------- close job / last job / reset
def test_close_job_keeps_it_as_last_finished(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="wo_code_calc").click().run()
    for _ in range(3):
        at.run()
    at.button(key="close_job").click().run()
    assert "job" not in at.session_state and "task" not in at.query_params
    text = html(at)
    assert "Last finished job" in text and TASK_ID in text and "solution.py" in text and "WO-B" in text
    assert "How a job runs" in text


def test_idle_screen_finds_last_job_in_audit_log(monkeypatch):
    from datetime import timedelta

    from fake_client import T0
    from shared.contracts import AuditRecord

    fake = FakeClient()
    fake.created.append(__import__("shared.contracts", fromlist=["TaskCreate"]).TaskCreate(message="old job"))
    fake.audit_records = [AuditRecord(ts=T0 + timedelta(minutes=5), task_id=TASK_ID, kind="system",
                                      name="task_finished", detail={"status": "succeeded"})]
    text = html(run_app(monkeypatch, fake))
    assert "Last finished job" in text and TASK_ID in text and "t = 7.246 mm" in text


def test_reset_clears_everything(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = run_app(monkeypatch, fake)
    at.sidebar.button(key="wo_code_calc").click().run()
    for _ in range(3):
        at.run()
    at.segmented_control(key="view").set_value("Network").run()
    at.button(key="probe").click().run()
    assert "probe_result" in at.session_state and "artifact_cache" in at.session_state
    at.sidebar.button(key="reset").click().run()
    assert not at.exception
    for key in ("job", "probe_result", "artifact_cache", "job_history", "prewarm_result"):
        assert key not in at.session_state, key
    assert "task" not in at.query_params
    assert at.segmented_control(key="view").value == "Workbench"


# ---------------------------------------------------------------- friendly messages (pure)
def test_friendly_messages_say_what_and_what_to_do():
    unreachable = messages.friendly("Upload failed", ErrorInfo(code="INTERNAL", message="x"), "unreachable")
    assert "not answering" in unreachable and "uvicorn backend.main:app" in unreachable
    assert "Wait a moment" in messages.friendly("X", ErrorInfo(code="INTERNAL", message="x"), "timeout")
    too_big = messages.friendly("report.pdf could not be uploaded",
                                ErrorInfo(code="UNSUPPORTED_FILE", message="File type .exe is not allowed."), "api")
    assert too_big.startswith("report.pdf could not be uploaded: File type .exe is not allowed. Use one of: PDF")
    assert "Open Docker Desktop" in messages.friendly("Job", ErrorInfo(code="SANDBOX_UNAVAILABLE", message="no"))
    assert "Traceback" not in messages.friendly("Job", ErrorInfo(code="WEIRD", message="odd"))
