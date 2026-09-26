"""
Environment health check for the Sovereign AI Workbench (Track A).

Run from the repo root:
    python scripts/doctor.py

Exits 0 if every check passes, 1 if any check fails.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.settings import settings  # noqa: E402

REQUIRED_MODELS = ["qwen3.5:4b", "qwen2.5-coder:3b", "bge-m3"]

_results: list[bool] = []


def check(ok: bool, message: str) -> None:
    _results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def check_python_version() -> None:
    version = sys.version.split()[0]
    ok = sys.version_info[:2] == (3, 11)
    suffix = "" if ok else " (expected 3.11.x)"
    check(ok, f"Python version: {version}{suffix}")


def check_installed_packages() -> None:
    lock_path = _REPO_ROOT / "requirements.lock.txt"
    if not lock_path.exists():
        check(False, "Installed packages: requirements.lock.txt not found")
        return

    required: dict[str, str] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        required[_normalize(name)] = version.strip()

    proc = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True,
        text=True,
        check=False,
    )
    installed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        installed[_normalize(name)] = version.strip()

    mismatches = []
    for name, version in sorted(required.items()):
        got = installed.get(name)
        if got is None:
            mismatches.append(f"{name} missing")
        elif got != version:
            mismatches.append(f"{name} {got} != {version}")

    if mismatches:
        check(False, "Installed packages match requirements.lock.txt: " + ", ".join(mismatches))
    else:
        check(True, f"Installed packages match requirements.lock.txt ({len(required)} packages)")


def check_ollama_reachable() -> bool:
    import httpx

    try:
        resp = httpx.get(settings.OLLAMA_HOST, timeout=5.0)
        ok = resp.status_code < 500
    except Exception as exc:
        check(False, f"Ollama reachable at {settings.OLLAMA_HOST}: {exc}")
        return False
    check(ok, f"Ollama reachable at {settings.OLLAMA_HOST}")
    return ok


def check_ollama_models(ollama_up: bool) -> None:
    if not ollama_up:
        check(False, "Ollama models: skipped, Ollama not reachable")
        return
    try:
        proc = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        check(False, "Ollama models: `ollama` executable not found on PATH")
        return
    if proc.returncode != 0:
        check(False, f"Ollama models: `ollama list` failed: {proc.stderr.strip()}")
        return
    missing = [m for m in REQUIRED_MODELS if m not in proc.stdout]
    if missing:
        check(False, "Ollama models: missing " + ", ".join(missing))
    else:
        check(True, "Ollama models: " + ", ".join(REQUIRED_MODELS))


def check_tesseract() -> None:
    cmd = str(settings.TESSERACT_CMD)
    try:
        proc = subprocess.run([cmd, "--version"], capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError) as exc:
        check(False, f"Tesseract callable: {cmd}: {exc}")
        return
    if proc.returncode == 0:
        first_line = proc.stdout.splitlines()[0] if proc.stdout else "tesseract"
        check(True, f"Tesseract callable: {first_line}")
    else:
        check(False, f"Tesseract callable: `{cmd} --version` failed")


def check_docker_running() -> bool:
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        check(False, "Docker running: `docker` executable not found on PATH")
        return False
    ok = proc.returncode == 0
    if ok:
        check(True, "Docker running")
    else:
        last_line = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown error"
        check(False, f"Docker running: `docker info` failed: {last_line}")
    return ok


def check_docker_image(docker_up: bool) -> None:
    image = settings.WB_SANDBOX_IMAGE
    if not docker_up:
        check(False, f"Docker image {image}: skipped, Docker not running")
        return
    proc = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    images = proc.stdout.splitlines()
    ok = image in images
    check(ok, f"Docker image {image} exists" if ok else f"Docker image {image}: not found")


def check_env_keys() -> None:
    example_path = _REPO_ROOT / ".env.example"
    env_path = _REPO_ROOT / ".env"
    if not example_path.exists():
        check(False, "Env keys: .env.example not found")
        return
    if not env_path.exists():
        check(False, "Env keys: .env not found")
        return

    def load_keys(path: Path) -> dict[str, str]:
        keys: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            keys[key.strip()] = value.strip()
        return keys

    example_keys = load_keys(example_path)
    env_keys = load_keys(env_path)

    missing = [k for k in example_keys if not env_keys.get(k)]
    if missing:
        check(False, "Env keys: missing values in .env for " + ", ".join(missing))
    else:
        check(True, f"Env keys: all {len(example_keys)} keys from .env.example set in .env")


def user_env_value(name: str) -> str | None:
    """A variable from the Windows USER environment (what `setx` writes), else the process environment."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None
    import os

    return os.environ.get(name)


def check_ollama_no_cloud() -> None:
    """Ollama's cloud features (remote models, web search) must be off for the sovereign demo."""
    value = user_env_value("OLLAMA_NO_CLOUD")
    if value is not None and value.strip() == "1":
        check(True, "OLLAMA_NO_CLOUD is set to 1 (user environment)")
    else:
        now = "not set" if value is None else f"is {value!r}"
        check(False, f"OLLAMA_NO_CLOUD {now} (user environment). Fix: setx OLLAMA_NO_CLOUD 1, then restart Ollama "
                     "(scripts/start_demo.ps1 does this)")


def main() -> int:
    check_python_version()
    check_installed_packages()
    ollama_up = check_ollama_reachable()
    check_ollama_models(ollama_up)
    check_ollama_no_cloud()
    check_tesseract()
    docker_up = check_docker_running()
    check_docker_image(docker_up)
    check_env_keys()

    return 0 if all(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
