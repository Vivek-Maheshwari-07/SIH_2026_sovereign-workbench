"""B5 tests: router badge, plan checklist and the deliverables tray (previews made inside the tests)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from fake_client import (
    ROUTE,
    TASK_ID,
    FakeClient,
    all_event_pages,
    artifact,
    docx_bytes,
    xlsx_bytes,
)
from streamlit.testing.v1 import AppTest

from shared.contracts import ArtifactKind, ErrorInfo, PlanStep, TaskStatus
from ui import api_client
from ui.components.artifacts import build_preview, docx_paragraphs, xlsx_table
from ui.components.plan_view import plan_html, step_states
from ui.components.router_badge import badge_html

APP = str(Path(__file__).resolve().parents[2] / "ui" / "app.py")
PLAN = [PlanStep(index=1, title="Write code", tool="run_code_task"), PlanStep(index=2, title="Report", tool="finish")]

DOCX = artifact(ArtifactKind.DOCX, "approval_note.docx", 0, 2)
XLSX = artifact(ArtifactKind.XLSX, "pid_tags.xlsx", 0, 3)
PY = artifact(ArtifactKind.PY, "solution.py", 0, 4)


def events_upto(seq: int):
    return [e for page in all_event_pages() for e in page if e.seq <= seq]


def finished_app(monkeypatch, fake: FakeClient) -> AppTest:
    monkeypatch.setattr(api_client, "get_client", lambda: fake)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="wo_code_calc").click().run()
    for _ in range(4):
        at.run()
    assert not at.exception, [e.message for e in at.exception]
    return at


# ---------------------------------------------------------------- router badge
def test_badge_shows_model_layer_confidence_and_reason():
    text = badge_html(ROUTE)
    assert "qwen2.5-coder:3b" in text and "coding" in text and "rule (keyword match)" in text
    assert "100%" in text and "matched &#x27;function&#x27;" in text


def test_badge_empty_state():
    assert "picks a model when the job starts" in badge_html(None)


def test_badge_reason_in_app(monkeypatch):
    at = finished_app(monkeypatch, FakeClient(pages=all_event_pages()))
    assert "Rule: Coding keywords -&gt; code specialist model" in "\n".join(m.value for m in at.markdown)


# ---------------------------------------------------------------- plan checklist
def test_plan_ticks_as_events_arrive():
    assert [s for _, s in step_states(PLAN, events_upto(2))] == ["pending", "pending"]
    assert [s for _, s in step_states(PLAN, events_upto(3))] == ["current", "pending"]
    assert [s for _, s in step_states(PLAN, events_upto(12))] == ["done", "pending"]   # run_code_task ok
    assert [s for _, s in step_states(PLAN, events_upto(13))] == ["done", "current"]
    assert [s for _, s in step_states(PLAN, events_upto(15))] == ["done", "done"]


def test_plan_nested_tool_failure_does_not_fail_step():
    # the sandbox run inside step 1 failed (seq 8), but step 1's own tool is run_code_task
    assert [s for _, s in step_states(PLAN, events_upto(8))] == ["current", "pending"]


def test_plan_failed_task_marks_current_step():
    final = FakeClient(final_status=TaskStatus.FAILED).get_task(TASK_ID).data
    assert [s for _, s in step_states(PLAN, events_upto(5), final)] == ["failed", "pending"]


def test_plan_html_highlights_current():
    text = plan_html(step_states(PLAN, events_upto(3)))
    assert '<li class="current">' in text and "1. Write code" in text and "run_code_task" in text
    assert "once the job is planned" in plan_html([])


# ---------------------------------------------------------------- previews (files made here)
def test_docx_preview():
    content = docx_bytes(["APPROVAL NOTE", "", "Subject: Tank T-104", "Finding 1: shell thinning"])
    assert docx_paragraphs(content) == ["APPROVAL NOTE", "Subject: Tank T-104", "Finding 1: shell thinning"]


def test_xlsx_preview_first_50_rows():
    content = xlsx_bytes([{"tag": f"P-{i:03d}", "equipment_type": "Pump"} for i in range(80)])
    table = xlsx_table(content)
    assert isinstance(table, pd.DataFrame) and len(table) == 50 and list(table.columns) == ["tag", "equipment_type"]


def test_py_preview():
    assert build_preview(ArtifactKind.PY, b"def f():\n    return 1\n").startswith("def f()")


def test_tray_renders_all_three_previews(monkeypatch):
    files = {
        DOCX.artifact_id: docx_bytes(["APPROVAL NOTE", "Recommendation: replace plates"]),
        XLSX.artifact_id: xlsx_bytes([{"tag": "P-101A", "equipment_type": "Pump"}]),
        PY.artifact_id: b"def pipe_wall_thickness(P, D, S):\n    return P * D / (2 * S)\n",
    }
    fake = FakeClient(pages=all_event_pages(), artifacts=[DOCX, XLSX, PY], files=files)
    at = finished_app(monkeypatch, fake)
    text = "\n".join(m.value for m in at.markdown)
    assert "approval_note.docx" in text and "pid_tags.xlsx" in text and "solution.py" in text
    assert "Recommendation: replace plates" in text                                # docx paragraphs
    assert len(at.dataframe) == 1 and at.dataframe[0].value.iloc[0]["tag"] == "P-101A"   # xlsx table
    assert any("pipe_wall_thickness" in c.value for c in at.code)                  # py code
    assert {b.label for b in at.get("download_button")} == {"Download"}
    assert len(at.get("download_button")) == 3


def test_download_bytes_cached(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    at = finished_app(monkeypatch, fake)
    at.run()
    at.run()
    assert fake.downloads.count("a_000000000001") == 1


def test_failed_download_is_friendly(monkeypatch):
    fake = FakeClient(pages=all_event_pages())
    fake.download_error = ErrorInfo(code="FILE_NOT_FOUND", message="no artifact with id 'a_000000000001'")
    at = finished_app(monkeypatch, fake)
    assert any("Could not fetch solution.py from the backend" in w.value for w in at.warning)
    assert len(at.get("download_button")) == 0
    fake.download_error = None
    at.button(key="retry_a_000000000001").click().run()
    assert not at.exception and len(at.get("download_button")) == 1


def test_broken_preview_is_friendly(monkeypatch):
    bad = artifact(ArtifactKind.XLSX, "broken.xlsx", 0, 9)
    fake = FakeClient(pages=all_event_pages(), artifacts=[bad], files={bad.artifact_id: b"not a real xlsx"})
    at = finished_app(monkeypatch, fake)
    assert any("No preview for this file" in i.value for i in at.info)
    assert len(at.get("download_button")) == 1   # download still offered
