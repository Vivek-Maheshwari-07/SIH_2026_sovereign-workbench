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
from typing import Callable, Literal, Optional

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
from ui import api_client, messages
from ui.components import plan_view, router_badge
from ui.scenarios import get_scenario
from ui.components.theme import ACTIVE, ALARM, DONE, FACE, INK_2, LINE, esc, fmt_seconds

JOB_KEY = "job"
HISTORY_KEY = "job_history"
LOOKUP_KEY = "last_job_lookup"
TASK_QUERY_KEY = "task"        # ?task=<id> lets a page reload pick the job up again (from after=0)
AUDIT_LOOKBACK = 1000          # audit records searched for the last finished job (once per session)
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
    scenario_key: Optional[str] = None         # demo scenario, so "Run it again" can repeat it
    had_files: bool = False                    # chat job with attachments (cannot be re-sent without them)
    lost: bool = False                         # TASK_NOT_FOUND: the backend restarted and forgot the job

    @property
    def active(self) -> bool:
        return not self.lost and (not self.done or (self.final is None and self.final_error is None))

    @property
    def can_rerun(self) -> bool:
        return self.scenario_key is not None or (bool(self.message) and not self.had_files)


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
        if result.error.code == "TASK_NOT_FOUND":  # the backend restarted: the job is gone, stop polling
            job.done, job.lost, job.poll_error = True, True, None
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
    if result.error is not None and result.error.code == "TASK_NOT_FOUND":
        job.lost = True
        return
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
    "lost": ("Lost", "c-warn"),
}


def live_state(job: Job) -> str:
    if job.final is not None:
        return job.final.status.value
    if job.cancel_sent:
        return "stopping"
    return "running" if job.events else "queued"


def header(job: Job, rerun: Optional[Callable[[Job], bool]] = None) -> None:
    label, css = STATE_STYLE["lost" if job.lost else live_state(job)]
    what = " · ".join(x for x in (job.work_order, job.mode.value.capitalize() if job.mode else None) if x)
    left, mid, right = st.columns([5, 1.3, 1.7], vertical_alignment="center")
    left.markdown(
        f'<div class="wb-job"><span class="id">Job {esc(job.task_id)}</span>'
        f'<span class="wb-state {css}">{label}</span><span class="wb-meta">{esc(what)}</span></div>',
        unsafe_allow_html=True)
    if not job.lost:
        mid.markdown(f'<div class="wb-clock" title="Elapsed time">{fmt_seconds(elapsed_s(job))}</div>',
                     unsafe_allow_html=True)
    if job.active and not job.done:
        right.button("Cancel job", key="cancel_job", disabled=job.cancel_sent, width="stretch",
                     on_click=cancel_job, args=(job,))
    elif right.button("Close job", key="close_job", width="stretch",
                      help="Put this job away. It stays on the start screen as the last finished job."):
        close_job()
        st.rerun(scope="app")
    message = job.message or (job.final.message if job.final else "")
    st.markdown(f'<div class="wb-job"><span class="msg" title="{esc(message)}">'
                f'{esc(message) or "Resumed after page reload"}</span></div>', unsafe_allow_html=True)
    if job.cancel_error is not None:
        messages.show("The job could not be cancelled", job.cancel_error)


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


LOST_TEXT = "The backend restarted, this job was lost. Run it again."


def lost_box(job: Job, rerun: Optional[Callable[[Job], bool]]) -> None:
    st.markdown(f'<div class="wb-banner warn"><div class="h">{LOST_TEXT}</div>'
                "<p>Jobs live in the backend's memory, so a restart forgets them. "
                "Files you already downloaded are safe.</p></div>", unsafe_allow_html=True)
    if rerun is not None and job.can_rerun:
        if st.button("Run it again", key="rerun_job", type="primary"):
            if rerun(job):
                st.rerun(scope="app")
    elif job.had_files:
        st.caption("This job had attached files. Attach them again in the request box and send it.")
    else:
        st.caption("Pick the work order again on the left, or describe the job in the request box.")


def result_box(job: Job) -> None:
    if job.final_error is not None:
        messages.show("The job finished, but its result could not be loaded", job.final_error)
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
        what = final.error.message if final.error else "No error details were reported."
        advice = messages.ADVICE.get(final.error.code, "") if final.error else ""
        code = esc(final.error.code) if final.error else "FAILED"
        st.markdown(f'<div class="wb-result failed"><div class="wb-h c-alarm">Job failed ({code})</div>'
                    f'<div class="wb-meta">{total}</div><div class="ans">{multiline(what)}</div>'
                    f'<div class="ans">What to do: {esc(advice or "Run the job again.")}</div></div>',
                    unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="wb-result cancelled"><div class="wb-h">Job cancelled</div>'
                    f'<div class="wb-meta">{total}</div></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- history + idle screen
@dataclass
class JobSummary:
    task_id: str
    status: str
    work_order: Optional[str] = None
    message: str = ""
    elapsed_s: Optional[float] = None
    files: list[str] = field(default_factory=list)
    answer: str = ""
    details_lost: bool = False


def summary_of(state: TaskState, work_order: Optional[str] = None) -> JobSummary:
    if work_order is None and state.scenario is not None:
        work_order = get_scenario(state.scenario).work_order
    answer = state.final_answer or (state.error.message if state.error else "")
    return JobSummary(task_id=state.task_id, status=state.status.value, work_order=work_order,
                      message=state.message, elapsed_s=state.elapsed_s,
                      files=[a.filename for a in state.artifacts], answer=_first_line(answer, 200))


def close_job() -> None:
    job = get_job()
    if job is not None and job.final is not None:
        st.session_state.setdefault(HISTORY_KEY, []).append(summary_of(job.final, job.work_order))
    set_job(None)
    if TASK_QUERY_KEY in st.query_params:
        del st.query_params[TASK_QUERY_KEY]


def lookup_last_finished() -> Optional[JobSummary]:
    """Newest task_finished record in the audit log, with its TaskState if the backend still has it."""
    client = api_client.get_client()
    result = client.audit(limit=AUDIT_LOOKBACK)
    if result.data is None:
        return None
    rec = next((r for r in result.data if r.kind == "system" and r.name == "task_finished" and r.task_id), None)
    if rec is None:
        return None
    state = client.get_task(rec.task_id)
    if state.data is not None:
        return summary_of(state.data)
    return JobSummary(task_id=rec.task_id, status=str(rec.detail.get("status", "finished")), details_lost=True)


def last_job() -> Optional[JobSummary]:
    history = st.session_state.get(HISTORY_KEY) or []
    if history:
        return history[-1]
    if LOOKUP_KEY not in st.session_state:
        st.session_state[LOOKUP_KEY] = lookup_last_finished()
    return st.session_state[LOOKUP_KEY]


HOW_STEPS = [
    ("Route", "The router picks the right local model for the job: document, code or drawing."),
    ("Plan and work", "The agent reads files, searches the SOPs, writes code and tests it in the offline sandbox."),
    ("Deliver", "You get a Word note, an Excel list or tested code to check and download."),
]


def how_html() -> str:
    steps = "".join(f'<div class="s"><div class="n">{i}</div><div><div class="t">{esc(t)}</div>'
                    f'<div class="d">{esc(d)}</div></div></div>' for i, (t, d) in enumerate(HOW_STEPS, start=1))
    return f'<div class="wb-h" style="margin-top:14px">How a job runs</div><div class="wb-how">{steps}</div>'


def last_job_html(summary: JobSummary) -> str:
    label, css = STATE_STYLE.get(summary.status, (summary.status.capitalize(), "c-dim"))
    wo = f'<span class="wb-meta">{esc(summary.work_order)}</span>' if summary.work_order else ""
    took = f'<span class="wb-num wb-meta">{fmt_seconds(summary.elapsed_s)}</span>' if summary.elapsed_s else ""
    files = f'<div class="files">Files: {esc(", ".join(summary.files))}</div>' if summary.files else ""
    body = f'<div class="ans">{esc(summary.answer)}</div>' if summary.answer else ""
    if summary.details_lost:
        body = '<div class="files">Details are gone: the backend restarted after this job.</div>'
    msg = f'<div class="files">{esc(_first_line(summary.message, 140))}</div>' if summary.message else ""
    return (f'<div class="wb-h" style="margin-top:16px">Last finished job</div>'
            f'<div class="wb-last {esc(summary.status)}"><div class="row"><span class="wb-num">'
            f'<b>{esc(summary.task_id)}</b></span><span class="wb-state {css}">{label}</span>{wo}{took}</div>'
            f'{msg}{body}{files}</div>')


def idle_panel() -> None:
    st.markdown(process_line_html(build_stages([], [], None)), unsafe_allow_html=True)
    st.markdown('<div class="wb-empty">No job running. Pick a work order on the left or describe the job above.</div>',
                unsafe_allow_html=True)
    st.markdown(how_html(), unsafe_allow_html=True)
    summary = last_job()
    if summary is not None:
        st.markdown(last_job_html(summary), unsafe_allow_html=True)
        if not summary.details_lost and st.button("Open this job", key="open_last"):
            set_job(Job(task_id=summary.task_id, message=summary.message, work_order=summary.work_order))
            st.query_params[TASK_QUERY_KEY] = summary.task_id
            st.rerun(scope="app")


def job_panel(rerun: Optional[Callable[[Job], bool]] = None) -> None:
    job = get_job()
    if job is None:
        idle_panel()
        return
    if not job.done:
        poll_once(job)
    if job.done and not job.lost and job.final is None and job.final_error is None:
        fetch_final(job)

    header(job, rerun)
    if job.lost:
        lost_box(job, rerun)
        return
    if job.poll_error is not None:
        st.warning(messages.friendly("Lost contact with the backend", job.poll_error)
                   + " The job panel keeps trying every second.")
    plan = plan_of(job)
    st.markdown(process_line_html(build_stages(job.events, plan, job.final, job.done)), unsafe_allow_html=True)
    left, right = st.columns(2, gap="medium")
    with left:
        router_badge.render(route_of(job))
    with right:
        plan_view.render(plan, job.events, job.final)
    result_box(job)
    st.markdown('<div class="wb-h" style="margin-top:8px">Event journal</div>', unsafe_allow_html=True)
    with st.container(height=210, border=False, key="journal", autoscroll=False):
        if job.events:
            st.markdown(journal_html(job.events), unsafe_allow_html=True)
        else:
            st.markdown('<div class="wb-empty">Waiting for the first event. If another job is running, '
                        'this one starts when it finishes.</div>', unsafe_allow_html=True)

    if job.done and job.final is not None and not job.announced:
        job.announced = True
        st.rerun(scope="app")  # refresh the deliverables tray once, with the final artifacts


def render(rerun: Optional[Callable[[Job], bool]] = None) -> None:
    """Draw the job panel; it polls every second (fragment) only while a job is active."""
    job = get_job()
    run_every = POLL_S if job is not None and job.active else None
    st.fragment(job_panel, run_every=run_every)(rerun)
