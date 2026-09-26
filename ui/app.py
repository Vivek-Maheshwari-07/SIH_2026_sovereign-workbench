"""
Streamlit UI (Track B): "control room" layout.

Run:  python -m streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501

Top bar: title, screen switch (Workbench / Network / Audit), NET-001 / FW-001 faceplate (5 s).
Sidebar: work orders, mode, status lamps with fix hints (5 s), prewarm, reset.
Workbench: request box, live job panel (B4, 1 s while a job runs), deliverables tray (B5). Network (B7, 2 s) and Audit (B7) screens. Resilience (B8): backend-down banner
that re-checks every 3 s and recovers by itself, contract-version banner, friendly errors.
All backend calls go through ui.api_client, which never raises.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # `streamlit run ui/app.py` puts ui/ on sys.path, not the repo root
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402

from shared.contracts import CONTRACT_VERSION, HealthResponse, Scenario, TaskCreate, TaskMode  # noqa: E402
from ui import api_client, messages  # noqa: E402
from ui.api_client import ApiClient, ApiResult  # noqa: E402
from ui.components import artifacts, audit_page, net_faceplate, network_panel, theme, timeline  # noqa: E402
from ui.components.theme import esc  # noqa: E402
from ui.config import (  # noqa: E402
    ALLOWED_UPLOAD_TYPES,
    BACKEND_RETRY_S,
    HEALTH_REFRESH_S,
    MAX_MESSAGE_CHARS,
)
from ui.scenarios import SCENARIOS, DemoScenario, get_scenario  # noqa: E402

PAGE_TITLE = "Sovereign AI Workbench"
TAGLINE = "Approval notes, calculation code and P&ID tag lists, drafted by local AI models."
FOOTER = "Runs 100% offline on this machine."
MODE_LABELS = {"Guided": TaskMode.GUIDED, "Agent": TaskMode.AGENT}
VIEWS = ["Workbench", "Network", "Audit"]
CHAT_HEIGHT_PX = 68      # one line; without it the box can stretch to its maximum height
HEALTH_MAX_AGE_S = 2.0   # reuse one /api/health result within the same page run
TASK_QUERY_KEY = timeline.TASK_QUERY_KEY


# ---------------------------------------------------------------- helpers
def client() -> ApiClient:
    return api_client.get_client()


def fetch_health(max_age: float = HEALTH_MAX_AGE_S) -> ApiResult[HealthResponse]:
    """One /api/health per page run: the gate, the banners and the sidebar share the result."""
    cached = st.session_state.get("health_cache")
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    result = client().health()
    st.session_state["health_cache"] = (time.monotonic(), result)
    return result


def backend_down(result: ApiResult) -> bool:
    return result.failure in ("unreachable", "timeout")


def current_mode() -> TaskMode:
    return MODE_LABELS[st.session_state.get("mode_label", "Guided")]


def current_view() -> str:
    return st.session_state.get("view") or "Workbench"


def start_task(message: str, file_ids: list[str], mode: TaskMode, scenario: Optional[DemoScenario]) -> bool:
    """Create a task and make it the current job. Returns True on success."""
    request = TaskCreate(message=message, file_ids=file_ids, mode=mode,
                         scenario=scenario.scenario if scenario else None)
    result = client().create_task(request)
    if result.data is None:
        messages.show("The job could not be started", result.error, result.failure)
        return False
    current = timeline.get_job()
    if current is not None and current.final is not None:
        timeline.close_job()  # keep the finished job as "last finished job"
    timeline.set_job(timeline.Job(task_id=result.data.task_id, message=message, mode=mode,
                                  work_order=scenario.work_order if scenario else None,
                                  scenario_key=scenario.key if scenario else None,
                                  had_files=bool(file_ids) and scenario is None))
    st.query_params[TASK_QUERY_KEY] = result.data.task_id
    return True


def upload(filename: str, content: bytes, mime_type: str) -> Optional[str]:
    result = client().upload_file(filename, content, mime_type)
    if result.data is None:
        messages.show(f"{filename} could not be uploaded", result.error, result.failure)
        return None
    return result.data.file_id


def run_scenario(scn: DemoScenario, mode: Optional[TaskMode] = None) -> bool:
    file_ids: list[str] = []
    if scn.demo_file is not None:
        try:
            content = scn.demo_file.read_bytes()
        except OSError as exc:
            st.error(f"The demo file {scn.demo_file.name} could not be read ({exc.strerror or exc}). "
                     "Check that demo/inputs/ is complete.")
            return False
        file_id = upload(scn.demo_file.name, content, scn.mime_type or "application/octet-stream")
        if file_id is None:
            return False
        file_ids.append(file_id)
    return start_task(scn.prompt, file_ids, mode or current_mode(), scn)


def rerun_job(job: timeline.Job) -> bool:
    """'Run it again' after a backend restart: repeat the work order, or re-send the chat request."""
    mode = job.mode or current_mode()
    if job.scenario_key is not None:
        return run_scenario(get_scenario(Scenario(job.scenario_key)), mode)
    return start_task(job.message, [], mode, None)


def reset_session() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.query_params.clear()


def resume_from_url() -> None:
    task_id = st.query_params.get(TASK_QUERY_KEY)
    if task_id and timeline.get_job() is None:
        timeline.set_job(timeline.Job(task_id=task_id))


# ---------------------------------------------------------------- banners (B8)
def down_banner_html(url: str, checks: int) -> str:
    return (f'<div class="wb-banner"><div class="h">Backend not reachable at {esc(url)}</div>'
            f'<p>Start it with <code>{esc(messages.BACKEND_START)}</code>. This page checks again every '
            f'{BACKEND_RETRY_S:g} s and comes back by itself; nothing you did is lost.</p>'
            f'<p class="wb-meta">Checks so far: {checks}. Last check {time.strftime("%H:%M:%S")}.</p></div>')


def version_banner(result: ApiResult) -> None:
    """Compares the X-Contract-Version header (and /api/health's contract_version) with ours."""
    server = result.data.contract_version if result.data is not None else result.contract_version
    if result.version_warning() is None and server in (None, CONTRACT_VERSION):
        return
    server = result.contract_version if result.contract_version != CONTRACT_VERSION else server
    st.markdown(f'<div class="wb-banner"><div class="h">Contract version mismatch</div>'
                f'<p>The backend speaks contract {esc(server or "unknown")}, this screen expects '
                f'{esc(CONTRACT_VERSION)}. Update the older side (git pull) and restart it; until then some '
                'readings may be wrong.</p></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- sidebar
def lamp(label: str, ok: bool, reason: str, fix: str = "") -> str:
    fix_html = f'<span class="fix">{esc(fix)}</span>' if fix and not ok else ""
    return (f'<div class="wb-lamp"><i class="{"b-ok" if ok else "b-alarm"}"></i>'
            f'<span>{esc(label)} <span class="why">{esc(reason)}</span>{fix_html}</span></div>')


def health_lamps(health: HealthResponse) -> str:
    kb_ok = health.kb_chunks > 0
    fix = messages.LAMP_FIX
    return "".join([
        lamp("Ollama", health.ollama_ok, "models ready" if health.ollama_ok else "not reachable", fix["ollama"]),
        lamp("Sandbox", health.sandbox_ok, "Docker ready" if health.sandbox_ok else "Docker not running",
             fix["sandbox"]),
        lamp("Tesseract", health.tesseract_ok, "OCR ready" if health.tesseract_ok else "OCR engine not found",
             fix["tesseract"]),
        lamp("Knowledge base", kb_ok, f"{health.kb_chunks} chunks" if kb_ok else "empty", fix["kb"]),
    ])


@st.fragment(run_every=HEALTH_REFRESH_S)
def health_panel() -> None:
    st.markdown('<div class="wb-h">System status</div>', unsafe_allow_html=True)
    result = fetch_health()
    if backend_down(result):
        st.rerun(scope="app")  # switch the whole page to the backend-down banner
    if result.data is None:
        messages.show("The health check failed", result.error, result.failure)
    else:
        st.markdown(health_lamps(result.data), unsafe_allow_html=True)


def work_orders() -> None:
    st.markdown('<div class="wb-h">Work orders</div>', unsafe_allow_html=True)
    for scn in SCENARIOS:
        if st.button(f"**{scn.work_order}** {scn.title}", key=f"wo_{scn.key}", width="stretch"):
            if run_scenario(scn):
                st.session_state["view"] = "Workbench"
        st.markdown(f'<div class="wb-wo">{esc(scn.description)}<br>'
                    f'<span class="io">{esc(scn.input_type)} → {esc(scn.output_type)}</span></div>',
                    unsafe_allow_html=True)


def prewarm_panel() -> None:
    if st.button("Prewarm models", key="prewarm", width="stretch",
                 help="Load the models into memory before the demo (about 20 s)."):
        with st.spinner("Loading models..."):
            result = client().prewarm()
        if result.data is None:
            messages.show("Prewarm did not finish", result.error, result.failure)
        else:
            st.session_state["prewarm_result"] = result.data
    data = st.session_state.get("prewarm_result")
    if data is not None:
        lines = [f"- ready: {item}" for item in data.warmed] + [f"- failed: {item}" for item in data.failed]
        (st.warning if data.failed else st.success)(
            f"Prewarm finished in {data.duration_ms / 1000:.1f} s\n\n" + "\n".join(lines))


def sidebar() -> None:
    with st.sidebar:
        work_orders()
        st.radio("Mode", list(MODE_LABELS), key="mode_label", horizontal=True,
                 help="Guided runs the fixed, tested pipeline for a work order. Agent lets the model plan the steps.")
        st.divider()
        health_panel()
        st.divider()
        prewarm_panel()
        if st.button("Reset session", key="reset", width="stretch",
                     help="Forget the current job, filters and cached files in this browser tab."):
            reset_session()
            st.rerun()
        footer()


# ---------------------------------------------------------------- main area
def top_bar() -> None:
    left, right = st.columns([3, 1.4], vertical_alignment="top")
    with left:
        st.markdown(f'<h1 class="wb-title">{PAGE_TITLE}</h1><p class="wb-sub">{TAGLINE}</p>',
                    unsafe_allow_html=True)
        st.segmented_control("Screen", VIEWS, default="Workbench", key="view", label_visibility="collapsed")
    with right:
        net_faceplate.render()


def chat_box() -> None:
    value = st.chat_input("Describe the job, e.g. 'Summarise the confined space SOP'. Attach files with the +.",
                          key="chat", max_chars=MAX_MESSAGE_CHARS, accept_file="multiple",
                          file_type=ALLOWED_UPLOAD_TYPES, height=CHAT_HEIGHT_PX)
    if not value:
        return
    text = value if isinstance(value, str) else (value.text or "")
    files = [] if isinstance(value, str) else list(value.files or [])
    if not text.strip():
        st.warning("Write what the job should do; files alone are not enough.")
        return
    send_message(text.strip(), files)


def send_message(prompt: str, files: list) -> None:
    file_ids: list[str] = []
    for f in files:
        file_id = upload(f.name, f.getvalue(), f.type or "application/octet-stream")
        if file_id is None:
            return
        file_ids.append(file_id)
    if start_task(prompt, file_ids, current_mode(), None):
        st.rerun()


def tray_panel() -> None:
    job = timeline.get_job()
    artifacts.tray(timeline.artifacts_of(job) if job else [])


def workbench() -> None:
    centre, right = st.columns([2.1, 1], gap="large")
    with centre:
        # The request box sits on top of the job panel: always in reach during a run. (Pinned to the
        # page bottom instead, Streamlit keeps scrolling to the bottom and hides the top bar at 1366x768.)
        chat_box()
        timeline.render(rerun_job)
    with right:
        job = timeline.get_job()
        st.fragment(tray_panel, run_every=1.0 if job is not None and job.active else None)()


def footer() -> None:
    st.markdown(f'<div class="wb-footer">{FOOTER}</div>', unsafe_allow_html=True)


@st.fragment(run_every=BACKEND_RETRY_S)
def wait_for_backend() -> None:
    """While the backend is down: re-check every 3 s and reload the whole page once it answers."""
    checks = st.session_state.get("down_checks", 0) + 1
    st.session_state["down_checks"] = checks
    if not backend_down(fetch_health(max_age=0)):
        st.session_state.pop("down_checks", None)
        st.rerun(scope="app")
    st.markdown(down_banner_html(client().base_url, checks), unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, layout="wide", initial_sidebar_state="expanded")
    theme.inject()
    health = fetch_health()
    if backend_down(health):
        st.markdown(f'<h1 class="wb-title">{PAGE_TITLE}</h1><p class="wb-sub">{TAGLINE}</p>',
                    unsafe_allow_html=True)
        wait_for_backend()
        footer()
        return
    resume_from_url()
    sidebar()
    top_bar()
    version_banner(health)
    view = current_view()
    if view == "Network":
        network_panel.render()
    elif view == "Audit":
        job = timeline.get_job()
        audit_page.render(job.task_id if job else None)
    else:
        workbench()


main()
