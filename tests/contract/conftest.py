"""
Contract tests (B1): black-box HTTP checks of a running server against shared.contracts.
The server is chosen with --base-url (default: WB_API_URL from .env, env var wins).
Every test is skipped with a clear message when that server is not reachable.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FALLBACK_URL = "http://127.0.0.1:8001"


def _default_base_url() -> str:
    return os.environ.get("WB_API_URL") or dotenv_values(_REPO_ROOT / ".env").get("WB_API_URL") or _FALLBACK_URL


def pytest_addoption(parser):
    parser.addoption("--base-url", action="store", default=None,
                     help="API server for contract tests (default: WB_API_URL from .env)")


def pytest_configure(config):
    config.addinivalue_line("markers", "flow: full upload -> task -> events -> artifacts run (slow, uses the models)")


@pytest.fixture(scope="session")
def base_url(pytestconfig) -> str:
    return (pytestconfig.getoption("--base-url", default=None) or _default_base_url()).rstrip("/")


@pytest.fixture(scope="session")
def api(base_url):
    """httpx client for the server under test; skips everything if it does not answer."""
    try:
        httpx.get(f"{base_url}/api/health", timeout=15.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"backend not reachable at {base_url} ({type(exc).__name__}); start it or pass --base-url")
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        yield client
