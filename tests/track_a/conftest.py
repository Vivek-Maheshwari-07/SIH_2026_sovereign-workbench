"""Shared pytest configuration and fixtures for tests/track_a/."""
from __future__ import annotations

import logging
import socket

import pytest


def pytest_addoption(parser):
    parser.addoption("--runslow", action="store_true", default=False,
                     help="also run tests marked slow (live Ollama / end-to-end)")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: live Ollama / end-to-end test; skipped unless --runslow or -m slow")


def pytest_collection_modifyitems(config, items):
    """Plain runs skip slow tests; `--runslow` or a -m expression naming "slow" runs them."""
    if config.getoption("--runslow", default=False) or "slow" in (config.getoption("markexpr", default="") or ""):
        return
    skip = pytest.mark.skip(reason="slow/live test: run with --runslow or -m slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------- session-wide isolation
class RealProbeDisabled(OSError):
    """Raised if a test reaches the network probe's real connect without mocking it."""


def _blocked_probe_connect(ip, port, timeout_s):
    raise RealProbeDisabled(f"real network probe disabled in tests ({ip}:{port})")


def _no_powershell() -> str:
    raise RuntimeError("firewall read disabled in tests")


@pytest.fixture(scope="session", autouse=True)
def _isolated_session(tmp_path_factory):
    """
    For the whole test session: audit + backend.log go to a temp logs dir (never the real
    logs/), the network monitor reads an empty connection table, the firewall read returns
    None without starting powershell, and the probe cannot open a real connection or do a
    real DNS lookup. Tests that exercise these pieces override them per test.
    """
    from backend import net_probe
    from backend.settings import settings
    from monitor import firewall
    from monitor.net_monitor import monitor

    logs_dir = tmp_path_factory.mktemp("session_logs")
    backend_logger = logging.getLogger("backend")
    saved_handlers = backend_logger.handlers[:]
    temp_handler = logging.FileHandler(logs_dir / "backend.log", encoding="utf-8")
    backend_logger.handlers = [temp_handler]
    firewall.clear_cache()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "WB_LOG_DIR", logs_dir)
        mp.setattr(monitor, "connections_fn", lambda kind="inet": [])
        mp.setattr(firewall, "_run_powershell", _no_powershell)
        mp.setattr(net_probe, "_resolve", lambda host, port: ["192.0.2.1"])  # TEST-NET-1, never routed
        mp.setattr(net_probe, "_connect", _blocked_probe_connect)
        try:
            yield logs_dir
        finally:
            backend_logger.handlers = saved_handlers
            temp_handler.close()
            firewall.clear_cache()


# ---------------------------------------------------------------- network guard
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


class BlockedExternalConnection(OSError):
    """Raised by the network guard for any non-loopback connection attempt."""


def _is_local(address) -> bool:
    host = address[0] if isinstance(address, tuple) else str(address)
    host = str(host).strip("[]")
    return host in _LOCAL_HOSTS or host.startswith("127.")


@pytest.fixture
def network_guard(monkeypatch):
    """Block and record every connection that is not to loopback. Asserts zero at teardown."""
    blocked: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def guarded_connect(self, address):
        if not _is_local(address):
            blocked.append(repr(address))
            raise BlockedExternalConnection(f"external connection blocked by test guard: {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_local(address):
            blocked.append(repr(address))
            raise BlockedExternalConnection(f"external connection blocked by test guard: {address!r}")
        return real_connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_local(address):
            blocked.append(repr(address))
            raise BlockedExternalConnection(f"external connection blocked by test guard: {address!r}")
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield blocked
    assert blocked == [], f"external connection attempts: {blocked}"
