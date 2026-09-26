"""
Streamlit UI (Track B): "control room" layout.

Run:  python -m streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501

Top bar: title + NET-001 / FW-001 faceplate (5 s).  Sidebar: work orders, mode, status lamps
(5 s), prewarm, reset.  Centre: live job panel (B4, 1 s while a job runs) and the chat box.
Right: deliverables tray (B5). All backend calls go through ui.api_client, which never raises.
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

from shared.contracts import ErrorInfo, HealthResponse, TaskCreate, TaskMode  # noqa: E402
from ui import api_client  # noqa: E402
from ui.api_client import ApiClient, ApiResult  # noqa: E402
from ui.components import artifacts, net_faceplate, theme, timeline  # noqa: E402
from ui.components.theme import esc  # noqa: E402
from ui.config import ALLOWED_UPLOAD_TYPES, HEALTH_REFRESH_S, MAX_MESSAGE_CHARS  # noqa: E402
from ui.scenarios import SCENARIOS, DemoScenario  # noqa: E402

PAGE_TITLE = "Sovereign AI Workbench"
TAGLINE = "Approval notes, calculation code and P&ID tag lists, drafted by local AI models."
FOOTER = "Runs 100% offline on this machine."
MODE_LABELS = {"Guided": TaskMode.GUIDED, "Agent": TaskMode.AGENT}
HEALTH_MAX_AGE_S = 2.0   # reuse one /api/health result within the same page run
TASK_QUERY_KEY = "task"  # ?task=<id> lets a page reload pick the job up again (from after=0)


# ---------------------------------------------------------------- helpers
def client() -> ApiClient:
    return api_client.get_client()


def show_error(what: str, error: Optional[ErrorInfo], warn: bool = False) -> None:
    """Friendly message from an ErrorInfo; never a traceback."""
    if error is None:
        return
    text = f"{what}: {error.message}"
    if error.retryable:
        text += " You can try again."
    (st.warning if warn else st.error)(text)


def fetch_health() -> ApiResult[HealthResponse]:
    """One /api/health per page run: the gate and the sidebar share the result."""
    cached = st.session_state.get("health_cache")
    if cached and time.monotonic() - cached[0] < HEALTH_MAX_AGE_S:
        return cached[1]
    result = client().health()
    st.session_state["health_cache"] = (time.monotonic(), result)
    return result


def backend_down(result: ApiResult) -> bool:
    return result.failure in ("unreachable", "timeout")


def current_mode() -> TaskMode:
    return MODE_LABELS[st.session_state.get("mode_label", "Guided")]


def start_task(message: str, file_ids: list[str], mode: TaskMode, scenario: Optional[DemoScenario]) -> bool:
    """Create a task and make it the current job. Returns True on success."""
    request = TaskCreate(message=message, file_ids=file_ids, mode=mode,
                         scenario=scenario.scenario if scenario else None)
    result = client().create_task(request)
    if result.data is None:
        show_error("Could not start the job", result.error)
        return False
    timeline.set_job(timeline.Job(task_id=result.data.task_id, message=message, mode=mode,
                                  work_order=scenario.work_order if scenario else None))
    st.query_params[TASK_QUERY_KEY] = result.data.task_id
    return True


def upload(filename: str, content: bytes, mime_type: str) -> Optional[str]:
    result = client().upload_file(filename, content, mime_type)
    if result.data is None:
        show_error(f"Upload of {filename} failed", result.error)
        return None
    return result.data.file_id


def run_scenario(scn: DemoScenario) -> None:
    file_ids: list[str] = []
    if scn.demo_file is not None:
        try:
            content = scn.demo_file.read_bytes()
        except OSError as exc:
            st.error(f"Demo file {scn.demo_file.name} could not be read: {exc.strerror or exc}")
            return
        file_id = upload(scn.demo_file.name, content, scn.mime_type or "application/octet-stream")
        if file_id is None:
            return
        file_ids.append(file_id)
    start_task(scn.prompt, file_ids, current_mode(), scn)


def reset_session() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.query_params.clear()


def resume_from_url() -> None:
    task_id = st.query_params.get(TASK_QUERY_KEY)
    if task_id and timeline.get_job() is None:
        timeline.set_job(timeline.Job(task_id=task_id))


# ---------------------------------------------------------------- sidebar
def lamp(label: str, ok: bool, reason: str) -> str:
    return (f'<div class="wb-lamp"><i class="{"b-ok" if ok else "b-alarm"}"></i>'
            f'<span>{esc(label)} <span class="why">{esc(reason)}</span></span></div>')


def health_lamps(health: HealthResponse) -> str:
    kb_ok = health.kb_chunks > 0
    return "".join([
        lamp("Ollama", health.ollama_ok, "models ready" if health.ollama_ok else "not reachable or model missing"),
        lamp("Sandbox", health.sandbox_ok, "Docker ready" if health.sandbox_ok else "Docker not running or image missing"),
        lamp("Tesseract", health.tesseract_ok, "OCR ready" if health.tesseract_ok else "OCR engine not found"),
        lamp("Knowledge base", kb_ok, f"{health.kb_chunks} chunks" if kb_ok else "empty, run scripts/ingest.py"),
    ])


@st.fragment(run_every=HEALTH_REFRESH_S)
def health_panel() -> None:
    st.markdown('<div class="wb-h">System status</div>', unsafe_allow_html=True)
    result = fetch_health()
    if result.data is None:
        if backend_down(result):
            st.error(f"Backend not reachable at {client().base_url}")
        else:
            show_error("Health check failed", result.error)
    else:
        st.markdown(health_lamps(result.data), unsafe_allow_html=True)
    warning = result.version_warning()
    if warning:
        st.error(warning)


def work_orders() -> None:
    st.markdown('<div class="wb-h">Work orders</div>', unsafe_allow_html=True)
    for scn in SCENARIOS:
        if st.button(f"**{scn.work_order}** {scn.title}", key=f"wo_{scn.key}", width="stretch"):
            run_scenario(scn)
        st.markdown(f'<div class="wb-wo">{esc(scn.description)}<br>'
                    f'<span class="io">{esc(scn.input_type)} → {esc(scn.output_type)}</span></div>',
                    unsafe_allow_html=True)


def prewarm_panel() -> None:
    if st.button("Prewarm models", key="prewarm", width="stretch",
                 help="Load the models into memory before the demo (about 20 s)."):
        with st.spinner("Loading models..."):
            result = client().prewarm()
        if result.data is None:
            show_error("Prewarm failed", result.error)
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
        if st.button("Reset session", key="reset", width="stretch"):
            reset_session()
            st.rerun()


# ---------------------------------------------------------------- main area
def top_bar() -> None:
    left, right = st.columns([3, 1.4], vertical_alignment="center")
    left.markdown(f'<h1 class="wb-title">{PAGE_TITLE}</h1><p class="wb-sub">{TAGLINE}</p>', unsafe_allow_html=True)
    with right:
        net_faceplate.render()


def chat_box() -> None:
    value = st.chat_input("Describe the job, e.g. 'Summarise the confined space SOP'. Attach files with the clip.",
                          key="chat", max_chars=MAX_MESSAGE_CHARS, accept_file="multiple",
                          file_type=ALLOWED_UPLOAD_TYPES)
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


def footer() -> None:
    st.markdown(f'<div class="wb-footer">{FOOTER}</div>', unsafe_allow_html=True)


@st.fragment(run_every=HEALTH_REFRESH_S)
def wait_for_backend() -> None:
    """While the backend is down: re-check every few seconds and reload the page once it is up."""
    if not backend_down(fetch_health()):
        st.rerun(scope="app")
    st.caption(f"Checking again every {HEALTH_REFRESH_S:g} s (last check {time.strftime('%H:%M:%S')}).")


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, layout="wide", initial_sidebar_state="expanded")
    theme.inject()
    health = fetch_health()
    if backend_down(health):
        st.markdown(f'<h1 class="wb-title">{PAGE_TITLE}</h1>', unsafe_allow_html=True)
        st.error(f"Backend not reachable at {client().base_url}")
        st.markdown("Start it with `uvicorn backend.main:app --host 127.0.0.1 --port 8000`; "
                    "this page reloads by itself when it answers.")
        wait_for_backend()
        footer()
        return
    resume_from_url()
    sidebar()
    top_bar()
    centre, right = st.columns([2.1, 1], gap="large")
    with centre:
        timeline.render()
        chat_box()
    with right:
        job = timeline.get_job()
        st.fragment(tray_panel, run_every=1.0 if job is not None and job.active else None)()
    footer()


main()
