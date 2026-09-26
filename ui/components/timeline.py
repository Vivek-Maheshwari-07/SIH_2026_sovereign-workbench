"""
B4: live job panel. Polls /events every second inside a fragment (only this panel reruns),
draws the run as a process line (pipe + ISA instrument bubbles) and an event journal.

Rules:
  * next_seq lives in the Job in session state; a new session (page reload) starts at after=0.
  * An error event (even a retryable one) is only a red journal row; polling goes on until
    EventsPage.done. Success or failure is decided only from the final TaskState.
  * When done: stop polling, fetch TaskState once, show the answer and the total time.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

import streamlit as st
from pydantic import ValidationError

from shared.contracts import (
    AgentEvent,
    Artifact,
    ErrorInfo,
    EventType,
    PlanStep,
    RouteDecision,
    TaskMode,
    TaskState,
    TaskStatus,
)
from ui import api_client
from ui.components import plan_view, router_badge
from ui.components.theme import ACTIVE, ALARM, DONE, FACE, INK_2, LINE, esc, fmt_seconds

JOB_KEY = "job"
POLL_S = 1.0

BubbleState = Literal["wait", "active", "open", "done", "failed"]

TOOL_CODES = {
    "read_document": "RD", "search_knowledge": "KB", "draft_approval_note": "DN",
    "extract_pid_tags": "TG", "run_code_task": "CD", "sandbox": "SX", "finish": "RP",
}
TOOL_LABELS = {
    "read_document": "Read document", "search_knowledge": "Search SOPs", "draft_approval_note": "Draft note",
    "extract_pid_tags": "Extract tags", "run_code_task": "Write and test code", "finish": "Report",
}
KIND_LABELS = {
    EventType.ROUTE: "Route", EventType.PLAN: "Plan", EventType.STEP_START: "Step", EventType.LLM_CALL: "Model",
    EventType.TOOL_CALL: "Tool call", EventType.TOOL_RESULT: "Result", EventType.ARTIFACT: "File",
    EventType.LOG: "Note", EventType.FINAL: "Final", EventType.ERROR: "Error",
}


# ---------------------------------------------------------------- job state
@dataclass
class Job:
    task_id: str
    message: str = ""
    mode: Optional[TaskMode] = None
    work_order: Optional[str] = None           # "WO-B" for demo scenarios
    after: int = 0                             # next_seq cursor
    events: list[AgentEvent] = field(default_factory=list)
    done: bool = False
    final: Optional[TaskState] = None
    final_error: Optional[ErrorInfo] = None    # TaskState fetch failed
    state_fetches: int = 0
    poll_error: Optional[ErrorInfo] = None     # last poll failed (we keep polling)
    started_at: float = field(default_factory=time.time)
    cancel_sent: bool = False
    cancel_error: Optional[ErrorInfo] = None
    announced: bool = False                    # full-page rerun done after finishing

    @property
    def active(self) -> bool:
        return not self.done or (self.final is None and self.final_error is None)


def get_job() -> Optional[Job]:
    return st.session_state.get(JOB_KEY)


def set_job(job: Optional[Job]) -> None:
    if job is None:
        st.session_state.pop(JOB_KEY, None)
    else:
        st.session_state[JOB_KEY] = job


def poll_once(job: Job) -> None:
    """One events poll. Errors are remembered and shown; the cursor only moves on success."""
    result = api_client.get_client().poll_events(job.task_id, job.after)
    if result.error is not None:
        if result.error.code == "TASK_NOT_FOUND":  # e.g. backend restarted: the job is gone, stop polling
            job.done, job.final_error, job.poll_error = True, result.error, None
            return
        job.poll_error = result.error
        return
    job.poll_error = None
    last = job.events[-1].seq if job.events else 0
    job.events.extend(e for e in result.events if e.seq > last)
    job.after = result.next_seq
    job.done = result.done


def fetch_final(job: Job) -> None:
    job.state_fetches += 1
    result = api_client.get_client().get_task(job.task_id)
    job.final, job.final_error = result.data, result.error


# ---------------------------------------------------------------- derived data
def route_of(job: Job) -> Optional[RouteDecision]:
    if job.final and job.final.route:
        return job.final.route
    for ev in job.events:
        if ev.type == EventType.ROUTE:
            try:
                return RouteDecision.model_validate(ev.data.get("decision"))
            except ValidationError:
                return None
    return None


def plan_of(job: Job) -> list[PlanStep]:
    if job.final and job.final.plan:
        return job.final.plan
    for ev in reversed(job.events):
        if ev.type == EventType.PLAN:
            try:
                return [PlanStep.model_validate(s) for s in ev.data.get("steps", [])]
            except (ValidationError, TypeError):
                return []
    return []


def artifacts_of(job: Job) -> list[Artifact]:
    if job.final is not None:
        return job.final.artifacts
    found: list[Artifact] = []
    for ev in job.events:
        if ev.type == EventType.ARTIFACT:
            try:
                found.append(Artifact.model_validate(ev.data.get("artifact")))
            except ValidationError:
                continue
    return found


def elapsed_s(job: Job) -> float:
    if job.final is not None and job.final.elapsed_s is not None:
        return job.final.elapsed_s
    if job.events:
        return (datetime.now(timezone.utc) - job.events[0].ts).total_seconds()
    return time.time() - job.started_at


# ---------------------------------------------------------------- process line
@dataclass
class Stage:
    code: str
    caption: str
    state: BubbleState = "wait"
    tool: Optional[str] = None
    step: Optional[int] = None


def tool_code(tool: str) -> str:
    if tool in TOOL_CODES:
        return TOOL_CODES[tool]
    parts = [p for p in tool.replace("-", "_").split("_") if p]
    return ("".join(p[0] for p in parts[:2]) if len(parts) > 1 else tool[:2]).upper() or "??"


def build_stages(events: list[AgentEvent], plan: list[PlanStep], final: Optional[TaskState],
                 job_done: bool = False) -> list[Stage]:
    """route, plan, one bubble per tool call (waiting bubbles for planned steps), final."""
    route = next((e for e in events if e.type == EventType.ROUTE), None)
    plan_ev = next((e for e in events if e.type == EventType.PLAN), None)
    route_cap = "Route"
    if route is not None:
        model = (route.data.get("decision") or {}).get("model_id")
        route_cap = f"Route: {model}" if model else "Route"
    plan_cap = f"Plan: {len(plan)} steps" if plan else "Plan"
    head = [Stage("RT", route_cap, "done" if route else "wait"),
            Stage("PL", plan_cap, "done" if plan_ev or plan else "wait")]

    calls: list[Stage] = []
    open_calls: list[Stage] = []
    for ev in events:
        tool = ev.data.get("tool")
        if ev.type == EventType.TOOL_CALL and isinstance(tool, str):
            stage = Stage(tool_code(tool), TOOL_LABELS.get(tool, ev.title), "open", tool, ev.step)
            calls.append(stage)
            open_calls.append(stage)
        elif ev.type == EventType.TOOL_RESULT and isinstance(tool, str):
            match = next((s for s in reversed(open_calls) if s.tool == tool), None)
            if match is not None:
                match.state = "done" if ev.data.get("ok") else "failed"
                open_calls.remove(match)

    body: list[Stage] = []
    for step in plan:
        own = [s for s in calls if s.step == step.index]
        if own:
            body.extend(own)
        else:
            code = tool_code(step.tool) if step.tool else f"S{step.index}"
            body.append(Stage(code, step.title, "wait", step.tool, step.index))
    body.extend(s for s in calls if s not in body)

    has_final = any(e.type == EventType.FINAL for e in events)
    tail = Stage("FA", "Final answer", "done" if has_final else "wait")
    stages = head + body + [tail]

    status = final.status if final else None
    if status == TaskStatus.FAILED:
        for s in stages:
            if s.state == "open":
                s.state = "failed"
        if tail.state != "done":
            tail.state = "failed"
    elif status == TaskStatus.CANCELLED:
        for s in stages:
            if s.state == "open":
                s.state = "wait"
    elif not job_done:
        opened = [s for s in stages if s.state == "open"]
        if opened:
            opened[-1].state = "active"
        else:
            nxt = next((s for s in stages if s.state == "wait"), None)
            if nxt is not None and events:
                nxt.state = "active"
    return stages


def bubble_svg(stage: Stage, number: int) -> str:
    fill, stroke, ink, dash = {
        "done": (DONE, DONE, "#FFFFFF", ""),
        "active": (ACTIVE, ACTIVE, "#FFFFFF", ""),
        "open": (FACE, ACTIVE, ACTIVE, ""),
        "failed": (ALARM, ALARM, "#FFFFFF", ""),
        "wait": (FACE, LINE, INK_2, ' stroke-dasharray="3 2"'),
    }[stage.state]
    ring = '<circle class="ring" cx="23" cy="23" r="20"/>' if stage.state == "active" else ""
    return (
        f'<svg width="46" height="46" viewBox="0 0 46 46" role="img" aria-label="{esc(stage.caption)}: {stage.state}">'
        f'{ring}<circle cx="23" cy="23" r="20" fill="{fill}" stroke="{stroke}" stroke-width="2"{dash}/>'
        f'<line x1="3" y1="23" x2="43" y2="23" stroke="{ink}" stroke-width="1" opacity="0.8"/>'
        f'<text x="23" y="19" text-anchor="middle" fill="{ink}">{esc(stage.code)}</text>'
        f'<text x="23" y="36" text-anchor="middle" fill="{ink}">{number:02d}</text></svg>'
    )


def process_line_html(stages: list[Stage]) -> str:
    parts = []
    for i, stage in enumerate(stages, start=1):
        pipe = "wb-pipe wait" if stage.state == "wait" else "wb-pipe"
        parts.append(f'<div class="wb-st"><div class="{pipe}"></div>'
                     f'<div class="wb-bub {stage.state}">{bubble_svg(stage, i)}'
                     f'<div class="cap">{esc(stage.caption)}</div></div></div>')
    return '<div class="wb-pl" aria-label="Process line">' + "".join(parts) + "</div>"


# ---------------------------------------------------------------- event journal
def _secs(ms: object) -> str:
    return f"{ms / 1000:.1f} s" if isinstance(ms, (int, float)) else ""


def _first_line(text: object, limit: int = 160) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def journal_row(ev: AgentEvent) -> tuple[str, str, str, str]:
    """(row class, kind label, text html, duration) for one event, using the contract data keys."""
    d = ev.data or {}
    kind, cls, dur = KIND_LABELS.get(ev.type, ev.type.value), "", ""
    if ev.type == EventType.ROUTE:
        dec = d.get("decision") or {}
        text = (f"{esc(dec.get('task_type', '?'))} → {esc(dec.get('ollama_name', '?'))}"
                f" ({esc(dec.get('layer', '?'))}, {round(float(dec.get('confidence', 0)) * 100)}%)")
    elif ev.type == EventType.PLAN:
        steps = d.get("steps") or []
        text = f"{len(steps)} steps: " + esc("; ".join(str(s.get("title", "")) for s in steps if isinstance(s, dict)))
    elif ev.type == EventType.STEP_START:
        text = f"Step {esc(d.get('index', ev.step))}: {esc(d.get('title', ev.title))}"
    elif ev.type == EventType.LLM_CALL:
        ms, tokens = d.get("duration_ms"), d.get("tokens_out")
        text = f"{esc(d.get('model_id', '?'))}: {esc(d.get('purpose', ev.title))}"
        if isinstance(tokens, int):
            text += f", {tokens} tokens"
            if isinstance(ms, (int, float)) and ms > 0:
                text += f" ({tokens / (ms / 1000):.1f} tok/s)"
        dur = _secs(ms)
    elif ev.type == EventType.TOOL_CALL:
        tool = d.get("tool", "?")
        text = f"{esc(tool)}" + (f": {esc(ev.title)}" if ev.title and ev.title != tool else "")
    elif ev.type == EventType.TOOL_RESULT:
        ok = bool(d.get("ok"))
        flag = '<span class="ok">ok</span>' if ok else '<span class="bad">failed</span>'
        text = f"{flag} {esc(d.get('tool', '?'))}: {esc(_first_line(d.get('summary')))}"
        dur = _secs(d.get("duration_ms"))
        cls = "" if ok else "alarm"
    elif ev.type == EventType.ARTIFACT:
        art = d.get("artifact") or {}
        text = f"{esc(art.get('filename', ev.title))} ready"
    elif ev.type == EventType.LOG:
        warn = d.get("level") == "warn"
        cls, text = ("warn" if warn else "log"), esc(d.get("text", ev.title))
    elif ev.type == EventType.FINAL:
        text = esc(_first_line(d.get("answer", ev.title)))
    elif ev.type == EventType.ERROR:
        err = d.get("error") or {}
        text = f"{esc(err.get('code', 'ERROR'))}: {esc(err.get('message', ev.title))}"
        if err.get("retryable"):
            text += " (retrying, job continues)"
        cls = "alarm"
    else:
        text = esc(ev.title)
    return cls, kind, text, dur


def journal_html(events: list[AgentEvent]) -> str:
    rows = []
    for ev in reversed(events):  # newest first, like a DCS event log
        cls, kind, text, dur = journal_row(ev)
        ts = ev.ts.astimezone().strftime("%H:%M:%S")
        rows.append(f'<tr class="{cls}"><td class="t">{ts}</td><td class="k">{esc(kind)}</td>'
                    f'<td class="x">{text}</td><td class="d">{dur}</td></tr>')
    return '<table class="wb-jr">' + "".join(rows) + "</table>"


# ---------------------------------------------------------------- rendering
STATE_STYLE = {
    "queued": ("Queued", "c-dim"), "running": ("Running", "c-active"), "stopping": ("Stopping", "c-warn"),
    "succeeded": ("Succeeded", "c-ok"), "failed": ("Failed", "c-alarm"), "cancelled": ("Cancelled", "c-dim"),
}


def live_state(job: Job) -> str:
    if job.final is not None:
        return job.final.status.value
    if job.cancel_sent:
        return "stopping"
    return "running" if job.events else "queued"


def header(job: Job) -> None:
    label, css = STATE_STYLE[live_state(job)]
    what = " · ".join(x for x in (job.work_order, job.mode.value.capitalize() if job.mode else None) if x)
    left, mid, right = st.columns([5, 1.3, 1.7], vertical_alignment="center")
    left.markdown(
        f'<div class="wb-job"><span class="id">Job {esc(job.task_id)}</span>'
        f'<span class="wb-state {css}">{label}</span><span class="wb-meta">{esc(what)}</span></div>',
        unsafe_allow_html=True)
    mid.markdown(f'<div class="wb-clock" title="Elapsed time">{fmt_seconds(elapsed_s(job))}</div>',
                 unsafe_allow_html=True)
    if job.active and not job.done:
        right.button("Cancel job", key="cancel_job", disabled=job.cancel_sent, width="stretch",
                     on_click=cancel_job, args=(job,))
    st.markdown(f'<div class="wb-job"><span class="msg" title="{esc(job.message or (job.final.message if job.final else ''))}">'
                f'{esc(job.message or (job.final.message if job.final else "")) or "Resumed after page reload"}</span></div>', unsafe_allow_html=True)
    if job.cancel_error is not None:
        st.error(f"Could not cancel the job: {job.cancel_error.message}")


def cancel_job(job: Job) -> None:
    """Button callback (runs before the rerun, so the header already shows "Stopping")."""
    result = api_client.get_client().cancel_task(job.task_id)
    job.cancel_error = result.error
    job.cancel_sent = result.error is None


def clear_final_error(job: Job) -> None:
    job.final_error = None


def multiline(text: str) -> str:
    """Escape and keep line breaks (Streamlit's markdown pass would collapse them)."""
    return "<br>".join(esc(line) for line in text.splitlines())


def result_box(job: Job) -> None:
    if job.final_error is not None:
        st.error(f"The job finished, but its result could not be loaded: {job.final_error.message}")
        st.button("Load result again", key="reload_final", on_click=clear_final_error, args=(job,))
        return
    final = job.final
    if final is None:
        return
    total = f"Total time {fmt_seconds(elapsed_s(job))}"
    if final.status == TaskStatus.SUCCEEDED:
        body = multiline(final.final_answer or "The job finished without a written answer.")
        st.markdown(f'<div class="wb-result"><div class="wb-h">Result</div><div class="wb-meta">{total}</div>'
                    f'<div class="ans">{body}</div></div>', unsafe_allow_html=True)
    elif final.status == TaskStatus.FAILED:
        msg = multiline(final.error.message if final.error else "No error details were reported.")
        code = esc(final.error.code) if final.error else "FAILED"
        st.markdown(f'<div class="wb-result failed"><div class="wb-h c-alarm">Job failed ({code})</div>'
                    f'<div class="wb-meta">{total}</div><div class="ans">{msg}</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="wb-result cancelled"><div class="wb-h">Job cancelled</div>'
                    f'<div class="wb-meta">{total}</div></div>', unsafe_allow_html=True)


def idle_panel() -> None:
    st.markdown(process_line_html(build_stages([], [], None)), unsafe_allow_html=True)
    st.markdown('<div class="wb-empty">No job running. Pick a work order on the left or describe the job below.</div>',
                unsafe_allow_html=True)


def job_panel() -> None:
    job = get_job()
    if job is None:
        idle_panel()
        return
    if not job.done:
        poll_once(job)
    if job.done and job.final is None and job.final_error is None:
        fetch_final(job)

    header(job)
    if job.poll_error is not None:
        st.warning(f"Lost contact with the backend ({job.poll_error.message}). Retrying every second.")
    plan = plan_of(job)
    st.markdown(process_line_html(build_stages(job.events, plan, job.final, job.done)), unsafe_allow_html=True)
    left, right = st.columns(2, gap="medium")
    with left:
        router_badge.render(route_of(job))
    with right:
        plan_view.render(plan, job.events, job.final)
    result_box(job)
    st.markdown('<div class="wb-h" style="margin-top:8px">Event journal</div>', unsafe_allow_html=True)
    with st.container(height=230, border=False, key="journal"):
        if job.events:
            st.markdown(journal_html(job.events), unsafe_allow_html=True)
        else:
            st.markdown('<div class="wb-empty">Waiting for the first event. If another job is running, '
                        'this one starts when it finishes.</div>', unsafe_allow_html=True)

    if job.done and job.final is not None and not job.announced:
        job.announced = True
        st.rerun(scope="app")  # refresh the deliverables tray once, with the final artifacts


def render() -> None:
    """Draw the job panel; it polls every second (fragment) only while a job is active."""
    job = get_job()
    run_every = POLL_S if job is not None and job.active else None
    st.fragment(job_panel, run_every=run_every)()
