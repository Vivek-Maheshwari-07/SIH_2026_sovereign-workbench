"""
Typed settings for Track A (backend). Loads every variable from `.env` at the
repo root into one `Settings` object. Read config only through this module —
never hard-code ports, paths or model names elsewhere (AGENTS.md rule 6).

Sovereign guards, checked when the settings load (so the backend refuses to start):
  * OLLAMA_HOST and WB_API_HOST must be loopback (127.x.x.x, ::1 or localhost). Every prompt and
    every uploaded document goes to OLLAMA_HOST, so a mistyped .env must not send them off the
    machine. WB_ALLOW_NON_LOOPBACK=true allows a model server on an air-gapped plant LAN.
  * The telemetry kill switches in .env are exported to os.environ: libraries (Chroma, Hugging
    Face, ...) read the process environment, not our Settings object.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- servers (bind to localhost only: sovereign rule)
    WB_API_HOST: str = "127.0.0.1"
    WB_API_PORT: int = 8000
    WB_MOCK_PORT: int = 8001
    WB_API_URL: str = "http://127.0.0.1:8001"
    WB_UI_PORT: int = 8501
    WB_MOCK: bool = False

    # ---- model server
    OLLAMA_HOST: str = "http://127.0.0.1:11434"
    WB_MODELS_FILE: Path = Path("config/models.yaml")
    WB_NUM_CTX: int = 8192
    WB_TEMPERATURE: float = 0.2
    WB_THINK: bool = False
    WB_LLM_TIMEOUT_S: int = 180
    WB_LLM_RETRIES: int = 1

    # ---- agent limits
    WB_AGENT_MAX_STEPS: int = 8
    WB_AGENT_TIMEOUT_S: int = 600
    WB_CODE_MAX_ATTEMPTS: int = 3

    # ---- paths (relative to repo root)
    WB_WORKSPACE_DIR: Path = Path("workspace")
    WB_KB_DIR: Path = Path("data/kb")
    WB_CHROMA_DIR: Path = Path("data/chroma")
    WB_CACHE_DIR: Path = Path("data/cache")
    WB_LOG_DIR: Path = Path("logs")
    WB_TEMPLATES_DIR: Path = Path("templates")
    WB_MAX_UPLOAD_MB: int = 25

    # ---- OCR / vision
    TESSERACT_CMD: Path = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    WB_OCR_DPI: int = 200
    WB_VISION_MAX_PX: int = 1024
    WB_PID_TILES: int = 4

    # ---- sandbox
    WB_SANDBOX_IMAGE: str = "wb-sandbox:1.0"
    WB_SANDBOX_TIMEOUT_S: int = 30
    WB_SANDBOX_MEM: str = "512m"
    WB_SANDBOX_CPUS: float = 1

    # ---- network monitor
    WB_NET_POLL_S: float = 1

    # ---- kill all library telemetry / online lookups (sovereign proof depends on this)
    ANONYMIZED_TELEMETRY: bool = False
    HF_HUB_OFFLINE: bool = True
    TRANSFORMERS_OFFLINE: bool = True
    HF_HUB_DISABLE_TELEMETRY: bool = True
    STREAMLIT_BROWSER_GATHER_USAGE_STATS: bool = False
    DO_NOT_TRACK: bool = True

    # ---- sovereign guard (not in .env.example: the safe default needs no entry)
    WB_ALLOW_NON_LOOPBACK: bool = False

    @model_validator(mode="after")
    def _loopback_only(self) -> "Settings":
        if self.WB_ALLOW_NON_LOOPBACK:
            return self
        ollama_host = urlsplit(self.OLLAMA_HOST if "://" in self.OLLAMA_HOST else f"http://{self.OLLAMA_HOST}").hostname
        for key, host in (("OLLAMA_HOST", ollama_host), ("WB_API_HOST", self.WB_API_HOST)):
            if not is_loopback_host(host):
                raise ValueError(f"{key} must point to this machine (127.0.0.1 or localhost), got {host!r}. "
                                 "Sovereign rule: prompts and documents never leave the computer. "
                                 "Set WB_ALLOW_NON_LOOPBACK=true only for a model server on an air-gapped LAN.")
        return self


TELEMETRY_KEYS = ("ANONYMIZED_TELEMETRY", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
                  "STREAMLIT_BROWSER_GATHER_USAGE_STATS", "DO_NOT_TRACK")


def is_loopback_host(host: object) -> bool:
    """True for localhost and loopback IPs (127.0.0.0/8, ::1). Anything else, including None, is False."""
    if not isinstance(host, str) or not host:
        return False
    if host.strip("[]").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def telemetry_env(values: Settings) -> dict[str, str]:
    """The kill switches as environment strings ("1"/"0"; "False"/"true" styles where the library expects them)."""
    env: dict[str, str] = {}
    for key in TELEMETRY_KEYS:
        value = bool(getattr(values, key))
        if key == "ANONYMIZED_TELEMETRY":
            env[key] = str(value)                      # Chroma: "False"
        elif key.startswith("STREAMLIT_"):
            env[key] = str(value).lower()              # Streamlit: "false"
        else:
            env[key] = "1" if value else "0"
    return env


def export_telemetry_env(values: Settings) -> None:
    """Put the kill switches into os.environ, without overriding a value the shell already set."""
    for key, value in telemetry_env(values).items():
        os.environ.setdefault(key, value)


settings = Settings()
export_telemetry_env(settings)
