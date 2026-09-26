"""
The network probe behind POST /api/network/probe. This is the ONLY backend
code allowed to try an external connection: it proves isolation by trying
to reach the outside and failing.

What it does: resolve the target's host name, then open a plain TCP
connection to the first address on the target port (443 for https, 80 for
http) and close it straight away. No TLS handshake and no HTTP request, so
not a single byte of payload is sent even when the connection succeeds.
The whole probe (DNS + connect) is limited to PROBE_TIMEOUT_S.

While the probe runs, the network monitor labels our connections to the
resolved addresses as "probe" so they are not counted as leaks. Every probe
writes one audit record (kind "network", name "probe").
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

from backend.audit import write_audit_record
from monitor.net_monitor import monitor
from shared.contracts import ProbeResult

PROBE_TIMEOUT_S = 3.0
_DEFAULT_PORTS = {"https": 443, "http": 80}

_probe_lock = threading.Lock()  # one probe at a time keeps the monitor's probe window simple


def parse_target(target: str) -> tuple[str, int]:
    """'https://www.google.com' -> ('www.google.com', 443). A bare host name means https."""
    text = target.strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"unsupported scheme {parts.scheme!r} (use http or https)")
    if not parts.hostname:
        raise ValueError(f"no host name in target {target!r}")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise ValueError(f"bad port in target {target!r}") from exc
    return parts.hostname, port


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _connect(ip: str, port: int, timeout_s: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout_s)


def _resolve_within(host: str, port: int, timeout_s: float) -> list[str]:
    """getaddrinfo has no timeout of its own, so it runs in a worker thread."""
    box: dict[str, object] = {}

    def work() -> None:
        try:
            box["ips"] = _resolve(host, port)
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=work, name="probe-dns", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"DNS lookup for {host} timed out after {timeout_s:.1f} s")
    if "error" in box:
        raise OSError(f"DNS lookup for {host} failed: {box['error']}")
    ips = box.get("ips") or []
    if not ips:
        raise OSError(f"DNS lookup for {host} returned no addresses")
    return list(ips)  # type: ignore[arg-type]


def run_probe(target: str) -> ProbeResult:
    with _probe_lock:
        start = time.monotonic()
        ips: list[str] = []
        port: Optional[int] = None
        reachable, error = False, None
        try:
            host, port = parse_target(target)
            ips = _resolve_within(host, port, PROBE_TIMEOUT_S)
            monitor.begin_probe(ips)
            remaining = max(0.1, PROBE_TIMEOUT_S - (time.monotonic() - start))
            sock = _connect(ips[0], port, remaining)
            try:
                monitor.poll_once()  # record the live probe socket in the ledger, labelled "probe"
            finally:
                sock.close()
            reachable = True
        except ValueError as exc:
            error = f"invalid target: {exc}"
        except (socket.timeout, TimeoutError) as exc:
            error = f"timed out: {exc}" if str(exc) else f"timed out after {PROBE_TIMEOUT_S:.0f} s"
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            monitor.end_probe(ips)
        duration_ms = int((time.monotonic() - start) * 1000)

    write_audit_record(
        kind="network", name="probe", target=target, duration_ms=duration_ms, ok=True,
        detail={"reachable": reachable, "error": error, "resolved": ips, "port": port},
    )
    return ProbeResult(target=target, reachable=reachable, error=error, duration_ms=duration_ms)
