"""
Temporary echo agent — ticket A8 replaces this with the real agent loop.
Emits exactly 5 events in order (route, plan, step_start, log, final),
using the real router from ticket A2, with a small pause between each so
the events can be watched arrive while polling.
"""
from __future__ import annotations

import time

from backend.router import route
from backend.task_store import TaskHandle
from shared.contracts import EventType, PlanStep, RouteRequest

_STEP_DELAY_S = 0.5


def run(handle: TaskHandle) -> None:
    decision = route(RouteRequest(message=handle.message, file_ids=handle.file_ids))
    handle.set_route(decision)
    handle.emit(
        EventType.ROUTE,
        f"Routed to {decision.task_type.value}",
        {"decision": decision.model_dump(mode="json")},
    )
    time.sleep(_STEP_DELAY_S)
    if handle.is_cancelled():
        return

    plan = [PlanStep(index=1, title="Echo the request back")]
    handle.set_plan(plan)
    handle.emit(
        EventType.PLAN,
        "Planned 1 step",
        {"steps": [step.model_dump(mode="json") for step in plan]},
    )
    time.sleep(_STEP_DELAY_S)
    if handle.is_cancelled():
        return

    handle.emit(
        EventType.STEP_START,
        plan[0].title,
        {"index": plan[0].index, "title": plan[0].title},
        step=plan[0].index,
    )
    time.sleep(_STEP_DELAY_S)
    if handle.is_cancelled():
        return

    handle.emit(
        EventType.LOG,
        "Echoing message",
        {"level": "info", "text": f"Echo agent (temporary, see ticket A8) received: {handle.message!r}"},
    )
    time.sleep(_STEP_DELAY_S)
    if handle.is_cancelled():
        return

    answer = f"Echo: {handle.message}"
    handle.set_final_answer(answer)
    handle.emit(EventType.FINAL, "Done", {"answer": answer})
