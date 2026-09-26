"""Tests for backend/tools/sandbox.py. Docker tests skip when Docker is not running."""
from __future__ import annotations

import docker
import pytest

from backend.settings import settings
from backend.tools import sandbox
from backend.tools.sandbox import (
    MAX_OUTPUT_CHARS,
    SANDBOX_LABEL_KEY,
    SANDBOX_LABEL_VALUE,
    SandboxError,
    make_tar,
    parse_junit_counts,
    read_tar_file,
    run_in_sandbox,
    sandbox_available,
    truncate_output,
)

needs_docker = pytest.mark.skipif(not sandbox_available(), reason="Docker or sandbox image not available")

_SHORT_TIMEOUT_S = 5


def _our_containers() -> list:
    client = docker.from_env()
    return client.containers.list(all=True, filters={"label": f"{SANDBOX_LABEL_KEY}={SANDBOX_LABEL_VALUE}"})


# ---------------------------------------------------------------- pure helpers
def test_truncate_output_keeps_head_and_tail():
    text = "A" * 30_000 + "B" * 30_000
    cut = truncate_output(text)
    assert len(cut) <= MAX_OUTPUT_CHARS
    assert cut.startswith("A") and cut.endswith("B")
    assert "characters cut" in cut
    assert truncate_output("short") == "short"


def test_make_tar_round_trip():
    data = make_tar({"a.py": "print('hi')\n"})
    assert read_tar_file([data]) == b"print('hi')\n"


def test_parse_junit_counts():
    xml = (
        b'<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="1" failures="2" '
        b'skipped="1" tests="7"></testsuite></testsuites>'
    )
    assert parse_junit_counts(xml) == (3, 3)


def test_junit_path_detection():
    assert sandbox._junit_path(["python", "-m", "pytest", "--junitxml=r.xml"]) == "r.xml"
    assert sandbox._junit_path(["pytest", "--junitxml", "x.xml"]) == "x.xml"
    assert sandbox._junit_path(["python", "a.py", "--junitxml=r.xml"]) is None


def test_docker_down_raises_sandbox_unavailable(monkeypatch):
    def broken():
        raise docker.errors.DockerException("pipe not found")

    monkeypatch.setattr(sandbox.docker, "from_env", broken)
    with pytest.raises(SandboxError) as exc:
        run_in_sandbox({"a.py": "print(1)"}, ["python", "a.py"])
    assert exc.value.code == "SANDBOX_UNAVAILABLE"


@needs_docker
def test_missing_image_raises_sandbox_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "WB_SANDBOX_IMAGE", "wb-sandbox:does-not-exist")
    with pytest.raises(SandboxError) as exc:
        run_in_sandbox({"a.py": "print(1)"}, ["python", "a.py"])
    assert exc.value.code == "SANDBOX_UNAVAILABLE"


# ---------------------------------------------------------------- live docker
@needs_docker
def test_print_two_plus_two():
    result = run_in_sandbox({"a.py": "print(2+2)\n"}, ["python", "a.py"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "4"
    assert result.timed_out is False


@needs_docker
def test_internet_call_fails():
    code = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('https://google.com', timeout=5)\n"
        "    print('REACHED')\n"
        "except Exception as e:\n"
        "    print('BLOCKED', type(e).__name__)\n"
    )
    result = run_in_sandbox({"net.py": code}, ["python", "net.py"])
    assert "REACHED" not in result.stdout
    assert "BLOCKED" in result.stdout


@needs_docker
def test_infinite_loop_is_killed():
    result = run_in_sandbox({"loop.py": "while True:\n    pass\n"}, ["python", "loop.py"], timeout_s=_SHORT_TIMEOUT_S)
    assert result.timed_out is True
    assert result.exit_code != 0
    assert result.duration_ms < (_SHORT_TIMEOUT_S + 10) * 1000


@needs_docker
def test_memory_bomb_is_killed():
    code = "x = []\nwhile True:\n    x.append(bytearray(10_000_000))\n"
    result = run_in_sandbox({"mem.py": code}, ["python", "mem.py"], timeout_s=20)
    assert result.exit_code != 0
    assert result.oom_killed or "MemoryError" in result.stderr
    assert result.timed_out is False


@needs_docker
def test_fork_bomb_is_stopped_by_pids_limit():
    code = (
        "import os, sys\n"
        "count = 0\n"
        "try:\n"
        "    for _ in range(1000):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            import time; time.sleep(30); os._exit(0)\n"
        "        count += 1\n"
        "except OSError as e:\n"
        "    print('LIMITED', count, e.errno)\n"
        "    sys.exit(3)\n"
        "print('UNLIMITED', count)\n"
    )
    result = run_in_sandbox({"fork.py": code}, ["python", "fork.py"], timeout_s=20)
    assert "UNLIMITED" not in result.stdout
    assert "LIMITED" in result.stdout
    forked = int(result.stdout.split()[1])
    assert forked < sandbox.SANDBOX_PIDS_LIMIT


@needs_docker
def test_pytest_counts_from_junit():
    files = {
        "solution.py": "def add(a, b):\n    return a + b\n",
        "test_solution.py": (
            "from solution import add\n"
            "def test_ok1():\n    assert add(1, 2) == 3\n"
            "def test_ok2():\n    assert add(0, 0) == 0\n"
            "def test_bad():\n    assert add(2, 2) == 5\n"
        ),
    }
    command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=report.xml", "test_solution.py"]
    result = run_in_sandbox(files, command)
    assert result.tests_passed == 2
    assert result.tests_failed == 1
    assert result.exit_code == 1


@needs_docker
def test_leftover_containers_are_removed():
    client = docker.from_env()
    client.containers.create(
        settings.WB_SANDBOX_IMAGE, command=["true"], labels={SANDBOX_LABEL_KEY: SANDBOX_LABEL_VALUE}
    )
    assert len(_our_containers()) >= 1
    assert sandbox.remove_leftover_containers(client) >= 1
    assert _our_containers() == []


@needs_docker
def test_zz_no_container_left_behind():
    """Runs last in this file: every run above must have removed its container."""
    assert _our_containers() == []
