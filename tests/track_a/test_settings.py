from pathlib import Path

from backend.settings import settings


def test_server_settings_types_and_values():
    assert settings.WB_API_HOST == "127.0.0.1"
    assert isinstance(settings.WB_API_PORT, int)
    assert isinstance(settings.WB_API_URL, str)
    assert settings.WB_API_URL.startswith("http://127.0.0.1")


def test_model_server_settings():
    assert settings.OLLAMA_HOST.startswith("http://127.0.0.1")
    assert isinstance(settings.WB_NUM_CTX, int)
    assert isinstance(settings.WB_TEMPERATURE, float)
    assert isinstance(settings.WB_THINK, bool)


def test_path_settings_are_path_objects():
    assert isinstance(settings.WB_CHROMA_DIR, Path)
    assert isinstance(settings.WB_WORKSPACE_DIR, Path)
    assert isinstance(settings.WB_MODELS_FILE, Path)


def test_sandbox_settings():
    assert isinstance(settings.WB_SANDBOX_TIMEOUT_S, int)
    assert isinstance(settings.WB_SANDBOX_MEM, str)
    assert settings.WB_SANDBOX_IMAGE == "wb-sandbox:1.0"


def test_telemetry_settings_default_offline():
    assert settings.ANONYMIZED_TELEMETRY is False
    assert settings.HF_HUB_OFFLINE is True
