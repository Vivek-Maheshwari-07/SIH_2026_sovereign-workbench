"""
Reads whether Windows Firewall blocks outbound traffic, for
NetworkStatus.firewall_outbound_blocked. Runs Get-NetFirewallProfile in a
powershell subprocess (no admin needed to read; takes 1-4 s) in a background
thread and caches the answer for FIREWALL_CACHE_S seconds, so the status
endpoint (polled every second) never waits for powershell.

True  = the Domain, Private and Public profiles are all enabled with
        DefaultOutboundAction Block (what scripts/firewall_block.ps1 sets).
False = at least one profile is disabled or allows outbound.
None  = unknown: not Windows, powershell failed, or the output was unreadable.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import Callable, Optional

FIREWALL_CACHE_S = 30.0
POWERSHELL_TIMEOUT_S = 10.0
EXPECTED_PROFILES = frozenset({"Domain", "Private", "Public"})

# One "Name|Enabled|DefaultOutboundAction" line per profile; string formatting
# avoids PowerShell 5.1 turning the enums into numbers.
_PS_COMMAND = (
    "Get-NetFirewallProfile | ForEach-Object "
    "{ '{0}|{1}|{2}' -f $_.Name, $_.Enabled, $_.DefaultOutboundAction }"
)

_lock = threading.Lock()
_cache: Optional[tuple[float, Optional[bool]]] = None   # (monotonic time read, value)
_refreshing = False


def _run_powershell() -> str:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_COMMAND],
        capture_output=True, text=True, timeout=POWERSHELL_TIMEOUT_S, creationflags=creationflags,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"powershell exit code {proc.returncode}")
    return proc.stdout


def parse_profiles(output: str) -> Optional[bool]:
    profiles: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3 and parts[0] in EXPECTED_PROFILES:
            profiles[parts[0]] = (parts[1], parts[2])
    if set(profiles) != EXPECTED_PROFILES:
        return None
    return all(enabled == "True" and action == "Block" for enabled, action in profiles.values())


def refresh_now(run: Optional[Callable[[], str]] = None) -> Optional[bool]:
    """Reads the firewall synchronously (1-4 s for powershell) and caches the answer."""
    global _cache
    if not sys.platform.startswith("win"):
        value: Optional[bool] = None
    else:
        try:
            value = parse_profiles((run or _run_powershell)())
        except Exception:
            value = None
    with _lock:
        _cache = (time.monotonic(), value)
    return value


def _background_refresh() -> None:
    global _refreshing
    try:
        refresh_now()
    finally:
        with _lock:
            _refreshing = False


def firewall_outbound_blocked() -> Optional[bool]:
    """Never blocks: returns the cached value and starts a background re-read when it is stale."""
    global _refreshing
    with _lock:
        stale = _cache is None or time.monotonic() - _cache[0] >= FIREWALL_CACHE_S
        if stale and not _refreshing:
            _refreshing = True
            threading.Thread(target=_background_refresh, name="firewall-read", daemon=True).start()
        return _cache[1] if _cache is not None else None


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None
