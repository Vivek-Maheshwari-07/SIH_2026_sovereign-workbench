"""Shared pytest configuration for tests/track_a/."""
from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests as slow (e.g. a real Ollama vision call)")
