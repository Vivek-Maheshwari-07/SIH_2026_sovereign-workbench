"""
Docker sandbox for running model-written code (ticket A5).

Every run gets a fresh, locked-down container: no network, capped memory
(no extra swap), one CPU, a pids limit against fork bombs, all Linux
capabilities dropped, non-root user. Files go in and come out through
in-memory tar archives (put_archive / get_archive) — no host folder is ever
bind-mounted. The container is always removed, even on errors or timeouts.
"""
from __future__ import annotations

import io
import tarfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Optional

import docker
import requests
from docker.errors import DockerException, ImageNotFound, NotFound

from backend.audit import write_audit_record
from backend.settings import settings
from shared.contracts import ERROR_CODES

# ---- named constants (no .env key exists for these)
SANDBOX_LABEL_KEY = "wb-sandbox"
SANDBOX_LABEL_VALUE = "1"
SANDBOX_USER = "runner"
SANDBOX_WORKDIR = "/work"
SANDBOX_PIDS_LIMIT = 128
MAX_OUTPUT_CHARS = 20_000
# Extra seconds allowed for container start/stop around the code's own timeout.
_KILL_GRACE_S = 5
# Docker's exit code when a process is killed with SIGKILL (OOM killer, `docker kill`).
_EXIT_SIGKILL = 137

_cleanup_lock = threading.Lock()
_cleanup_done = False


class SandboxError(Exception):
    """Raised when the sandbox cannot run at all. `code` is a key from ERROR_CODES."""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message or ERROR_CODES[code])
        self.code = code


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    oom_killed: bool = False
    tests_passed: Optional[int] = None   # only set when pytest wrote a junit xml report
    tests_failed: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and the tail of `text` so errors at either end survive."""
    if len(text) <= limit:
        return text
    marker = f"\n... [{len(text) - limit} characters cut] ...\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    return text[:head] + marker + text[len(text) - (keep - head):]


def make_tar(files: dict[str, str]) -> bytes:
    """Pack {relative name: text} into an in-memory tar owned by the sandbox user."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.uid = info.gid = 1000
            info.uname = info.gname = SANDBOX_USER
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def read_tar_file(chunks: Any) -> Optional[bytes]:
    """Return the bytes of the first regular file in a tar stream from get_archive."""
    raw = b"".join(chunks)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for member in tar.getmembers():
            if member.isfile():
                extracted = tar.extractfile(member)
                return extracted.read() if extracted else None
    return None


def parse_junit_counts(xml_bytes: bytes) -> tuple[int, int]:
    """Return (passed, failed) from a pytest --junitxml report. Errors count as failed."""
    root = ET.fromstring(xml_bytes)
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
    failed = failures + errors
    return max(0, tests - failed - skipped), failed


def _junit_path(command: list[str]) -> Optional[str]:
    """Find the --junitxml target in a pytest command, if any."""
    if not any("pytest" in part for part in command):
        return None
    for i, part in enumerate(command):
        if part.startswith("--junitxml="):
            return part.split("=", 1)[1]
        if part == "--junitxml" and i + 1 < len(command):
            return command[i + 1]
    return None


def _docker_client() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
    except (DockerException, requests.RequestException) as exc:
        raise SandboxError("SANDBOX_UNAVAILABLE", f"Docker is not running or not reachable: {exc}") from exc
    return client


def remove_leftover_containers(client: docker.DockerClient) -> int:
    """Remove every container with our label (left over from a crash). Returns how many."""
    removed = 0
    for container in client.containers.list(all=True, filters={"label": f"{SANDBOX_LABEL_KEY}={SANDBOX_LABEL_VALUE}"}):
        try:
            container.remove(force=True)
            removed += 1
        except NotFound:
            pass
    return removed


def _cleanup_once(client: docker.DockerClient) -> None:
    global _cleanup_done
    with _cleanup_lock:
        if not _cleanup_done:
            remove_leftover_containers(client)
            _cleanup_done = True


def _nano_cpus() -> int:
    return int(settings.WB_SANDBOX_CPUS * 1_000_000_000)


def _create_container(client: docker.DockerClient, command: list[str]):
    try:
        client.images.get(settings.WB_SANDBOX_IMAGE)
    except ImageNotFound as exc:
        raise SandboxError(
            "SANDBOX_UNAVAILABLE",
            f"Sandbox image {settings.WB_SANDBOX_IMAGE} is missing; build it from sandbox/Dockerfile",
        ) from exc
    return client.containers.create(
        settings.WB_SANDBOX_IMAGE,
        command=command,
        network_disabled=True,
        network_mode="none",
        mem_limit=settings.WB_SANDBOX_MEM,
        memswap_limit=settings.WB_SANDBOX_MEM,
        nano_cpus=_nano_cpus(),
        pids_limit=SANDBOX_PIDS_LIMIT,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        user=SANDBOX_USER,
        working_dir=SANDBOX_WORKDIR,
        labels={SANDBOX_LABEL_KEY: SANDBOX_LABEL_VALUE},
        environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
        detach=True,
    )


def _wait(container, timeout_s: int) -> tuple[Optional[int], bool]:
    """Wait for the container to exit. Returns (exit_code, timed_out)."""
    try:
        status = container.wait(timeout=timeout_s)
        return int(status.get("StatusCode", -1)), False
    except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
        pass
    try:
        container.kill()
    except (DockerException, requests.RequestException):
        pass
    try:
        container.wait(timeout=_KILL_GRACE_S)
    except (DockerException, requests.RequestException):
        pass
    return None, True


def _read_report(container, path: str) -> Optional[tuple[int, int]]:
    full = path if path.startswith("/") else f"{SANDBOX_WORKDIR}/{path}"
    try:
        chunks, _ = container.get_archive(full)
        data = read_tar_file(chunks)
    except (NotFound, DockerException, tarfile.TarError):
        return None
    if not data:
        return None
    try:
        return parse_junit_counts(data)
    except ET.ParseError:
        return None


def run_in_sandbox(
    files: dict[str, str],
    command: list[str],
    *,
    timeout_s: Optional[int] = None,
) -> SandboxResult:
    """
    Copy `files` into /work of a fresh sandbox container, run `command`, and
    return its result. Raises SandboxError(SANDBOX_UNAVAILABLE) if Docker or
    the image is missing. A timeout does NOT raise: the result has
    timed_out=True (error code SANDBOX_TIMEOUT for the caller).
    """
    timeout = timeout_s if timeout_s is not None else settings.WB_SANDBOX_TIMEOUT_S
    client = _docker_client()
    container = None
    start = time.monotonic()
    try:
        _cleanup_once(client)
        container = _create_container(client, command)
        container.put_archive(SANDBOX_WORKDIR, make_tar(files))
        start = time.monotonic()
        container.start()
        exit_code, timed_out = _wait(container, timeout)
        duration_ms = int((time.monotonic() - start) * 1000)

        container.reload()
        state = container.attrs.get("State", {})
        oom_killed = bool(state.get("OOMKilled", False))
        if exit_code is None:
            exit_code = int(state.get("ExitCode", _EXIT_SIGKILL) or _EXIT_SIGKILL)

        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

        result = SandboxResult(
            exit_code=exit_code,
            stdout=truncate_output(stdout),
            stderr=truncate_output(stderr),
            timed_out=timed_out,
            duration_ms=duration_ms,
            oom_killed=oom_killed,
        )
        report = _junit_path(command)
        if report and not timed_out:
            counts = _read_report(container, report)
            if counts is not None:
                result.tests_passed, result.tests_failed = counts
    except SandboxError:
        raise
    except (DockerException, requests.RequestException) as exc:
        raise SandboxError("SANDBOX_UNAVAILABLE", f"Docker error while running sandbox: {exc}") from exc
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except (DockerException, requests.RequestException):
                pass

    write_audit_record(
        kind="tool",
        name="sandbox",
        duration_ms=result.duration_ms,
        ok=result.ok,
        detail={
            "command": command,
            "files": sorted(files),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "oom_killed": result.oom_killed,
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
        },
    )
    return result


def sandbox_available() -> bool:
    """True if Docker is reachable and the sandbox image exists (for health checks)."""
    try:
        client = _docker_client()
        client.images.get(settings.WB_SANDBOX_IMAGE)
    except (SandboxError, DockerException, requests.RequestException):
        return False
    return True
