"""
Evaluation pass, group 1: per-task model-call scope (deadline + audit task id), the `loaded`
flag on /api/models, AGENT_TIMEOUT for budget-exhausted model calls, loopback-only settings and
the telemetry environment export. No Ollama needed: a fake client stands in for it.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend import agent, llm_client
from backend.audit import read_audit_records
from backend.settings import Settings, export_telemetry_env, is_loopback_host, telemetry_env

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _FakeClient:
    """Answers chat() with a fixed reply and records nothing else."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(message=SimpleNamespace(content="hello", tool_calls=None), eval_count=3)


# ---------------------------------------------------------------- call scope
def test_call_scope_sets_and_restores_task_and_deadline():
    assert llm_client.current_task_id() is None
    assert llm_client.remaining_s() is None
    with llm_client.call_scope("t_outer000000", time.monotonic() + 100):
        assert llm_client.current_task_id() == "t_outer000000"
        assert 99 < llm_client.remaining_s() <= 100
        with llm_client.call_scope("t_inner000000"):
            assert llm_client.current_task_id() == "t_inner000000"
            assert llm_client.remaining_s() is None
        assert llm_client.current_task_id() == "t_outer000000"
    assert llm_client.current_task_id() is None


def test_capped_timeout_uses_the_time_left_but_never_below_the_minimum():
    assert llm_client.capped_timeout(180) == 180                       # outside a task: unchanged
    with llm_client.call_scope("t_a00000000000", time.monotonic() + 40):
        assert 39 < llm_client.capped_timeout(180) <= 40
        assert llm_client.capped_timeout(10) == 10
    with llm_client.call_scope("t_a00000000000", time.monotonic() - 50):
        assert llm_client.capped_timeout(180) == llm_client.MIN_CALL_TIMEOUT_S


def test_used_up_budget_fails_fast_without_calling_the_model(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(llm_client, "_client", lambda timeout_s=None: fake)
    with llm_client.call_scope("t_late00000000", time.monotonic() - 1):
        with pytest.raises(llm_client.LLMError) as exc:
            llm_client.chat("m", [{"role": "user", "content": "hi"}], purpose="test")
    assert exc.value.code == "MODEL_TIMEOUT"
    assert fake.calls == 0


def test_llm_audit_records_carry_the_task_id(monkeypatch):
    monkeypatch.setattr(llm_client, "_client", lambda timeout_s=None: _FakeClient())
    with llm_client.call_scope("t_audit0000001", time.monotonic() + 60):
        llm_client.chat("m", [{"role": "user", "content": "hi"}], purpose="audit_test")
    records = read_audit_records(task_id="t_audit0000001", limit=10)
    assert [(r.kind, r.detail.get("purpose")) for r in records] == [("llm", "audit_test")]


def test_same_model_treats_missing_tag_as_latest():
    assert llm_client.same_model("bge-m3", "bge-m3:latest")
    assert llm_client.same_model("qwen3.5:4b", "qwen3.5:4b")
    assert not llm_client.same_model("qwen3.5:4b", "qwen3.5:9b")


# ---------------------------------------------------------------- loaded flag
def test_models_with_loaded_marks_models_in_ram(monkeypatch):
    from backend import main

    monkeypatch.setattr(llm_client, "loaded_models", lambda timeout_s=None: ["bge-m3:latest"])
    by_id = {m.id: m.loaded for m in main.models_with_loaded()}
    assert by_id["embed"] is True
    assert by_id["general"] is False


def test_models_with_loaded_survives_ollama_down(monkeypatch):
    from backend import main

    def down(timeout_s=None):
        raise ConnectionError("down")

    monkeypatch.setattr(llm_client, "loaded_models", down)
    assert all(m.loaded is False for m in main.models_with_loaded())


# ---------------------------------------------------------------- stop codes
def test_model_timeout_after_the_budget_becomes_agent_timeout(monkeypatch):
    monkeypatch.setattr(agent.settings, "WB_AGENT_TIMEOUT_S", 600)
    stop = agent.AgentStop("MODEL_TIMEOUT", "slow")
    assert agent.stop_code(stop, 601)[0] == "AGENT_TIMEOUT"
    assert agent.stop_code(stop, 100) == ("MODEL_TIMEOUT", "slow")
    assert agent.stop_code(agent.AgentStop("CANCELLED", "x"), 700)[0] == "CANCELLED"


# ---------------------------------------------------------------- loopback-only settings
@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1", True), ("127.8.8.8", True), ("localhost", True), ("::1", True), ("[::1]", True),
    ("10.0.0.5", False), ("0.0.0.0", False), ("ollama.example.com", False), ("", False), (None, False),
])
def test_is_loopback_host(host, ok):
    assert is_loopback_host(host) is ok


def test_settings_refuse_a_remote_model_server():
    with pytest.raises(ValidationError, match="OLLAMA_HOST must point to this machine"):
        Settings(OLLAMA_HOST="http://10.1.2.3:11434")
    with pytest.raises(ValidationError, match="WB_API_HOST"):
        Settings(WB_API_HOST="0.0.0.0")


def test_settings_allow_a_lan_model_server_only_when_asked():
    assert Settings(OLLAMA_HOST="http://10.1.2.3:11434", WB_ALLOW_NON_LOOPBACK=True).OLLAMA_HOST.startswith("http://10.")
    assert Settings(OLLAMA_HOST="localhost:11434").OLLAMA_HOST == "localhost:11434"


def test_telemetry_env_strings():
    env = telemetry_env(Settings())
    assert env["ANONYMIZED_TELEMETRY"] == "False"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] == "false"


def test_export_telemetry_env_keeps_values_the_shell_set(monkeypatch):
    values = Settings()
    monkeypatch.setenv("DO_NOT_TRACK", "yes")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    export_telemetry_env(values)
    assert os.environ["DO_NOT_TRACK"] == "yes"
    assert os.environ["HF_HUB_OFFLINE"] == "1"


# ---------------------------------------------------------------- pytest collection
def test_root_conftest_keeps_generated_tests_out_of_collection():
    namespace: dict = {}
    exec((_REPO_ROOT / "conftest.py").read_text(encoding="utf-8"), namespace)
    assert "workspace/*" in namespace["collect_ignore_glob"]
    assert "docs/*" in namespace["collect_ignore_glob"]
