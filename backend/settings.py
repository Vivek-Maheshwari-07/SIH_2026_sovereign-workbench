"""
Typed settings for Track A (backend). Loads every variable from `.env` at the
repo root into one `Settings` object. Read config only through this module —
never hard-code ports, paths or model names elsewhere (AGENTS.md rule 6).
"""
from __future__ import annotations

from pathlib import Path

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


settings = Settings()
