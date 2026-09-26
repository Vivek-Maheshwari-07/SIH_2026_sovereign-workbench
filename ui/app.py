"""
Streamlit UI shell (Track B, ticket B3).

Run:  python -m streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501

Sidebar: health lights, contract check, mode, demo scenarios, prewarm, reset.
Main: chat + upload (left), task panels (right; Router/Plan/Timeline/Files/Network are
placeholders filled by B4, B5, B7). All backend calls go through ui.api_client, which never
raises; every error is shown with its ErrorInfo message.
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

from shared.contracts import (  # noqa: E402
    TERMINAL_STATUSES,
    ErrorInfo,
    HealthResponse,
    TaskCreate,
    TaskMode,
    TaskStatus,
)
from ui import api_client  # noqa: E402
from ui.api_client import ApiClient, ApiResult  # noqa: E402
from ui.config import ALLOWED_UPLOAD_TYPES, HEALTH_REFRESH_S, MAX_MESSAGE_CHARS  # noqa: E402
from ui.scenarios import SCENARIOS, DemoScenario  # noqa: E402

PAGE_TITLE = "Sovereign AI Workbench"
FOOTER = "Runs 100% offline on this machine."
MODE_LABELS = {"Guided": TaskMode.GUIDED, "Agent": TaskMode.AGENT}
HEALTH_MAX_AGE_S = 2.0   # reuse one /api/health result within the same page run

CSS = """
<style>
.block-container {padding-top: 1.6rem; padding-bottom: 1rem;}
.wb-header {border-left: 6px solid #1F4E79; padding: 0.2rem 0 0.2rem 0.9rem; margin-bottom: 1rem;}
.wb-header h1 {font-size: 1.7rem; margin: 0; color: #1B2631; letter-spacing: 0.01em;}
.wb-header p {margin: 0.15rem 0 0 0; color: #4A5A6A; font-size: 0.95rem;}
.wb-section {font-size: 0.78rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
             color: #4A5A6A; margin: 0.4rem 0 0.3rem 0;}
.wb-light {display: flex; align-items: baseline; gap: 0.5rem; margin: 0.18rem 0; font-size: 0.9rem;}
.wb-dot {width: 0.7rem; height: 0.7rem; border-radius: 50%; display: inline-block; flex: none;
         position: relative; top: 0.05rem;}
.wb-ok {background: #2E8B57;} .wb-bad {background: #C0392B;}
.wb-reason {color: #5D6D7E; font-size: 0.8rem;}
.wb-status {display: inline-block; padding: 0.1rem 0.55rem; border-radius: 0.25rem; font-weight: 600;
            font-size: 0.85rem; background: #E6EBF1; color: #1B2631;}
.wb-status-succeeded {background: #D4EDDA; color: #1E5631;}
.wb-status-failed, .wb-status-cancelled {background: #F8D7DA; color: #7B1E24;}
.wb-status-running, .wb-status-queued {background: #FFF3CD; color: #6B4E00;}
.wb-footer {margin-top: 2rem; padding-top: 0.6rem; border-top: 1px solid #D5DCE4; color: #5D6D7E;
            font-size: 0.82rem; text-align: center;}
</style>
"""


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


def add_message(role: str, text: str) -> None:
    st.session_state.setdefault("messages", []).append({"role": role, "text": text})


def start_task(message: str, file_ids: list[str], mode: TaskMode, scenario: Optional[DemoScenario]) -> bool:
    """Create a task and remember its id. Returns True on success."""
    request = TaskCreate(message=message, file_ids=file_ids, mode=mode,
                         scenario=scenario.scenario if scenario else None)
    result = client().create_task(request)
    if result.data is None:
        show_error("Could not start the task", result.error)
        return False
    st.session_state["task_id"] = result.data.task_id
    add_message("user", message)
    add_message("assistant", f"Task {result.data.task_id} created ({mode.value} mode). Status: {result.data.status.value}.")
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
    if start_task(scn.prompt, file_ids, current_mode(), scn):
        st.success(f"Started: {scn.title}")


def reset_session() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]


# ---------------------------------------------------------------- sidebar
def light(label: str, ok: bool, reason: str) -> str:
    css = "wb-ok" if ok else "wb-bad"
    return (f'<div class="wb-light"><span class="wb-dot {css}"></span><span><b>{label}</b> '
            f'<span class="wb-reason">{reason}</span></span></div>')


def health_lights(health: HealthResponse) -> str:
    kb_ok = health.kb_chunks > 0
    rows = [
        light("Ollama", health.ollama_ok, "models ready" if health.ollama_ok else "not reachable or model missing"),
        light("Sandbox", health.sandbox_ok, "Docker and image ready" if health.sandbox_ok
              else "Docker not running or image missing"),
        light("Tesseract", health.tesseract_ok, "OCR ready" if health.tesseract_ok else "OCR engine not found"),
        light("Knowledge base", kb_ok, f"{health.kb_chunks} chunks" if kb_ok else "empty, run scripts/ingest.py"),
    ]
    return "".join(rows)


@st.fragment(run_every=HEALTH_REFRESH_S)
def health_panel() -> None:
    st.markdown('<div class="wb-section">System health</div>', unsafe_allow_html=True)
    result = fetch_health()
    if result.data is None:
        if backend_down(result):
            st.error(f"Backend not reachable at {client().base_url}")
        else:
            show_error("Health check failed", result.error)
    else:
        st.markdown(health_lights(result.data), unsafe_allow_html=True)
        st.caption(f"Overall: {result.data.status}  |  checked {time.strftime('%H:%M:%S')}")
    warning = result.version_warning()
    if warning:
        st.error(warning)


def prewarm_panel() -> None:
    if st.button("Prewarm models", key="prewarm", width="stretch",
                 help="Load the models into RAM before the demo (about 20 s)."):
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
        health_panel()
        st.divider()
        st.markdown('<div class="wb-section">Mode</div>', unsafe_allow_html=True)
        st.radio("Mode", list(MODE_LABELS), key="mode_label", horizontal=True, label_visibility="collapsed",
                 help="Guided runs a fixed, tested pipeline for the demo scenarios. Agent lets the model plan.")
        st.markdown('<div class="wb-section">Demo scenarios</div>', unsafe_allow_html=True)
        for scn in SCENARIOS:
            if st.button(scn.title, key=f"scn_{scn.key}", help=scn.description, width="stretch"):
                run_scenario(scn)
        st.divider()
        prewarm_panel()
        if st.button("Reset session", key="reset", width="stretch"):
            reset_session()
            st.rerun()


# ---------------------------------------------------------------- main area
def chat_column() -> None:
    st.markdown('<div class="wb-section">Assistant</div>', unsafe_allow_html=True)
    history = st.container(height=420, border=True)
    with history:
        messages = st.session_state.get("messages", [])
        if not messages:
            st.caption("Pick a demo scenario on the left, or type a request below.")
        for msg in messages:
            with st.chat_message(msg["role"]):
                st.write(msg["text"])

    uploader_key = f"uploads_{st.session_state.get('uploader_n', 0)}"
    files = st.file_uploader("Attach files", type=ALLOWED_UPLOAD_TYPES, accept_multiple_files=True,
                             key=uploader_key)
    prompt = st.chat_input("Describe the task, e.g. 'Summarise this inspection report'",
                           max_chars=MAX_MESSAGE_CHARS, key="chat")
    if prompt:
        send_message(prompt, files or [])


def send_message(prompt: str, files: list) -> None:
    file_ids: list[str] = []
    for f in files:
        file_id = upload(f.name, f.getvalue(), f.type or "application/octet-stream")
        if file_id is None:
            return
        file_ids.append(file_id)
    if start_task(prompt, file_ids, current_mode(), None):
        st.session_state["uploader_n"] = st.session_state.get("uploader_n", 0) + 1  # clears the uploader
        st.rerun()


def placeholder(title: str, ticket: str) -> None:
    with st.container(border=True):
        st.markdown(f'<div class="wb-section">{title}</div>', unsafe_allow_html=True)
        st.caption(f"Filled in {ticket}.")


def task_panel() -> None:
    with st.container(border=True):
        st.markdown('<div class="wb-section">Current task</div>', unsafe_allow_html=True)
        task_id = st.session_state.get("task_id")
        if not task_id:
            st.caption("No task yet.")
            return
        top = st.columns([3, 1])
        top[0].code(task_id, language=None)
        top[1].button("Refresh", key="refresh_task", width="stretch")
        result = client().get_task(task_id)
        if result.data is None:
            show_error("Could not load the task", result.error, warn=True)
            return
        state = result.data
        status = state.status.value
        elapsed = f" | {state.elapsed_s:.0f} s" if state.elapsed_s is not None else ""
        st.markdown(f'<span class="wb-status wb-status-{status}">{status.upper()}</span>'
                    f'<span class="wb-reason"> {state.mode.value} mode{elapsed}</span>', unsafe_allow_html=True)
        if state.status == TaskStatus.FAILED and state.error is not None:
            show_error("Task failed", state.error)
        elif state.status not in TERMINAL_STATUSES:
            st.caption("Running. Press Refresh to update (live timeline comes in B4).")


def results_column() -> None:
    task_panel()
    placeholder("Router", "B4")
    placeholder("Plan", "B4")
    placeholder("Timeline", "B4")
    placeholder("Files", "B5")
    placeholder("Network", "B7")


def header() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(f'<div class="wb-header"><h1>{PAGE_TITLE}</h1>'
                '<p>On-premise agentic AI for inspection reports, engineering code and P&amp;ID drawings</p></div>',
                unsafe_allow_html=True)


def footer() -> None:
    st.markdown(f'<div class="wb-footer">{FOOTER}</div>', unsafe_allow_html=True)


@st.fragment(run_every=HEALTH_REFRESH_S)
def wait_for_backend() -> None:
    """While the backend is down: re-check every few seconds and reload the page once it is up."""
    if not backend_down(fetch_health()):
        st.rerun(scope="app")
    st.caption(f"Checking again every {HEALTH_REFRESH_S:g} s...  (last check {time.strftime('%H:%M:%S')})")


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, layout="wide", initial_sidebar_state="expanded")
    header()
    health = fetch_health()
    if backend_down(health):
        st.error(f"Backend not reachable at {client().base_url}")
        st.markdown("Start it with `uvicorn backend.main:app --host 127.0.0.1 --port 8000`, "
                    "then this page reloads by itself.")
        wait_for_backend()
        footer()
        return
    sidebar()
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        chat_column()
    with right:
        results_column()
    footer()


main()
