"""
Network monitor: the evidence behind "this workbench makes no external
connections". A background thread reads psutil.net_connections(kind="inet")
every WB_NET_POLL_S seconds.

Per socket:
  - ignored: listening sockets, sockets with no remote address, loopback
    remotes (127.0.0.0/8, ::1, ::ffff:127.x). Only loopback is ignored: a
    LAN address (router, NAS) still leaves this machine, so it is external.
  - group "established": ESTABLISHED and the other data states (FIN_WAIT,
    CLOSE_WAIT, TIME_WAIT, ..., and connected UDP) = a REAL external
    connection happened.
  - group "attempt": SYN_SENT (and SYN_RECV) = tried to connect but never
    did, e.g. blocked by the firewall. Shown, never counted as a leak.
  - origin "ours": the backend (this pid and its children), Ollama,
    Streamlit, Docker Desktop / WSL. These are the sovereign proof.
    origin "other": any other app on the laptop, shown for information.
    origin "probe": the backend's own /api/network/probe connection to the
    probe target (while the probe runs, plus PROBE_GRACE_S for TIME_WAIT).

Each unique (pid, remote ip, remote port, group) is recorded ONCE, with its
first-seen time, and writes one audit record plus one warning line in
logs/backend.log. The headline number (external_seen_since_start) counts
only origin "ours" + group "established".

Limits of polling: a connection that opens and closes between two polls is
missed, and a closed socket in TIME_WAIT has pid 0 on Windows (owner
unknown), so it is labelled as another app. The firewall scripts + Wi-Fi off
are the hard guarantee; this monitor is the visible evidence.

psutil errors (e.g. permissions) never stop the thread: the last good data is
kept and the error is reported in status.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal, Optional

import psutil

from backend.audit import write_audit_record
from backend.settings import settings
from shared.contracts import Connection, NetworkStatus

Group = Literal["established", "attempt"]
Origin = Literal["ours", "other", "probe"]

ATTEMPT_STATES = frozenset({psutil.CONN_SYN_SENT, psutil.CONN_SYN_RECV})
MIN_POLL_S = 0.2                 # floor for WB_NET_POLL_S so a typo can't spin a core
PROBE_GRACE_S = 240.0            # probe sockets linger in TIME_WAIT up to 4 min on Windows
STOP_JOIN_S = 5.0

# Process names (lower case, ".exe" stripped) that belong to the workbench stack.
OUR_PROCESS_NAMES = frozenset({
    "ollama", "ollama app", "ollama_llama_server",
    "streamlit",
    "docker", "dockerd", "docker desktop", "com.docker.backend", "com.docker.build", "com.docker.proxy",
    "com.docker.dev-envs", "vpnkit",
    "wsl", "wslhost", "wslservice", "wslrelay", "vmmem", "vmmemwsl",
})
_PYTHON_NAMES = frozenset({"python", "pythonw", "python3"})
ORIGIN_LABELS: dict[str, str] = {"ours": "ours", "other": "other app", "probe": "probe"}

NAME_ACCESS_DENIED = "<access denied>"
NAME_GONE = "<process gone>"
NAME_UNKNOWN = "<unknown>"
NAME_CLOSED = "<closed socket, owner unknown>"

logger = logging.getLogger("backend.net_monitor")


@dataclass(frozen=True)
class SeenConnection:
    pid: Optional[int]
    process: str
    remote_ip: str
    remote_port: int
    status: str
    group: Group
    origin: Origin
    first_seen: datetime

    @property
    def is_leak(self) -> bool:
        return self.origin == "ours" and self.group == "established"


# ---------------------------------------------------------------- pure helpers
def _normalize_name(name: str) -> str:
    name = name.strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def remote_of(conn: Any) -> Optional[tuple[str, int]]:
    """(ip, port) of the remote end, or None if there is no real remote address."""
    raddr = getattr(conn, "raddr", None)
    if not raddr:
        return None
    ip, port = raddr[0], raddr[1]
    if not ip or ip in ("0.0.0.0", "::"):
        return None
    return ip.split("%", 1)[0], int(port)


def is_loopback(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def state_group(status: str) -> Group:
    return "attempt" if status in ATTEMPT_STATES else "established"


def _fmt(ip: str, port: int) -> str:
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _fmt_local(conn: Any) -> str:
    laddr = getattr(conn, "laddr", None)
    return _fmt(laddr[0], int(laddr[1])) if laddr else ""


# ---------------------------------------------------------------- monitor
class NetMonitor:
    def __init__(
        self,
        *,
        connections_fn: Callable[..., Iterable[Any]] = psutil.net_connections,
        process_fn: Callable[[int], Any] = psutil.Process,
        own_pid: Optional[int] = None,
        log: logging.Logger = logger,
    ) -> None:
        self.connections_fn = connections_fn
        self.process_fn = process_fn
        self.own_pid = own_pid if own_pid is not None else os.getpid()
        self.log = log

        self._poll_lock = threading.Lock()      # one poll at a time (thread + probe)
        self._state_lock = threading.Lock()     # guards everything below
        self._seen: dict[tuple, SeenConnection] = {}
        self._current: list[Connection] = []
        self._current_leaks = 0
        self._total_connections = 0
        self._checked_at: Optional[datetime] = None
        self._error: Optional[str] = None
        self._probe_ips: dict[str, float] = {}  # ip -> monotonic expiry (inf while the probe runs)
        self._describe_cache: dict[int, tuple[str, bool]] = {}
        self.started_at = datetime.now(timezone.utc)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._run, name="net-monitor", daemon=True)
        self._thread.start()
        self.log.info("Network monitor started (poll every %.1f s)", self._poll_s())

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_JOIN_S)
            self._thread = None
        self.log.info("Network monitor stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _poll_s(self) -> float:
        return max(MIN_POLL_S, float(settings.WB_NET_POLL_S))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # the monitor must never die
                self._set_error(f"monitor poll failed: {exc!r}")
            self._stop.wait(self._poll_s())

    # ------------------------------------------------------------ probe window
    def begin_probe(self, ips: Iterable[str]) -> None:
        with self._state_lock:
            for ip in ips:
                self._probe_ips[ip] = math.inf

    def end_probe(self, ips: Iterable[str]) -> None:
        expiry = time.monotonic() + PROBE_GRACE_S
        with self._state_lock:
            for ip in ips:
                self._probe_ips[ip] = expiry

    def _active_probe_ips(self) -> set[str]:
        now = time.monotonic()
        with self._state_lock:
            for ip in [ip for ip, expiry in self._probe_ips.items() if expiry < now]:
                del self._probe_ips[ip]
            return set(self._probe_ips)

    # ------------------------------------------------------------ processes
    def _our_pids(self) -> set[int]:
        pids = {self.own_pid}
        try:
            pids.update(child.pid for child in self.process_fn(self.own_pid).children(recursive=True))
        except Exception:
            pass  # children unreadable: the backend pid itself is still ours
        return pids

    def _describe(self, pid: Optional[int]) -> tuple[str, bool]:
        """(process name, belongs to the workbench stack by name). Cached per pid."""
        if not pid:
            return NAME_CLOSED, False
        cached = self._describe_cache.get(pid)
        if cached is not None:
            return cached
        try:
            process = self.process_fn(pid)
            name = process.name()
        except psutil.AccessDenied:
            result = (NAME_ACCESS_DENIED, False)
        except psutil.NoSuchProcess:
            result = (NAME_GONE, False)
        except Exception:
            result = (NAME_UNKNOWN, False)
        else:
            normalized = _normalize_name(name)
            ours = normalized in OUR_PROCESS_NAMES
            if not ours and normalized in _PYTHON_NAMES:
                ours = self._is_streamlit(process)
            result = (name, ours)
        self._describe_cache[pid] = result
        return result

    @staticmethod
    def _is_streamlit(process: Any) -> bool:
        try:
            return any("streamlit" in part.lower() for part in process.cmdline())
        except Exception:
            return False

    def _origin(self, pid: Optional[int], ours_by_name: bool, ip: str, our_pids: set[int],
                probe_ips: set[str]) -> Origin:
        if ip in probe_ips and (pid in our_pids or not pid):
            return "probe"
        if ours_by_name or pid in our_pids:
            return "ours"
        return "other"

    # ------------------------------------------------------------ polling
    def poll_once(self) -> None:
        with self._poll_lock:
            try:
                raw = list(self.connections_fn(kind="inet"))
            except Exception as exc:
                self._set_error(f"psutil.net_connections failed: {exc!r}")
                return
            self._process(raw)

    def _process(self, raw: list[Any]) -> None:
        our_pids = self._our_pids()
        probe_ips = self._active_probe_ips()
        now = datetime.now(timezone.utc)
        total = 0
        current: list[Connection] = []
        current_leaks = 0
        new: list[SeenConnection] = []
        live_pids: set[int] = set()

        for conn in raw:
            if conn.status == psutil.CONN_LISTEN:
                continue
            remote = remote_of(conn)
            if remote is None:
                continue
            total += 1
            ip, port = remote
            if is_loopback(ip):
                continue
            pid = conn.pid
            if pid:
                live_pids.add(pid)
            name, ours_by_name = self._describe(pid)
            group = state_group(conn.status)
            origin = self._origin(pid, ours_by_name, ip, our_pids, probe_ips)
            current.append(Connection(
                pid=pid, process=f"{name} [{ORIGIN_LABELS[origin]}]", local=_fmt_local(conn),
                remote=_fmt(ip, port), status=conn.status, external=True))
            if origin == "ours" and group == "established":
                current_leaks += 1
            key = (pid, ip, port, group)
            with self._state_lock:
                if key in self._seen:
                    continue
                seen = SeenConnection(pid, name, ip, port, conn.status, group, origin, now)
                self._seen[key] = seen
            new.append(seen)

        self._describe_cache = {pid: v for pid, v in self._describe_cache.items() if pid in live_pids}
        with self._state_lock:
            self._current = current
            self._current_leaks = current_leaks
            self._total_connections = total
            self._checked_at = now
            if self._error is not None:
                self.log.info("Network monitor recovered after: %s", self._error)
            self._error = None
        for seen in new:
            self._report(seen)

    def _set_error(self, message: str) -> None:
        with self._state_lock:
            changed = message != self._error
            self._error = message
        if changed:  # log once per distinct error, not every poll
            self.log.error("Network monitor: %s (monitor keeps running)", message)

    def _report(self, seen: SeenConnection) -> None:
        label = ORIGIN_LABELS[seen.origin]
        self.log.warning(
            "New external connection%s [%s, %s]: %s (pid %s) -> %s status %s",
            " (LEAK)" if seen.is_leak else "", label, seen.group, seen.process, seen.pid,
            _fmt(seen.remote_ip, seen.remote_port), seen.status)
        detail = asdict(seen)
        detail["first_seen"] = seen.first_seen.isoformat()
        detail["label"] = label
        write_audit_record(kind="network", name="external_connection",
                           target=_fmt(seen.remote_ip, seen.remote_port), ok=not seen.is_leak, detail=detail)

    # ------------------------------------------------------------ read side
    def seen(self) -> list[SeenConnection]:
        with self._state_lock:
            return sorted(self._seen.values(), key=lambda s: s.first_seen)

    def summary(self) -> dict[str, Any]:
        """Everything status() can't carry in the contract (see main.py: X-Net-* headers)."""
        seen = self.seen()
        with self._state_lock:
            error = self._error
        return {
            "since": self.started_at,
            "leaks_since_start": sum(1 for s in seen if s.is_leak),
            "attempts_since_start": sum(1 for s in seen if s.group == "attempt" and s.origin != "probe"),
            "other_apps_since_start": sum(1 for s in seen if s.origin == "other" and s.group == "established"),
            "probe_since_start": sum(1 for s in seen if s.origin == "probe"),
            "error": error,
        }

    def status(self, firewall_outbound_blocked: Optional[bool]) -> NetworkStatus:
        leaks = self.summary()["leaks_since_start"]
        with self._state_lock:
            return NetworkStatus(
                checked_at=self._checked_at or datetime.now(timezone.utc),
                external_count=self._current_leaks,
                external_seen_since_start=leaks,
                total_connections=self._total_connections,
                firewall_outbound_blocked=firewall_outbound_blocked,
                connections=list(self._current),
            )


monitor = NetMonitor()
