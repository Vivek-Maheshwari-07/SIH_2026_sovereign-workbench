"""
Typed settings for Track B (UI). Loads `.env` at the repo root. Read config only
through this module (AGENTS.md rule 6): no hard-coded ports, URLs or paths in ui/.
"""
from __future__ import annotations

from pathlib import Path

from annotated_types import MaxLen
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.contracts import TaskCreate

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Client timeouts (seconds). No .env key exists for these; values come from the B2 ticket.
DEFAULT_TIMEOUT_S = 10.0
MODEL_TIMEOUT_S = 30.0  # /api/route and /api/kb/search (may load a cold model)
LONG_TIMEOUT_S = 60.0   # uploads and /api/admin/prewarm

HEALTH_REFRESH_S = 5.0  # sidebar health lights refresh
# Upload types the backend accepts: must match ERROR_CODES["UNSUPPORTED_FILE"] in shared/contracts.py.
ALLOWED_UPLOAD_TYPES = ["pdf", "png", "jpg", "jpeg", "txt", "md", "py", "csv", "xlsx", "docx"]
# TaskCreate.message max_length, read from the contract so the chat box can never exceed it.
MAX_MESSAGE_CHARS = next(m.max_length for m in TaskCreate.model_fields["message"].metadata if isinstance(m, MaxLen))


class UISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    WB_API_URL: str = "http://127.0.0.1:8001"
    WB_UI_PORT: int = 8501


def load_settings() -> UISettings:
    """Fresh read of `.env` (plus environment variables, which win)."""
    return UISettings()


settings = load_settings()
