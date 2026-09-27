"""
Evaluation pass: agent-mode fixes. The P&ID tag list re-reads an image in tile mode, and an
agent-mode safety net only picks a guided scenario that fits the request. No Ollama needed.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from backend import agent_tools
from backend.agent_tools import DocEntry, ToolContext
from backend.file_store import file_store
from backend.flows import guided
from backend.registry import registry
from backend.settings import settings
from backend.tools.documents import ExtractResult, PageResult
from shared.contracts import RouteDecision, Scenario, TaskType


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WB_WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(settings, "WB_CACHE_DIR", tmp_path / "cache")


def png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def ctx_with_events() -> tuple[ToolContext, list]:
    events: list = []
    ctx = ToolContext(task_id="t_test00000000", emit=lambda *a, **k: events.append(a), add_artifact=lambda a: None)
    return ctx, events


def decision(task_type: TaskType) -> RouteDecision:
    model = registry.model_for_task(task_type)
    return RouteDecision(task_type=task_type, model_id=model.id, ollama_name=model.ollama_name,
                         reason="test", layer="forced", confidence=1.0)


# ---------------------------------------------------------------- P&ID re-read
def test_ensure_pid_read_rereads_an_image_in_tile_mode(monkeypatch):
    file_id = file_store.save("drawing.png", png_bytes(), "image/png").file_id
    tiles = [PageResult(page=1, text="P-101 FT-201", method="ocr_tiles", tile="top-left")]
    calls = []
    monkeypatch.setattr(agent_tools, "extract", lambda fid, kind="auto": calls.append(kind) or ExtractResult(pages=tiles))
    ctx, events = ctx_with_events()
    doc = DocEntry(doc_id="doc_1", file_id=file_id, filename="drawing.png", kind="auto",
                   pages=[PageResult(page=1, text="few words", method="ocr")])
    out = agent_tools.ensure_pid_read(ctx, doc)
    assert calls == ["pid"] and out.kind == "pid" and out.pages == tiles
    assert any(e[1] == "Re-read as P&ID" for e in events)


def test_ensure_pid_read_leaves_pid_reads_and_documents_alone(monkeypatch):
    monkeypatch.setattr(agent_tools, "extract", lambda *a, **k: pytest.fail("must not re-read"))
    ctx, _ = ctx_with_events()
    image_id = file_store.save("drawing.png", png_bytes(), "image/png").file_id
    text_id = file_store.save("notes.txt", b"plain notes", "text/plain").file_id
    pid_doc = DocEntry("doc_1", image_id, "drawing.png", "pid", [])
    text_doc = DocEntry("doc_2", text_id, "notes.txt", "auto", [])
    assert agent_tools.ensure_pid_read(ctx, pid_doc) is pid_doc
    assert agent_tools.ensure_pid_read(ctx, text_doc) is text_doc


# ---------------------------------------------------------------- strict fallback scenario
@pytest.mark.parametrize("scenario,message,files,fits", [
    (Scenario.PID_TAGS, "List the tags on this P&ID", [], True),
    (Scenario.PID_TAGS, "What is wrong with the pipe in this photo?", [], False),
    (Scenario.INSPECTION_NOTE, "Draft an approval note from this inspection report", [], True),
    (Scenario.INSPECTION_NOTE, "Summarise this manual", [], False),
    (Scenario.CODE_CALC, "Write a python function", [], True),
    (Scenario.CODE_CALC, "Analyse this data in python", ["data.csv"], False),
])
def test_fits_request(scenario, message, files, fits):
    assert guided.fits_request(scenario, message, files) is fits


def test_strict_pick_drops_a_scenario_that_does_not_fit():
    photo = file_store.save("site_photo.png", png_bytes(), "image/png").file_id
    vision = decision(TaskType.VISION)
    assert guided.pick_scenario(None, vision, [photo], "Is this flange leaking?") == Scenario.PID_TAGS
    assert guided.pick_scenario(None, vision, [photo], "Is this flange leaking?", strict=True) is None
    assert guided.pick_scenario(None, vision, [photo], "List all tags in this drawing", strict=True) == Scenario.PID_TAGS


def test_strict_pick_keeps_an_explicit_scenario():
    assert guided.pick_scenario(Scenario.CODE_CALC, None, [], "anything", strict=True) == Scenario.CODE_CALC


def test_strict_pick_no_code_fallback_for_table_files():
    table = file_store.save("equipment.csv", b"tag,thk\nV-1,10\n", "text/csv").file_id
    coding = decision(TaskType.CODING)
    assert guided.pick_scenario(None, coding, [table], "compute wall loss in python", strict=True) is None
    assert guided.pick_scenario(None, coding, [], "write a python function", strict=True) == Scenario.CODE_CALC


# ---------------------------------------------------------------- router: spreadsheet rule
def test_router_sends_an_attached_spreadsheet_to_the_code_model():
    from backend.router import route
    from shared.contracts import RouteRequest

    table = file_store.save("equipment.csv", b"tag,thk\nV-1,10\n", "text/csv").file_id
    decision = route(RouteRequest(message="Which items need attention?", file_ids=[table]))
    assert decision.task_type == TaskType.CODING and decision.layer == "rule"
    assert "Spreadsheet attached" in decision.reason
    assert decision.model_id == registry.model_for_task(TaskType.CODING).id


@pytest.mark.parametrize("scenario,message,asks", [
    (Scenario.PID_TAGS, "Extract all tags from this P&ID into an Excel tag list.", True),
    (Scenario.PID_TAGS, "What equipment is shown in this drawing?", False),
    (Scenario.INSPECTION_NOTE, "Draft an approval note from this report", True),
    (Scenario.INSPECTION_NOTE, "How many findings are in this inspection report?", False),
    (None, "Draft an approval note", False),
])
def test_asks_for_deliverable(scenario, message, asks):
    assert guided.asks_for_deliverable(scenario, message) is asks


def test_a_deck_from_an_inspection_report_does_not_ask_for_a_note():
    assert not guided.asks_for_deliverable(Scenario.INSPECTION_NOTE, "Draft a PowerPoint deck from this inspection report")
