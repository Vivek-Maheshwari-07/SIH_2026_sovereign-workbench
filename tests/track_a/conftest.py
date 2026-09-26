"""Shared pytest configuration and fixtures for tests/track_a/."""
from __future__ import annotations

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
