"""
B8: every API error in the interface's voice: what happened + what to do. Never a traceback.
The texts use the contract's ErrorInfo (code + message) and the client's failure kind.
"""
from __future__ import annotations

from typing import Optional

import streamlit as st

from shared.contracts import ErrorInfo
from ui.config import ALLOWED_UPLOAD_TYPES, settings

BACKEND_START = "uvicorn backend.main:app --host 127.0.0.1 --port 8000"
TYPES_TEXT = ", ".join(t.upper() for t in ALLOWED_UPLOAD_TYPES)

# code -> what to do (the "what happened" part is the server's own message)
ADVICE = {
    "BAD_REQUEST": "Check the request and try again.",
    "FILE_NOT_FOUND": "The file is no longer on the backend. Attach it again.",
    "FILE_TOO_LARGE": "Pick a smaller file (the limit is set by WB_MAX_UPLOAD_MB).",
    "UNSUPPORTED_FILE": f"Use one of: {TYPES_TEXT}.",
    "TASK_NOT_FOUND": "The backend restarted and forgot this job. Run it again.",
    "TASK_BUSY": "Another job is running; this one starts when it finishes.",
    "MODEL_UNAVAILABLE": "Start Ollama and check the models are pulled (python scripts/doctor.py).",
    "MODEL_TIMEOUT": "The model is slow on this CPU. Press Prewarm models, then try again.",
    "BAD_MODEL_OUTPUT": "The model gave an unusable answer. Run the job again, or use Guided mode.",
    "SANDBOX_UNAVAILABLE": "Open Docker Desktop and wait until it says it is running.",
    "SANDBOX_TIMEOUT": "The code ran too long in the sandbox. Run the job again.",
    "AGENT_STEP_LIMIT": "The agent needed too many steps. Try Guided mode for this work order.",
    "AGENT_TIMEOUT": "The job took too long. Press Prewarm models, then run it again.",
    "CANCELLED": "The job was cancelled. Start it again when ready.",
    "INTERNAL": "Something failed inside the backend; details are in logs/backend.log.",
}


def friendly(what: str, error: Optional[ErrorInfo], failure: Optional[str] = None) -> str:
    """One sentence for what happened and one for what to do."""
    if error is None:
        return what
    if failure == "unreachable":
        return f"{what}: the backend is not answering at {settings.WB_API_URL}. Start it with `{BACKEND_START}`."
    if failure == "timeout":
        return f"{what}: the backend did not answer in time (a model may be loading). Wait a moment and try again."
    if failure == "bad_response":
        return (f"{what}: the backend sent an answer this screen cannot read. "
                "Check that the UI and backend are on the same contract version.")
    advice = ADVICE.get(error.code, "Try again; if it keeps failing, check logs/backend.log.")
    message = error.message.rstrip(".")
    return f"{what}: {message}. {advice}"


def show(what: str, error: Optional[ErrorInfo], failure: Optional[str] = None, warn: bool = False) -> None:
    if error is None:
        return
    (st.warning if warn else st.error)(friendly(what, error, failure))


# ---------------------------------------------------------------- health lamps
LAMP_FIX = {
    "ollama": "Start Ollama",
    "sandbox": "Open Docker Desktop",
    "tesseract": "Install Tesseract OCR (see README)",
    "kb": "Run python scripts/ingest.py",
}
