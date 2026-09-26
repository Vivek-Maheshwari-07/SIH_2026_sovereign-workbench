"""
B4 tests: live job panel (ui/components/timeline.py). AppTest runs the real app with a fake client
that releases one page of events per poll; each at.run() is one poll of the fragment.
"""
from __future__ import annotations

from pathlib import Path

from fake_client import TASK_ID, FakeClient, all_event_pages, ev
from streamlit.testing.v1 import AppTest

from shared.contracts import EventType, PlanStep, TaskStatus
from ui import api_client
from ui.components import timeline
from ui.components.timeline import build_stages, journal_html, journal_row, tool_code

APP = str(Path(__file__).resolve().parents[2] / "ui" / "app.py")


def start_job(monkeypatch, fake: FakeClient) -> AppTest:
    """Open the app and click WO-B; the click run already does the first poll."""
    monkeypatch.setattr(api_client, "get_client", lambda: fake)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="wo_code_calc").click().run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def html(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def job(at: AppTest) -> timeline.Job:
    return at.session_state["job"]


# ---------------------------------------------------------------- polling behaviour
def test_every_event_type_renders(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = start_job(monkeypatch, fake)
    for _ in range(4):
        at.run()
        assert not at.exception
    text = html(at)
    for label in ("Route", "Plan", "Step", "Model", "Tool call", "Result", "File", "Note", "Final", "Error"):
        assert f'<td class="k">{label}</td>' in text, label
    assert "coding → qwen2.5-coder:3b (rule, 100%)" in text                  # route
    assert "500 tokens (10.0 tok/s)" in text and "50.0 s" in text            # llm_call
    assert '<span class="bad">failed</span> sandbox: 1 failed' in text        # tool_result failed
    assert '<span class="ok">ok</span> run_code_task' in text                # tool_result ok
    assert '<tr class="warn">' in text and "Attempt 1 failed, retrying" in text   # log warn
    assert '<tr class="log">' in text                                        # log info (grey)
    assert "MODEL_TIMEOUT: slow model (retrying, job continues)" in text      # error
    assert "solution.py ready" in text                                       # artifact
    assert "Result" in text and "t = 7.246 mm" in text and "Total time 01:06" in text


def test_retryable_error_does_not_stop_polling(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = start_job(monkeypatch, fake)          # poll 1: route + plan
    at.run()                                    # poll 2: includes the retryable error event
    assert any(e.type == EventType.ERROR for e in job(at).events)
    assert job(at).done is False and fake.task_gets == 0
    polls_before = len(fake.polls)
    at.run()
    assert len(fake.polls) == polls_before + 1  # still polling after the error row
    assert fake.polls[-1] == 6                  # cursor = next_seq of the previous page
    assert "Running" in html(at)


def test_done_stops_polling_and_fetches_state_once(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = start_job(monkeypatch, fake)
    for _ in range(3):
        at.run()
    assert job(at).done and fake.task_gets == 1
    polls = len(fake.polls)
    for _ in range(3):
        at.run()
    assert len(fake.polls) == polls and fake.task_gets == 1
    seqs = [e.seq for e in job(at).events]
    assert seqs == list(range(1, 17))           # no gaps, no duplicates
    assert "Succeeded" in html(at)


def test_success_or_failure_comes_from_task_state(monkeypatch):
    # the event stream ends with a FINAL event, but the TaskState says failed: the UI must say failed
    fake = FakeClient(pages=all_event_pages(), final_status=TaskStatus.FAILED)
    at = start_job(monkeypatch, fake)
    for _ in range(3):
        at.run()
    text = html(at)
    assert "Job failed (AGENT_TIMEOUT)" in text and "Task exceeded 600 s" in text
    assert "Succeeded" not in text


def test_cancel_calls_api(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = start_job(monkeypatch, fake)
    at.button(key="cancel_job").click().run()
    assert not at.exception
    assert fake.cancelled == [TASK_ID]
    assert job(at).cancel_sent and "Stopping" in html(at)
    assert at.button(key="cancel_job").disabled


def test_cancel_error_is_friendly(monkeypatch):
    from shared.contracts import ErrorInfo
    from ui.api_client import ApiResult

    fake = FakeClient(pages=all_event_pages())
    fake.cancel_task = lambda tid: ApiResult(error=ErrorInfo(code="TASK_NOT_FOUND", message="gone"), failure="api")
    at = start_job(monkeypatch, fake)
    at.button(key="cancel_job").click().run()
    assert any("Could not cancel the job: gone" in e.value for e in at.error)


def test_poll_error_keeps_polling(monkeypatch):
    from shared.contracts import ErrorInfo
    from ui.api_client import PollResult

    fake = FakeClient(pages=all_event_pages())
    real = fake.poll_events
    calls = {"n": 0}

    def flaky(task_id, after=0):
        calls["n"] += 1
        if calls["n"] == 2:
            return PollResult(next_seq=after, error=ErrorInfo(code="INTERNAL", message="refused", retryable=True))
        return real(task_id, after)

    fake.poll_events = flaky
    at = start_job(monkeypatch, fake)
    at.run()
    assert any("Lost contact with the backend" in w.value for w in at.warning)
    at.run()
    assert not at.warning and len(job(at).events) > 2


def test_unknown_task_stops_polling(monkeypatch):
    from shared.contracts import ErrorInfo
    from ui.api_client import PollResult

    fake = FakeClient()
    fake.poll_events = lambda tid, after=0: PollResult(next_seq=after, error=ErrorInfo(code="TASK_NOT_FOUND",
                                                                                      message="task_id does not exist."))
    at = start_job(monkeypatch, fake)
    assert job(at).done and fake.task_gets == 0
    assert any("task_id does not exist" in e.value for e in at.error)


def test_live_elapsed_and_queued_state(monkeypatch):
    fake = FakeClient(pages=[[], *all_event_pages()])  # first poll: nothing yet (queued)
    at = start_job(monkeypatch, fake)
    assert "Queued" in html(at) and "Waiting for the first event" in html(at)
    assert 'class="wb-clock"' in html(at)


# ---------------------------------------------------------------- process line (pure)
def events_upto(seq: int):
    return [e for page in all_event_pages() for e in page if e.seq <= seq]


PLAN = [PlanStep(index=1, title="Write code", tool="run_code_task"), PlanStep(index=2, title="Report", tool="finish")]


def test_process_line_waiting_stages_from_plan():
    stages = build_stages(events_upto(2), PLAN, None)
    assert [s.code for s in stages] == ["RT", "PL", "CD", "RP", "FA"]
    assert [s.state for s in stages] == ["done", "done", "active", "wait", "wait"]


def test_process_line_active_and_failed_bubbles():
    stages = build_stages(events_upto(8), PLAN, None)
    by_code = [(s.code, s.state) for s in stages]
    assert ("SX", "failed") in by_code            # sandbox tool_result ok=False
    assert ("CD", "active") in by_code            # run_code_task still open, innermost open call
    html_text = timeline.process_line_html(stages)
    assert 'class="wb-bub active"' in html_text and 'class="ring"' in html_text
    assert 'class="wb-bub failed"' in html_text and 'class="wb-pipe wait"' in html_text


def test_process_line_all_done():
    stages = build_stages(events_upto(16), PLAN, None, job_done=True)
    assert all(s.state in ("done", "failed") for s in stages)
    assert stages[-1].code == "FA" and stages[-1].state == "done"


def test_process_line_marks_open_step_failed_when_task_failed():
    from fake_client import FakeClient as _F

    final = _F(final_status=TaskStatus.FAILED).get_task(TASK_ID).data
    stages = build_stages(events_upto(5), PLAN, final, job_done=True)
    assert [s.state for s in stages if s.code in ("CD", "FA")] == ["failed", "failed"]


def test_tool_codes():
    assert tool_code("sandbox") == "SX" and tool_code("read_document") == "RD"
    assert tool_code("brand_new_tool") == "BN" and tool_code("zip") == "ZI"


def test_journal_uses_exact_data_keys():
    row = journal_row(ev(1, EventType.LLM_CALL, "t", {"model_id": "general", "purpose": "plan",
                                                     "duration_ms": 2000, "tokens_out": None}))
    assert row[1] == "Model" and "general: plan" in row[2] and "tok/s" not in row[2] and row[3] == "2.0 s"
    bad = journal_row(ev(2, EventType.TOOL_RESULT, "t", {}))  # missing keys never crash
    assert bad[0] == "alarm"
    assert journal_html([]) == '<table class="wb-jr"></table>'
