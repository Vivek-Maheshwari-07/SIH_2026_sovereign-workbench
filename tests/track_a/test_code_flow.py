"""Tests for backend/flows/code_flow.py. Unit tests mock the model; live tests need Ollama + Docker."""
from __future__ import annotations

import time

import httpx
import pytest

from backend.flows import code_flow
from backend.flows.code_flow import (
    PIPE_THICKNESS_DEMO_REQUEST,
    CodeFlowError,
    ParseError,
    check_tests,
    parse_code_blocks,
    run_code_task,
)
from backend.llm_client import ChatResult
from backend.registry import registry
from backend.settings import settings
from backend.tools.sandbox import SandboxResult, run_in_sandbox, sandbox_available
from shared.contracts import CodeResult, EventType, TaskType

GOOD_SOLUTION = (
    "def pipe_wall_thickness(P, D, S):\n"
    "    return P * D / (2 * S)\n\n"
    "if __name__ == '__main__':\n"
    "    print('t = P*D/(2*S) =', pipe_wall_thickness(10, 200, 138))\n"
)
BUGGY_SOLUTION = GOOD_SOLUTION.replace("(2 * S)", "S")
GOOD_TESTS = (
    "import pytest\n"
    "from solution import pipe_wall_thickness\n\n"
    "def test_known_case():\n"
    "    assert pipe_wall_thickness(10, 200, 138) == pytest.approx(7.246, abs=0.01)\n\n"
    "def test_scales_with_pressure():\n"
    "    assert pipe_wall_thickness(20, 200, 138) == pytest.approx(2 * pipe_wall_thickness(10, 200, 138))\n\n"
    "def test_unit_values():\n"
    "    assert pipe_wall_thickness(1, 2, 1) == pytest.approx(1.0)\n"
)


def _reply(solution: str = GOOD_SOLUTION, tests: str = GOOD_TESTS) -> str:
    return (
        "Here you go.\n\n### solution.py\n```python\n" + solution + "```\n\n"
        "### test_solution.py\n```python\n" + tests + "```\n"
    )


# ---------------------------------------------------------------- parser
def test_parser_reads_named_blocks():
    files = parse_code_blocks(_reply())
    assert files["solution.py"].startswith("def pipe_wall_thickness")
    assert "def test_known_case" in files["test_solution.py"]


def test_parser_reads_file_comment_and_unnamed_blocks():
    text = "```python\n# test_solution.py\ndef test_a():\n    assert 1 == 1\n```\n```py\ndef f():\n    return 1\n```"
    files = parse_code_blocks(text)
    assert "def test_a" in files["test_solution.py"]
    assert "def f" in files["solution.py"]


def test_parser_handles_unclosed_last_block():
    text = "### solution.py\n```python\ndef f():\n    return 1\n```\n### test_solution.py\n```python\ndef test_f():\n    assert True is not False\n"
    files = parse_code_blocks(text)
    assert set(files) == {"solution.py", "test_solution.py"}


def test_parser_missing_block_raises():
    with pytest.raises(ParseError):
        parse_code_blocks("I think you should use t = P*D/(2*S).")


def test_parser_only_solution_block():
    files = parse_code_blocks("### solution.py\n```python\ndef f():\n    return 1\n```")
    assert set(files) == {"solution.py"}


# ---------------------------------------------------------------- anti-cheat
def test_guard_accepts_good_tests():
    assert check_tests(GOOD_TESTS) == []
    raises_test = "import pytest\nfrom solution import f\ndef test_bad():\n    with pytest.raises(ValueError):\n        f(-1)\n"
    assert check_tests(raises_test) == []


def test_guard_catches_fewer_tests():
    problems = check_tests(GOOD_TESTS, previous_count=5)
    assert any("went down from 5 to 3" in p for p in problems)


def test_guard_catches_test_without_assert():
    problems = check_tests("def test_a():\n    x = 1\n")
    assert any("test_a has no assert" in p for p in problems)


@pytest.mark.parametrize(
    "tests",
    [
        "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert 1 == 2\n",
        "import pytest\ndef test_a():\n    pytest.skip('later')\n    assert 1 == 2\n",
        "import pytest\n@pytest.mark.xfail\ndef test_a():\n    assert 1 == 2\n",
        "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    assert 1 == 2\n",
    ],
)
def test_guard_catches_skip_and_xfail(tests):
    assert any("skipping tests is not allowed" in p for p in check_tests(tests))


def test_guard_catches_assert_true():
    assert any("always passes" in p for p in check_tests("def test_a():\n    assert True\n"))


@pytest.mark.parametrize(
    "line",
    ["import os", "import subprocess", "import socket", "import urllib.request", "import requests",
     "from os import path", "from urllib import request"],
)
def test_guard_catches_forbidden_imports(line):
    tests = f"{line}\ndef test_a():\n    assert 1 == 1\n"
    assert any("forbidden" in p for p in check_tests(tests))


def test_guard_catches_syntax_error_and_no_tests():
    assert any("syntax error" in p for p in check_tests("def test_a(:\n"))
    assert any("no test functions" in p for p in check_tests("x = 1\n"))


# ---------------------------------------------------------------- flow with mocks
class _FakeModel:
    """Replaces llm_client.chat with scripted replies."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, model_name, messages, tools=None, images=None, *, purpose="chat"):
        self.prompts.append(messages[-1]["content"])
        return ChatResult(text=self.replies.pop(0), tokens_out=123)


def _fake_sandbox(passing_marker: str = "(2 * S)"):
    """Fake run_in_sandbox: tests pass iff the solution has the correct formula."""
    calls: list[list[str]] = []

    def run(files, command, *, timeout_s=None):
        calls.append(command)
        good = passing_marker in files["solution.py"]
        if "pytest" in " ".join(command):
            return SandboxResult(
                exit_code=0 if good else 1,
                stdout="3 passed" if good else "E   assert 14.49 == 7.246\n1 failed, 2 passed",
                stderr="", timed_out=False, duration_ms=5,
                tests_passed=3 if good else 2, tests_failed=0 if good else 1,
            )
        return SandboxResult(exit_code=0, stdout="t = 7.246 mm\n", stderr="", timed_out=False, duration_ms=5)

    return run, calls


@pytest.fixture
def events():
    collected: list[tuple[EventType, str, dict]] = []
    return collected


def _emitter(events):
    return lambda event_type, title, data: events.append((event_type, title, data))


def test_flow_passes_first_try(monkeypatch, events):
    fake = _FakeModel([_reply()])
    run, calls = _fake_sandbox()
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", run)

    result = run_code_task("pipe thickness", emit=_emitter(events))

    assert isinstance(result, CodeResult)
    assert (result.passed, result.failed, result.attempts) == (3, 0, 1)
    assert "7.246" in result.stdout_tail
    assert calls[0][:3] == ["python", "-m", "pytest"] and calls[1] == ["python", "solution.py"]


def test_flow_emits_events_in_order_with_contract_keys(monkeypatch, events):
    monkeypatch.setattr(code_flow, "chat", _FakeModel([_reply(BUGGY_SOLUTION), _reply()]))
    monkeypatch.setattr(code_flow, "run_in_sandbox", _fake_sandbox()[0])

    run_code_task("pipe thickness", emit=_emitter(events))

    types = [e[0] for e in events]
    assert types == [
        EventType.LLM_CALL, EventType.TOOL_CALL, EventType.TOOL_RESULT, EventType.LOG,       # attempt 1 fails
        EventType.LLM_CALL, EventType.TOOL_CALL, EventType.TOOL_RESULT, EventType.LOG,       # attempt 2 passes
        EventType.TOOL_CALL, EventType.TOOL_RESULT,                                          # print steps
    ]
    keys = {
        EventType.LLM_CALL: {"model_id", "purpose", "duration_ms", "tokens_out"},
        EventType.TOOL_CALL: {"tool", "args"},
        EventType.TOOL_RESULT: {"tool", "ok", "summary", "duration_ms"},
        EventType.LOG: {"level", "text"},
    }
    for event_type, title, data in events:
        assert set(data) == keys[event_type]
        assert isinstance(title, str) and title
    assert events[0][2]["model_id"] == registry.model_for_task(TaskType.CODING).id
    assert events[0][2]["tokens_out"] == 123 and events[4][2]["tokens_out"] == 123
    assert events[2][2]["ok"] is False and events[6][2]["ok"] is True


def test_flow_sends_pytest_output_back(monkeypatch):
    fake = _FakeModel([_reply(BUGGY_SOLUTION), _reply()])
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", _fake_sandbox()[0])
    run_code_task("pipe thickness")
    assert "assert 14.49 == 7.246" in fake.prompts[1]
    assert "Current files" in fake.prompts[1]


def test_retry_loop_stops_after_max_attempts(monkeypatch):
    fake = _FakeModel([_reply(BUGGY_SOLUTION)] * 10)
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", _fake_sandbox()[0])

    with pytest.raises(CodeFlowError) as exc:
        run_code_task("pipe thickness")

    assert len(fake.prompts) == settings.WB_CODE_MAX_ATTEMPTS == 3
    assert exc.value.code == "BAD_MODEL_OUTPUT"
    assert exc.value.result.attempts == 3 and exc.value.result.failed == 1


def test_parse_failure_counts_as_attempt(monkeypatch):
    fake = _FakeModel(["no code here, sorry", _reply()])
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", _fake_sandbox()[0])
    result = run_code_task("pipe thickness")
    assert result.attempts == 2
    assert "No fenced" in fake.prompts[1]


def test_cheating_retry_is_rejected(monkeypatch):
    cheat_tests = "from solution import pipe_wall_thickness\ndef test_a():\n    assert True\n"
    fake = _FakeModel([_reply(BUGGY_SOLUTION), _reply(BUGGY_SOLUTION, cheat_tests), _reply()])
    run, calls = _fake_sandbox()
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", run)

    result = run_code_task("pipe thickness")

    assert result.attempts == 3
    assert "went down from 3 to 1" in fake.prompts[2] and "always passes" in fake.prompts[2]
    assert sum("pytest" in " ".join(c) for c in calls) == 2   # cheat attempt never ran


def test_seed_is_run_first_then_fixed(monkeypatch, events):
    seed = _reply(BUGGY_SOLUTION)
    fake = _FakeModel(["### solution.py\n```python\n" + GOOD_SOLUTION + "```\n"])  # resends only the fix
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", _fake_sandbox()[0])

    result = run_code_task("fix it", emit=_emitter(events), seed_code=seed)

    assert result.attempts == 2 and result.failed == 0
    assert result.tests == GOOD_TESTS
    assert events[0][0] == EventType.TOOL_CALL          # seed runs before any model call
    assert len(fake.prompts) == 1


def test_sandbox_unavailable_becomes_error_code(monkeypatch):
    def down(files, command, *, timeout_s=None):
        raise code_flow.SandboxError("SANDBOX_UNAVAILABLE", "docker down")

    monkeypatch.setattr(code_flow, "chat", _FakeModel([_reply()]))
    monkeypatch.setattr(code_flow, "run_in_sandbox", down)
    with pytest.raises(CodeFlowError) as exc:
        run_code_task("pipe thickness")
    assert exc.value.code == "SANDBOX_UNAVAILABLE"
    assert exc.value.error_info().code == "SANDBOX_UNAVAILABLE"


def test_model_unavailable_becomes_error_code(monkeypatch):
    def down(*args, **kwargs):
        raise code_flow.LLMError("MODEL_UNAVAILABLE", "ollama down")

    monkeypatch.setattr(code_flow, "chat", down)
    with pytest.raises(CodeFlowError) as exc:
        run_code_task("pipe thickness")
    assert exc.value.code == "MODEL_UNAVAILABLE"


def test_unexpected_error_becomes_internal(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(code_flow, "chat", boom)
    with pytest.raises(CodeFlowError) as exc:
        run_code_task("pipe thickness")
    assert exc.value.code == "INTERNAL"


def test_empty_request_is_bad_request():
    with pytest.raises(CodeFlowError) as exc:
        run_code_task("   ")
    assert exc.value.code == "BAD_REQUEST"


# ---------------------------------------------------------------- live (Ollama + Docker)
def _ollama_has_coder() -> bool:
    try:
        tags = httpx.get(f"{settings.OLLAMA_HOST}/api/tags", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return False
    name = registry.model_for_task(TaskType.CODING).ollama_name
    return any(m.get("name") in (name, f"{name}:latest") for m in tags.get("models", []))


needs_docker = pytest.mark.skipif(not sandbox_available(), reason="Docker or sandbox image not available")
needs_live = pytest.mark.skipif(
    not (sandbox_available() and _ollama_has_coder()), reason="Ollama coder model or Docker not available"
)


def _print_attempts(label: str, events, started: float) -> None:
    print(f"\n==== {label} ({time.monotonic() - started:.1f} s total)")
    for event_type, title, data in events:
        if event_type == EventType.LOG:
            print("  " + data["text"])
        elif event_type == EventType.LLM_CALL:
            print(f"  - {title}: {data['duration_ms'] / 1000:.1f} s")


def _verify_known_case(solution: str) -> SandboxResult:
    """Independent of the model's own tests: P=10 MPa, D=200 mm, S=138 MPa -> t = 7.246 mm."""
    check = (
        "from solution import pipe_wall_thickness\n"
        "t = pipe_wall_thickness(10, 200, 138)\n"
        "assert abs(t - 7.246) < 0.01, t\n"
        "print('CHECK_OK', round(t, 4))\n"
    )
    return run_in_sandbox({"solution.py": solution, "check.py": check}, ["python", "check.py"])


@pytest.mark.slow
@needs_live
def test_live_demo_task():
    events: list = []
    started = time.monotonic()
    result = run_code_task(PIPE_THICKNESS_DEMO_REQUEST, emit=_emitter(events))
    _print_attempts("DEMO TASK", events, started)
    print(f"attempts={result.attempts} passed={result.passed} failed={result.failed}")
    print("---- solution.py\n" + result.code)
    print("---- test_solution.py\n" + result.tests)
    print("---- printed steps\n" + result.stdout_tail)

    assert result.failed == 0 and result.passed >= 3
    assert "7.24" in result.stdout_tail or "7.25" in result.stdout_tail
    llm_events = [d for t, _, d in events if t == EventType.LLM_CALL]
    assert all(isinstance(d["tokens_out"], int) and d["tokens_out"] > 0 for d in llm_events)
    print("tokens_out per call:", [d["tokens_out"] for d in llm_events])
    check = _verify_known_case(result.code)
    assert "CHECK_OK" in check.stdout, check.stderr


@pytest.mark.slow
@needs_live
def test_live_buggy_seed_is_fixed():
    seed = _reply(BUGGY_SOLUTION)
    events: list = []
    started = time.monotonic()
    result = run_code_task(PIPE_THICKNESS_DEMO_REQUEST, emit=_emitter(events), seed_code=seed)
    _print_attempts("BUGGY SEED", events, started)
    print(f"attempts={result.attempts} passed={result.passed} failed={result.failed}")
    print("---- fixed solution.py\n" + result.code)

    assert result.failed == 0 and 2 <= result.attempts <= 3
    assert "CHECK_OK" in _verify_known_case(result.code).stdout


@needs_docker
def test_model_style_code_cannot_reach_internet():
    solution = (
        "import urllib.request\n\n"
        "def fetch_rate():\n"
        "    return urllib.request.urlopen('https://google.com', timeout=5).read()\n\n"
        "if __name__ == '__main__':\n"
        "    print(fetch_rate()[:20])\n"
    )
    result = run_in_sandbox({"solution.py": solution}, ["python", "solution.py"])
    assert result.exit_code != 0
    assert "URLError" in result.stderr or "gaierror" in result.stderr or "Errno" in result.stderr


def test_solution_check_requires_main_block(monkeypatch):
    assert code_flow.check_solution(GOOD_SOLUTION) == []
    assert "__main__" in code_flow.check_solution("def f():\n    return 1\n")[0]

    no_main = "def pipe_wall_thickness(P, D, S):\n    return P * D / (2 * S)\n"
    fake = _FakeModel([_reply(no_main), _reply()])
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", _fake_sandbox()[0])
    assert run_code_task("pipe thickness").attempts == 2
    assert "__main__" in fake.prompts[1]


def test_seed_tests_are_locked(monkeypatch, events):
    weakened = "from solution import pipe_wall_thickness\ndef test_a():\n    assert pipe_wall_thickness(1, 2, 1) == 2\n"
    fake = _FakeModel([_reply(GOOD_SOLUTION, weakened)])
    monkeypatch.setattr(code_flow, "chat", fake)
    monkeypatch.setattr(code_flow, "run_in_sandbox", _fake_sandbox()[0])

    result = run_code_task("fix it", emit=_emitter(events), seed_code=_reply(BUGGY_SOLUTION))

    assert result.tests == GOOD_TESTS                    # model's weaker tests were ignored
    assert "will not be changed" in fake.prompts[0]
    first_log = next(d for t, _, d in events if t == EventType.LOG)
    assert "FAILED" not in first_log["text"] or "1 failed" in first_log["text"]
