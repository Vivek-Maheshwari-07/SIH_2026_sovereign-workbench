"""
Guided flows (ticket A8, second half): fixed pipelines for the three demo
scenarios, using the SAME tools as the agent (agent_tools.execute_tool) so the
timeline shows the same event types. Used for mode="guided", and as the
agent-mode safety net when the agent cannot finish.

Scenario rule (pick_scenario):
  1. TaskCreate.scenario, if given.
  2. Otherwise from the route decision + attachments:
     - an image attached, or route "vision"            -> PID_TAGS
     - route "coding"                                  -> CODE_CALC
     - route "document" with a non-image file attached -> INSPECTION_NOTE
     - anything else                                   -> no scenario
"""
from __future__ import annotations

import re
from typing import Any, Optional, Protocol

from backend.agent_tools import ToolContext, ToolOutcome, execute_tool
from backend.file_store import file_store
from backend.tools import office
from shared.contracts import ArtifactKind, EventType, PlanStep, RouteDecision, Scenario, TaskType

# ---- named constants (no .env key exists for these)
SEARCH_QUERY_MAX_CHARS = 240
MAX_KB_REFS = 3
FINAL_STEPS_MAX_CHARS = 1200
FINDING_WORDS = ("thickness", "thinning", "corrosion", "pitting", "crack", "leak", "weep", "defect",
                 "damage", "dent", "erosion", "rust")
_FINDING_LABEL_RE = re.compile(r"^(?:\d+\.\s*)?(?:findings\s*)?(?:finding\s*\d+\s*[-:.]\s*)?", re.IGNORECASE)

GUIDED_PLANS: dict[Scenario, list[tuple[str, Optional[str]]]] = {
    Scenario.INSPECTION_NOTE: [("Read the inspection report", "read_document"),
                               ("Find the relevant SOP passages", "search_knowledge"),
                               ("Draft the approval note", "draft_approval_note"),
                               ("Summarise the result", "finish")],
    Scenario.CODE_CALC: [("Write, test and run the calculation code", "run_code_task"),
                         ("Report the calculation steps", "finish")],
    Scenario.PID_TAGS: [("Read the P&ID drawing in tiles", "read_document"),
                        ("Extract the tag list to Excel", "extract_pid_tags"),
                        ("Summarise the tag counts", "finish")],
}
EXPECTED_ARTIFACT: dict[Scenario, ArtifactKind] = {
    Scenario.INSPECTION_NOTE: ArtifactKind.DOCX,
    Scenario.CODE_CALC: ArtifactKind.PY,
    Scenario.PID_TAGS: ArtifactKind.XLSX,
}


class GuidedError(Exception):
    """A guided step failed. `code` is a key from shared.contracts.ERROR_CODES."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Runner(Protocol):
    """What the guided flow needs from the agent run (implemented by agent.AgentRun)."""
    ctx: ToolContext
    step: int

    def checkpoint(self) -> None: ...
    def emit(self, event_type: EventType, title: str, data: dict[str, Any], step: Optional[int] = None) -> None: ...


# ------------------------------------------------------------------ scenario choice
def pick_scenario(scenario: Optional[Scenario], decision: Optional[RouteDecision],
                  file_ids: list[str]) -> Optional[Scenario]:
    if scenario is not None:
        return scenario
    refs = [r for r in (file_store.get_ref(f) for f in file_ids) if r is not None]
    has_image = any(r.is_image for r in refs)
    has_document = any(not r.is_image for r in refs)
    task_type = decision.task_type if decision else None
    if has_image or task_type == TaskType.VISION:
        return Scenario.PID_TAGS
    if task_type == TaskType.CODING:
        return Scenario.CODE_CALC
    if task_type == TaskType.DOCUMENT and has_document:
        return Scenario.INSPECTION_NOTE
    return None


def guided_plan(scenario: Scenario) -> list[PlanStep]:
    return [PlanStep(index=i, title=title, tool=tool) for i, (title, tool) in enumerate(GUIDED_PLANS[scenario], 1)]


def _first_file(file_ids: list[str], want_image: bool) -> Optional[str]:
    for file_id in file_ids:
        ref = file_store.get_ref(file_id)
        if ref is not None and ref.is_image == want_image:
            return file_id
    return file_ids[0] if file_ids else None


def search_query_from_text(text: str) -> str:
    """
    Build a KB query from the report's damage statements (fallback: its first
    words). Text is split into sentences because OCR does not keep line
    breaks reliably; "Finding N -" labels and scope sentences are skipped.
    """
    sentences = [" ".join(s.split()) for s in re.split(r"(?<=[.;])\s+|\n+", text)]
    picked = []
    for sentence in sentences:
        lower = sentence.lower()
        if not sentence or lower.startswith(("1. scope", "scope")) or lower.startswith("severity"):
            continue
        if any(word in lower for word in FINDING_WORDS):
            picked.append(_FINDING_LABEL_RE.sub("", sentence))
    query = " ".join(picked[:3]) or " ".join(text.split()[:40])
    return query[:SEARCH_QUERY_MAX_CHARS].strip() or "inspection repair criteria"


# ------------------------------------------------------------------ running
def _step(runner: Runner, tool: str, args: dict[str, Any]) -> ToolOutcome:
    runner.checkpoint()
    runner.step += 1
    runner.ctx.step = runner.step
    runner.emit(EventType.STEP_START, f"Step {runner.step}: {tool}", {"index": runner.step, "title": tool},
                step=runner.step)
    outcome = execute_tool(runner.ctx, tool, args)
    runner.checkpoint()
    if not outcome.ok:
        raise GuidedError(outcome.error_code or "INTERNAL", f"{tool} failed: {outcome.summary}")
    return outcome


def _finish(runner: Runner, answer: str) -> str:
    return _step(runner, "finish", {"answer": answer}).finish_answer or answer


def _scenario_a(runner: Runner, file_ids: list[str]) -> str:
    file_id = _first_file(file_ids, want_image=False)
    if file_id is None:
        raise GuidedError("BAD_REQUEST", "The inspection-note scenario needs an attached report.")
    _step(runner, "read_document", {"file_id": file_id})
    doc_id = list(runner.ctx.scratchpad.docs)[-1]
    doc = runner.ctx.scratchpad.docs[doc_id]
    before = set(runner.ctx.scratchpad.kb_hits)
    _step(runner, "search_knowledge", {"query": search_query_from_text(doc.full_text()), "top_k": 4})
    kb_ids = [k for k in runner.ctx.scratchpad.kb_hits if k not in before][:MAX_KB_REFS]
    outcome = _step(runner, "draft_approval_note", {"doc_id": doc_id, "kb_ref_ids": kb_ids})
    note = list(runner.ctx.scratchpad.notes.values())[-1]
    high = sum(f.severity.value in ("high", "critical") for f in note.findings)
    filename = outcome.summary.split(" saved as ", 1)[-1].split(":", 1)[0]
    answer = (f"Approval note drafted with {len(note.findings)} findings ({high} high/critical) and "
              f"{len(note.sop_references)} SOP reference(s). Recommendation: {note.recommendation} "
              f"File: {filename}")
    return _finish(runner, answer)


def _scenario_b(runner: Runner, request: str) -> str:
    _step(runner, "run_code_task", {"request": request})
    result = list(runner.ctx.scratchpad.code_results.values())[-1]
    steps = result.stdout_tail.strip()[:FINAL_STEPS_MAX_CHARS] or "(no steps printed)"
    answer = (f"Calculation code passed its tests ({result.passed} passed, {result.failed} failed, "
              f"{result.attempts} attempt(s)).\nCalculation steps:\n{steps}")
    return _finish(runner, answer)


def _scenario_c(runner: Runner, file_ids: list[str]) -> str:
    file_id = _first_file(file_ids, want_image=True)
    if file_id is None:
        raise GuidedError("BAD_REQUEST", "The P&ID scenario needs an attached drawing.")
    _step(runner, "read_document", {"file_id": file_id, "kind": "pid"})
    doc_id = list(runner.ctx.scratchpad.docs)[-1]
    _step(runner, "extract_pid_tags", {"doc_id": doc_id})
    tag_list = list(runner.ctx.scratchpad.tag_lists.values())[-1]
    rows = office.dedupe_tags(tag_list)
    counts = ", ".join(f"{name}: {n}" for name, n in office.count_by_type(rows)) or "none"
    return _finish(runner, f"Found {len(rows)} unique tags. Per equipment type: {counts}.")


def run_guided(runner: Runner, scenario: Scenario, message: str, file_ids: list[str],
               set_plan=None) -> str:
    """Emit the fixed plan and run the scenario's tools in order. Returns the final answer."""
    plan = guided_plan(scenario)
    if set_plan is not None:
        set_plan(plan)
    runner.emit(EventType.PLAN, f"Guided plan ({scenario.value}) with {len(plan)} steps",
                {"steps": [s.model_dump(mode="json") for s in plan]})
    if scenario == Scenario.INSPECTION_NOTE:
        return _scenario_a(runner, file_ids)
    if scenario == Scenario.CODE_CALC:
        return _scenario_b(runner, message)
    if scenario == Scenario.PID_TAGS:
        return _scenario_c(runner, file_ids)
    raise GuidedError("BAD_REQUEST", f"unknown scenario {scenario!r}")


def has_expected_artifact(ctx_artifacts: list, scenario: Scenario) -> bool:
    kind = EXPECTED_ARTIFACT[scenario]
    return any(a.kind == kind for a in ctx_artifacts)

