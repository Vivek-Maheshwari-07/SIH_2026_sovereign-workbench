"""
The 3 demo scenarios as data (Track B, ticket B3).

Prompts are copied (not imported: the UI never imports backend code) from the runs that
were tested end to end with these exact inputs:
  * inspection_note, pid_tags: scripts/sovereign_proof.py (INSPECTION_MESSAGE, PID_MESSAGE),
    which ran them on the same demo/inputs/ files (docs/proof/2026-09-26_1225: succeeded).
  * code_calc: backend/flows/code_flow.py PIPE_THICKNESS_DEMO_REQUEST, used by
    tests/track_a/test_e2e.py and tests/contract (it pins the function signature and the
    expected result, so the small coder model passes more reliably than a shorter prompt).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared.contracts import Scenario, TaskMode

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_INPUTS_DIR = _REPO_ROOT / "demo" / "inputs"


@dataclass(frozen=True)
class DemoScenario:
    scenario: Scenario
    work_order: str                      # "WO-A", shown in the sidebar and the job header
    title: str
    description: str
    input_type: str
    output_type: str
    prompt: str
    default_mode: TaskMode = TaskMode.GUIDED
    demo_file: Optional[Path] = None     # uploaded before the task is created
    mime_type: Optional[str] = None      # of demo_file

    @property
    def key(self) -> str:
        return self.scenario.value


SCENARIOS: list[DemoScenario] = [
    DemoScenario(
        scenario=Scenario.INSPECTION_NOTE,
        work_order="WO-A",
        title="Inspection approval note",
        description="Drafts the approval note and cites the SOPs.",
        input_type="Scanned PDF",
        output_type="Word approval note",
        prompt="Draft an approval note from this scanned inspection report, citing the relevant SOPs.",
        demo_file=DEMO_INPUTS_DIR / "scenario_a_report_1.pdf",
        mime_type="application/pdf",
    ),
    DemoScenario(
        scenario=Scenario.CODE_CALC,
        work_order="WO-B",
        title="Pipe wall thickness calc",
        description="Writes code and tests, runs them offline.",
        input_type="Text request",
        output_type="Python code + tests",
        prompt=(
            "Write a function for pipe wall thickness from design pressure, outside diameter "
            "and allowable stress, using t = P*D / (2*S), with tests, and print the calculation steps. "
            "The function must be exactly `def pipe_wall_thickness(P: float, D: float, S: float) -> float` "
            "(P in MPa, D in mm, S in MPa, returns t in mm). "
            "In the `if __name__ == \"__main__\":` block use exactly P = 10 MPa, D = 200 mm, S = 138 MPa "
            "and print each step; the result is t = 10 * 200 / (2 * 138) = 7.246 mm."
        ),
    ),
    DemoScenario(
        scenario=Scenario.PID_TAGS,
        work_order="WO-C",
        title="P&ID tag register",
        description="Lists every equipment and instrument tag.",
        input_type="P&ID image",
        output_type="Excel tag list",
        prompt="Extract every equipment and instrument tag from this P&ID drawing into an Excel tag list.",
        demo_file=DEMO_INPUTS_DIR / "scenario_c_pid_generated.png",
        mime_type="image/png",
    ),
]


def get_scenario(scenario: Scenario) -> DemoScenario:
    return next(s for s in SCENARIOS if s.scenario == scenario)


# ---------------------------------------------------------------- example requests (Agent mode)
@dataclass(frozen=True)
class ExampleRequest:
    """A one-click free-form request that shows a general capability (no fixed pipeline behind it)."""
    key: str
    label: str                           # button text
    shows: str                           # tooltip: the capability it demonstrates
    prompt: str
    demo_file: Optional[Path] = None
    mime_type: Optional[str] = None


# Prompts as run end to end against the real backend in the evaluation pass (docs/known_issues.md).
EXAMPLES: list[ExampleRequest] = [
    ExampleRequest(
        key="ask_sop", label="Ask the SOPs",
        shows="Searches the offline SOP library and answers with page citations.",
        prompt="What does our SOP say about H2S exposure limits and what PPE is needed?",
    ),
    ExampleRequest(
        key="deck", label="Briefing deck",
        shows="Builds a PowerPoint deck from the SOP library.",
        prompt="Make a short PowerPoint deck for the shift briefing on H2S safety precautions.",
    ),
    ExampleRequest(
        key="table", label="Analyse a table",
        shows="The code model writes pandas code, runs it in the offline sandbox, and returns Excel.",
        prompt="Compute the wall loss percentage for each item and flag anything above 15 percent loss.",
        demo_file=DEMO_INPUTS_DIR / "equipment_thickness.csv", mime_type="text/csv",
    ),
    ExampleRequest(
        key="note", label="Read handwriting",
        shows="The vision model reads a photographed handwritten field note.",
        prompt="Read this handwritten shift-round note and list the problems found and the actions needed.",
        demo_file=DEMO_INPUTS_DIR / "field_note_handwritten.jpg", mime_type="image/jpeg",
    ),
]
