"""Shared pytest configuration and fixtures for tests/track_a/."""
from __future__ import annotations

import socket

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests as slow (e.g. a real Ollama vision call)")


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
