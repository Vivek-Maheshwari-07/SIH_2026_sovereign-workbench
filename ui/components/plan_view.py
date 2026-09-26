"""B5: plan steps as a checklist that ticks as step_start / tool_result events arrive."""
from __future__ import annotations

from typing import Literal, Optional

import streamlit as st

from shared.contracts import AgentEvent, EventType, PlanStep, TaskState, TaskStatus
from ui.components.theme import esc

StepState = Literal["pending", "current", "done", "failed"]
MARKS = {"pending": "○", "current": "▸", "done": "✓", "failed": "✕"}


def step_states(plan: list[PlanStep], events: list[AgentEvent],
                final: Optional[TaskState] = None) -> list[tuple[PlanStep, StepState]]:
    """A step is done when its own tool reports ok or a later step starts; failed when its tool fails."""
    started: list[int] = []
    ok_steps: set[int] = set()
    failed_steps: set[int] = set()
    tools = {s.index: s.tool for s in plan}
    for ev in events:
        if ev.type == EventType.STEP_START:
            index = ev.data.get("index", ev.step)
            if isinstance(index, int):
                started.append(index)
        elif ev.type == EventType.TOOL_RESULT and isinstance(ev.step, int):
            tool = tools.get(ev.step)
            if tool is None or ev.data.get("tool") == tool:
                (ok_steps if ev.data.get("ok") else failed_steps).add(ev.step)
    last_started = max(started, default=0)
    status = final.status if final else None

    result: list[tuple[PlanStep, StepState]] = []
    for step in plan:
        if step.index in failed_steps:
            state: StepState = "failed"
        elif step.index in ok_steps or step.index < last_started or status == TaskStatus.SUCCEEDED:
            state = "done"
        elif step.index == last_started:
            state = "failed" if status == TaskStatus.FAILED else ("pending" if status else "current")
        else:
            state = "pending"
        result.append((step, state))
    return result


def plan_html(states: list[tuple[PlanStep, StepState]]) -> str:
    if not states:
        return ('<div class="wb-h">Plan</div>'
                '<div class="wb-empty">The steps show here once the job is planned.</div>')
    rows = []
    for step, state in states:
        tool = f' <span class="tool">{esc(step.tool)}</span>' if step.tool else ""
        rows.append(f'<li class="{state}"><span class="mk">{MARKS[state]}</span>'
                    f'<span>{step.index}. {esc(step.title)}{tool}</span></li>')
    return '<div class="wb-h">Plan</div><ul class="wb-plan">' + "".join(rows) + "</ul>"


def render(plan: list[PlanStep], events: list[AgentEvent], final: Optional[TaskState] = None) -> None:
    st.markdown(plan_html(step_states(plan, events, final)), unsafe_allow_html=True)
