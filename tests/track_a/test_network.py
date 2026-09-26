"""
Tests for ticket A9 network proof: monitor/net_monitor.py (fake psutil
connection tables), monitor/firewall.py (fake powershell output),
backend/net_probe.py (mocked resolve/connect) and the two API endpoints.
No real internet: the network guard is on for every test, and all logs and
workspace files go to temp folders.
"""
from __future__ import annotations

import logging
import socket
import time
from types import SimpleNamespace

import psutil
import pytest
from fastapi.testclient import TestClient

from backend import net_probe
from backend.audit import read_audit_records
from backend.settings import settings
from monitor import firewall
from monitor import net_monitor
from monitor.net_monitor import NetMonitor, is_loopback, remote_of, state_group
from shared.contracts import API_PREFIX, NetworkStatus, ProbeResult

pytestmark = pytest.mark.usefixtures("network_guard")

BACKEND_PID = 1000
CHILD_PID = 1001
GOOGLE = "142.250.1.1"


@pytest.fixture(autouse=True)
def _temp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WB_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "WB_WORKSPACE_DIR", tmp_path / "workspace")


# ---------------------------------------------------------------- fakes
def conn(remote=(GOOGLE, 443), status="ESTABLISHED", pid=BACKEND_PID, local=("10.0.0.5", 50000)):
    return SimpleNamespace(laddr=local, raddr=remote or (), status=status, pid=pid)


class FakeProcess:
    def __init__(self, pid, name="python.exe", error=None, cmdline=(), children=()):
        self.pid, self._name, self._error, self._cmdline, self._children = pid, name, error, cmdline, children

    def name(self):
        if self._error is not None:
            raise self._error
        return self._name

    def cmdline(self):
        return list(self._cmdline)

    def children(self, recursive=False):
        return [SimpleNamespace(pid=c) for c in self._children]


def make_monitor(table, processes=None):
    """NetMonitor over a mutable fake connection table (a list the test can change between polls)."""
    procs = {BACKEND_PID: FakeProcess(BACKEND_PID, "python.exe", children=(CHILD_PID,)),
             CHILD_PID: FakeProcess(CHILD_PID, "python.exe")}
    procs.update(processes or {})

    def process_fn(pid):
        if pid not in procs:
            raise psutil.NoSuchProcess(pid)
        return procs[pid]

    return NetMonitor(connections_fn=lambda kind="inet": list(table), process_fn=process_fn, own_pid=BACKEND_PID)


def network_audit():
    return [r for r in read_audit_records(limit=1000) if r.kind == "network" and r.name == "external_connection"]


# ---------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("ip, expected", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("142.250.1.1", False), ("192.168.1.10", False), ("2001:db8::1", False), ("fe80::1%12", False),
])
def test_is_loopback(ip, expected):
    assert is_loopback(ip) is expected


def test_remote_of_and_state_group():
    assert remote_of(conn(remote=())) is None and remote_of(conn(remote=("0.0.0.0", 0))) is None
    assert remote_of(conn(remote=("fe80::1%12", 80))) == ("fe80::1", 80)
    assert state_group("SYN_SENT") == "attempt" and state_group("SYN_RECV") == "attempt"
    assert {state_group(s) for s in ("ESTABLISHED", "TIME_WAIT", "CLOSE_WAIT", "NONE")} == {"established"}


# ---------------------------------------------------------------- monitor classification
def test_listening_no_remote_and_loopback_are_ignored():
    table = [
        conn(remote=(), status="LISTEN"),
        conn(remote=(), status="NONE"),                         # UDP socket, no remote
        conn(remote=("127.0.0.1", 11434)),
        conn(remote=("127.3.3.3", 8000)),
        conn(remote=("::1", 8501)),
        conn(remote=("::ffff:127.0.0.1", 8000)),
    ]
    mon = make_monitor(table)
    mon.poll_once()
    status = mon.status(None)
    assert status.connections == [] and status.external_count == 0 and status.external_seen_since_start == 0
    assert status.total_connections == 4                        # loopback counts as a connection, not external
    assert network_audit() == []


def test_external_established_counted_once_over_many_polls(caplog):
    table = [conn()]
    mon = make_monitor(table)
    with caplog.at_level(logging.WARNING, logger="backend.net_monitor"):
        for _ in range(5):
            mon.poll_once()
    status = mon.status(None)
    assert status.external_seen_since_start == 1 and status.external_count == 1
    first = status.connections[0]
    assert first.remote == f"{GOOGLE}:443" and first.process == "python.exe [ours: core]"
    assert first.group == "established" and first.origin == "ours" and first.component == "core"
    assert first.first_seen is not None
    assert status.platform_seen_since_start == 0 and status.platform_attempts_since_start == 0
    assert status.since == mon.started_at and status.monitor_error is None
    assert status.attempts_since_start == 0 and status.other_apps_since_start == 0 and status.probe_since_start == 0
    mon.poll_once()
    assert mon.status(None).connections[0].first_seen == first.first_seen     # first-seen time is kept
    records = network_audit()
    assert len(records) == 1 and records[0].ok is False and records[0].detail["origin"] == "ours"
    warnings = [r for r in caplog.records if "New external connection" in r.getMessage()]
    assert len(warnings) == 1 and "LEAK" in warnings[0].getMessage()

    table.clear()                                               # connection closed
    mon.poll_once()
    status = mon.status(None)
    assert status.external_count == 0 and status.external_seen_since_start == 1


def test_syn_sent_is_an_attempt_not_a_leak():
    mon = make_monitor([conn(status="SYN_SENT")])
    mon.poll_once()
    mon.poll_once()
    status, summary = mon.status(None), mon.summary()
    assert status.external_seen_since_start == 0 and status.external_count == 0
    assert summary["attempts_since_start"] == 1 and status.attempts_since_start == 1
    assert [(c.status, c.group, c.origin) for c in status.connections] == [("SYN_SENT", "attempt", "ours")]
    records = network_audit()
    assert len(records) == 1 and records[0].ok is True and records[0].detail["group"] == "attempt"


def test_same_socket_attempt_then_established_is_two_records():
    table = [conn(status="SYN_SENT")]
    mon = make_monitor(table)
    mon.poll_once()
    table[0] = conn(status="ESTABLISHED")
    mon.poll_once()
    assert mon.summary()["attempts_since_start"] == 1 and mon.status(None).external_seen_since_start == 1


def test_probe_connection_is_labelled_probe_and_not_counted():
    table = [conn(), conn(remote=(GOOGLE, 443), status="TIME_WAIT", pid=0), conn(remote=("8.8.8.8", 53))]
    mon = make_monitor(table)
    mon.begin_probe([GOOGLE])
    mon.poll_once()
    status, summary = mon.status(None), mon.summary()
    by_remote = {(c.remote, c.status): c for c in status.connections}
    assert by_remote[(f"{GOOGLE}:443", "ESTABLISHED")].process.endswith("[probe]")
    assert by_remote[(f"{GOOGLE}:443", "TIME_WAIT")].origin == "probe"      # closed probe socket, pid 0
    assert by_remote[("8.8.8.8:53", "ESTABLISHED")].origin == "ours"
    assert status.external_seen_since_start == 1                            # only 8.8.8.8 is a leak
    assert summary["probe_since_start"] == 2 and status.probe_since_start == 2

    mon.end_probe([GOOGLE])                                                 # grace period: still probe
    table.append(conn(remote=(GOOGLE, 443), local=("10.0.0.5", 50001)))
    mon.poll_once()
    assert mon.status(None).external_seen_since_start == 1


def test_probe_window_expires(monkeypatch):
    mon = make_monitor([conn()])
    mon.begin_probe([GOOGLE])
    mon.end_probe([GOOGLE])
    monkeypatch.setattr(net_monitor, "PROBE_GRACE_S", -1.0)
    mon.end_probe([GOOGLE])                                                 # already expired
    mon.poll_once()
    assert mon.status(None).external_seen_since_start == 1


def test_process_name_access_denied_and_gone_are_handled():
    table = [conn(pid=4242, remote=("20.1.1.1", 443)), conn(pid=5353, remote=("20.1.1.2", 443))]
    mon = make_monitor(table, {4242: FakeProcess(4242, error=psutil.AccessDenied(4242))})  # 5353: no such process
    mon.poll_once()
    names = sorted(c.process for c in mon.status(None).connections)
    assert names == ["<access denied> [other app]", "<process gone> [other app]"]
    assert mon.status(None).external_seen_since_start == 0 and mon.summary()["other_apps_since_start"] == 2


CORE_PIDS = (CHILD_PID, 2001, 2003, 2004)
PLATFORM_PIDS = (2002, 4001, 4002, 4003, 4004, 4005, 4006)
OTHER_PIDS = (3001, 3002, 3003)


def _mixed_processes():
    return {
        2001: FakeProcess(2001, "ollama.exe"),
        2002: FakeProcess(2002, "ollama app.exe"),                                   # tray app / updater
        2003: FakeProcess(2003, "ollama_llama_server.exe"),
        2004: FakeProcess(2004, "python.exe", cmdline=("python", "-m", "streamlit", "run", "ui/app.py")),
        4001: FakeProcess(4001, "com.docker.backend.exe"),
        4002: FakeProcess(4002, "Docker Desktop.exe"),
        4003: FakeProcess(4003, "vpnkit.exe"),
        4004: FakeProcess(4004, "wslrelay.exe"),
        4005: FakeProcess(4005, "wslservice.exe"),
        4006: FakeProcess(4006, "vmmemWSL"),
        3001: FakeProcess(3001, "python.exe", cmdline=("python", "-m", "pylsp")),   # IDE language server
        3002: FakeProcess(3002, "chrome.exe"),
        3003: FakeProcess(3003, "language_server_windows_x64.exe"),
    }


def test_core_vs_platform_vs_other_app_marking():
    procs = _mixed_processes()
    table = [conn(pid=pid, remote=(f"20.0.{pid // 1000}.{pid % 100}", 443)) for pid in procs]
    table.append(conn(pid=CHILD_PID, remote=("20.0.9.9", 443)))            # backend child (sandbox runner etc.)
    mon = make_monitor(table, procs)
    mon.poll_once()
    by_pid = {c.pid: c for c in mon.status(None).connections}

    for pid in CORE_PIDS:
        assert (by_pid[pid].origin, by_pid[pid].component) == ("ours", "core"), pid
        assert by_pid[pid].process.endswith("[ours: core]")
    for pid in PLATFORM_PIDS:
        assert (by_pid[pid].origin, by_pid[pid].component) == ("ours", "platform"), pid
        assert by_pid[pid].process.endswith("[ours: platform]")
    for pid in OTHER_PIDS:
        assert (by_pid[pid].origin, by_pid[pid].component) == ("other_app", None), pid
        assert by_pid[pid].process.endswith("[other app]")


def test_docker_backend_is_platform_not_core_and_not_hidden(caplog):
    """The real A9 finding: com.docker.backend.exe update checks / usage stats to AWS and Cloudflare."""
    procs = {4001: FakeProcess(4001, "com.docker.backend.exe")}
    table = [conn(pid=4001, remote=("52.1.2.3", 443)), conn(pid=4001, remote=("104.16.1.1", 443)),
             conn(pid=4001, remote=("52.9.9.9", 443), status="SYN_SENT")]
    mon = make_monitor(table, procs)
    with caplog.at_level(logging.WARNING, logger="backend.net_monitor"):
        mon.poll_once()
        mon.poll_once()
    status = mon.status(None)
    assert status.external_seen_since_start == 0 and status.external_count == 0     # core headline stays 0
    assert status.platform_seen_since_start == 2 and status.platform_attempts_since_start == 1
    assert status.attempts_since_start == 1                                         # platform attempt included
    assert len(status.connections) == 3                                             # shown, not hidden

    platform = [r for r in read_audit_records(limit=100) if r.name == "platform_connection"]
    assert len(platform) == 3 and network_audit() == []                             # separate audit name
    assert sorted(r.ok for r in platform) == [False, False, True]                   # attempt never connected
    assert all(r.detail["component"] == "platform" and r.detail["label"] == "ours: platform" for r in platform)
    messages = [r.getMessage() for r in caplog.records if "New external connection" in r.getMessage()]
    assert len(messages) == 3 and sum("PLATFORM" in m for m in messages) == 2
    assert not any("LEAK" in m for m in messages)


@pytest.mark.parametrize("name, expected", [
    ("ollama.exe", "core"), ("OLLAMA.EXE", "core"), ("ollama_llama_server.exe", "core"),
    ("ollama app.exe", "platform"), ("Ollama App.exe", "platform"), ("ollama-helper.exe", None),
])
def test_ollama_server_is_core_tray_app_is_platform(name, expected):
    assert net_monitor.component_by_name(net_monitor._normalize_name(name)) == expected


def test_ollama_tray_app_update_check_is_platform_not_a_leak(caplog):
    """The real finding from the UI: "ollama app.exe" (tray app) reached an update server on 443."""
    procs = {2001: FakeProcess(2001, "ollama.exe"), 2002: FakeProcess(2002, "ollama app.exe")}
    table = [conn(pid=2002, remote=("34.36.133.15", 443))]
    mon = make_monitor(table, procs)
    with caplog.at_level(logging.WARNING, logger="backend.net_monitor"):
        mon.poll_once()
        mon.poll_once()
    status = mon.status(None)
    assert status.external_seen_since_start == 0 and status.external_count == 0     # headline stays 0
    assert status.platform_seen_since_start == 1                                    # counted under platform
    assert [(c.process, c.component) for c in status.connections] == [("ollama app.exe [ours: platform]", "platform")]
    platform = [r for r in read_audit_records(limit=100) if r.name == "platform_connection"]
    assert len(platform) == 1 and network_audit() == []
    assert platform[0].detail["process"] == "ollama app.exe" and platform[0].ok is False   # flagged, not hidden
    messages = [r.getMessage() for r in caplog.records if "New external connection" in r.getMessage()]
    assert len(messages) == 1 and "Ollama tray app" in messages[0] and "LEAK" not in messages[0]

    table.append(conn(pid=2001, remote=("34.36.133.16", 443)))                      # the model server itself
    mon.poll_once()
    assert mon.status(None).external_seen_since_start == 1                          # that IS a core leak


def test_python_outside_backend_pid_tree_is_other_app():
    procs = {3001: FakeProcess(3001, "python.exe", cmdline=("python", "-m", "pylsp")),
             3004: FakeProcess(3004, "python.exe", error=psutil.AccessDenied(3004))}
    table = [conn(pid=3001, remote=("20.1.1.1", 443)), conn(pid=3004, remote=("20.1.1.2", 443)),
             conn(pid=CHILD_PID, remote=("20.1.1.3", 443))]
    mon = make_monitor(table, procs)
    mon.poll_once()
    by_pid = {c.pid: c for c in mon.status(None).connections}
    assert by_pid[3001].origin == by_pid[3004].origin == "other_app"
    assert by_pid[CHILD_PID].component == "core"                                    # same exe name, in the tree
    assert mon.status(None).external_seen_since_start == 1


def test_counters_split_core_platform_other_attempts_probe():
    procs = _mixed_processes()
    table = [conn(pid=pid, remote=(f"20.0.{pid // 1000}.{pid % 100}", 443)) for pid in procs]
    table += [conn(pid=CHILD_PID, remote=("20.0.9.9", 443)),
              conn(pid=2001, remote=("20.5.5.5", 443), status="SYN_SENT"),          # core attempt
              conn(pid=4001, remote=("20.6.6.6", 443), status="SYN_SENT"),          # platform attempt
              conn(pid=BACKEND_PID, remote=(GOOGLE, 443))]                          # probe
    mon = make_monitor(table, procs)
    mon.begin_probe([GOOGLE])
    for _ in range(3):
        mon.poll_once()
    status = mon.status(None)
    assert status.external_seen_since_start == len(CORE_PIDS) and status.external_count == len(CORE_PIDS)
    assert status.platform_seen_since_start == len(PLATFORM_PIDS)
    assert status.platform_attempts_since_start == 1
    assert status.other_apps_since_start == len(OTHER_PIDS)
    assert status.attempts_since_start == 2 and status.probe_since_start == 1

    table.clear()                                                                   # all closed
    mon.poll_once()
    status = mon.status(None)
    assert status.external_count == 0 and status.external_seen_since_start == len(CORE_PIDS)


def test_psutil_error_is_reported_and_monitor_recovers():
    table = [conn()]
    calls = {"fail": False}

    def flaky(kind="inet"):
        if calls["fail"]:
            raise psutil.AccessDenied()
        return list(table)

    mon = make_monitor(table)
    mon.connections_fn = flaky
    mon.poll_once()
    calls["fail"] = True
    mon.poll_once()
    assert "AccessDenied" in mon.status(None).monitor_error
    assert mon.status(None).external_seen_since_start == 1                  # last good data kept
    calls["fail"] = False
    mon.poll_once()
    assert mon.status(None).monitor_error is None


# ---------------------------------------------------------------- monitor thread
def test_monitor_thread_starts_polls_and_stops_cleanly(monkeypatch):
    monkeypatch.setattr(settings, "WB_NET_POLL_S", 0.01)                     # clamped to MIN_POLL_S
    polls: list[int] = []

    def counting(kind="inet"):
        polls.append(1)
        if len(polls) == 2:
            raise RuntimeError("psutil hiccup")                              # thread must survive this
        return []

    mon = make_monitor([])
    mon.connections_fn = counting
    mon.start()
    mon.start()                                                               # second start is a no-op
    deadline = time.monotonic() + 5
    while len(polls) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mon.running and len(polls) >= 3
    thread = mon._thread
    mon.stop()
    assert not mon.running and not thread.is_alive()


# ---------------------------------------------------------------- firewall
def test_parse_firewall_profiles():
    all_block = "Domain|True|Block\nPrivate|True|Block\nPublic|True|Block\n"
    assert firewall.parse_profiles(all_block) is True
    assert firewall.parse_profiles(all_block.replace("Public|True|Block", "Public|True|NotConfigured")) is False
    assert firewall.parse_profiles(all_block.replace("Private|True", "Private|False")) is False
    assert firewall.parse_profiles("Domain|True|Block\n") is None
    assert firewall.parse_profiles("garbage") is None


def test_firewall_refresh_is_cached_and_never_blocks(monkeypatch):
    monkeypatch.setattr(firewall.sys, "platform", "win32")
    firewall.clear_cache()
    try:
        assert firewall.refresh_now(lambda: "Domain|True|Block\nPrivate|True|Block\nPublic|True|Block") is True
        monkeypatch.setattr(firewall, "_run_powershell", lambda: time.sleep(5) or "")
        start = time.monotonic()
        assert firewall.firewall_outbound_blocked() is True                  # fresh cache, no powershell
        assert time.monotonic() - start < 0.5
        assert firewall.refresh_now(lambda: (_ for _ in ()).throw(RuntimeError("no powershell"))) is None
    finally:
        firewall.clear_cache()


# ---------------------------------------------------------------- probe
@pytest.fixture
def probe_monitor(monkeypatch):
    """A fresh monitor for the probe; its table holds the live probe socket while _connect is 'open'."""
    table: list = []
    mon = make_monitor(table)
    monkeypatch.setattr(net_probe, "monitor", mon)
    monkeypatch.setattr(net_probe, "_resolve", lambda host, port: [GOOGLE])
    return mon, table


def test_parse_target():
    assert net_probe.parse_target("https://www.google.com") == ("www.google.com", 443)
    assert net_probe.parse_target("http://example.com:8080/x") == ("example.com", 8080)
    assert net_probe.parse_target("example.com") == ("example.com", 443)
    with pytest.raises(ValueError):
        net_probe.parse_target("ftp://example.com")


def test_probe_success_is_reachable_labelled_probe_and_audited(probe_monitor, monkeypatch):
    mon, table = probe_monitor

    def fake_connect(ip, port, timeout_s):
        assert timeout_s <= net_probe.PROBE_TIMEOUT_S
        table.append(conn(remote=(ip, port)))                                # socket visible while open
        return SimpleNamespace(close=table.clear)

    monkeypatch.setattr(net_probe, "_connect", fake_connect)
    result = net_probe.run_probe("https://www.google.com")
    assert result.reachable is True and result.error is None and result.duration_ms >= 0
    assert mon.status(None).external_seen_since_start == 0 and mon.summary()["probe_since_start"] == 1
    probes = [r for r in read_audit_records() if r.name == "probe"]
    assert len(probes) == 1 and probes[0].detail["reachable"] is True and probes[0].detail["resolved"] == [GOOGLE]


def test_probe_timeout_is_unreachable_with_error_and_audited(probe_monitor, monkeypatch):
    def slow_connect(ip, port, timeout_s):
        raise socket.timeout("timed out")

    monkeypatch.setattr(net_probe, "_connect", slow_connect)
    result = net_probe.run_probe("https://www.google.com")
    assert result.reachable is False and "timed out" in result.error
    probes = [r for r in read_audit_records() if r.name == "probe"]
    assert len(probes) == 1 and probes[0].detail["reachable"] is False


def test_probe_dns_failure_and_bad_target_are_audited(probe_monitor, monkeypatch):
    def no_dns(host, port):
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(net_probe, "_resolve", no_dns)
    result = net_probe.run_probe("https://www.google.com")
    assert result.reachable is False and "DNS lookup for www.google.com failed" in result.error
    bad = net_probe.run_probe("ftp://example.com")
    assert bad.reachable is False and bad.error.startswith("invalid target")
    assert len([r for r in read_audit_records() if r.name == "probe"]) == 2


def test_probe_dns_hang_is_cut_at_timeout(probe_monitor, monkeypatch):
    monkeypatch.setattr(net_probe, "PROBE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(net_probe, "_resolve", lambda host, port: time.sleep(2) or [GOOGLE])
    start = time.monotonic()
    result = net_probe.run_probe("https://www.google.com")
    assert result.reachable is False and "timed out" in result.error and time.monotonic() - start < 1.5


# ---------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.main import app
    from backend.tools import knowledge

    for key, sub in (("WB_CHROMA_DIR", "chroma"), ("WB_KB_DIR", "kb"), ("WB_CACHE_DIR", "cache")):
        monkeypatch.setattr(settings, key, tmp_path / sub)
    knowledge.reset_client()
    with TestClient(app) as c:
        yield c
    knowledge.reset_client()


def test_status_endpoint_validates_and_carries_monitor_facts(client, monkeypatch):
    from backend import main

    monkeypatch.setattr(main.monitor, "connections_fn", lambda kind="inet": [conn(pid=None, remote=("20.9.9.9", 443),
                                                                                   status="SYN_SENT")])
    monkeypatch.setattr(main.firewall, "firewall_outbound_blocked", lambda: True)
    main.monitor.poll_once()
    resp = client.get(f"{API_PREFIX}/network/status")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("since", "attempts_since_start", "other_apps_since_start", "probe_since_start", "monitor_error",
                "platform_seen_since_start", "platform_attempts_since_start"):
        assert key in body                                                  # contract 1.0.1 + 1.0.2 fields are sent
    assert all("component" in c for c in body["connections"])
    status = NetworkStatus.model_validate(body)
    assert status.firewall_outbound_blocked is True and status.external_seen_since_start == 0
    assert any(c.remote == "20.9.9.9:443" and c.group == "attempt" and c.origin == "other_app"
               and c.component is None and c.first_seen is not None for c in status.connections)
    assert status.attempts_since_start >= 1 and status.since is not None
    assert status.platform_seen_since_start == 0 and status.platform_attempts_since_start == 0
    assert int(resp.headers["X-Net-Attempts-Since-Start"]) == status.attempts_since_start  # headers kept, same facts
    assert resp.headers["X-Contract-Version"] == "1.0.2"


_OLD_1_0_0 = {"checked_at": "2026-09-26T00:00:00Z", "external_count": 0, "external_seen_since_start": 0,
              "total_connections": 3, "firewall_outbound_blocked": None,
              "connections": [{"pid": 1, "process": "x", "local": "10.0.0.5:1", "remote": "20.0.0.1:443",
                               "status": "ESTABLISHED", "external": True}]}
_OLD_1_0_1 = {**_OLD_1_0_0, "since": "2026-09-26T00:00:00Z", "attempts_since_start": 1,
              "other_apps_since_start": 4, "probe_since_start": 0, "monitor_error": None,
              "connections": [{**_OLD_1_0_0["connections"][0], "group": "established", "origin": "ours",
                               "first_seen": "2026-09-26T00:00:01Z"}]}


def test_contract_1_0_payload_still_validates():
    """All newer fields are optional: a 1.0.0-shaped payload (e.g. an old mock) still parses."""
    status = NetworkStatus.model_validate(_OLD_1_0_0)
    assert status.since is None and status.monitor_error is None and status.connections[0].origin is None
    with pytest.raises(ValueError):
        NetworkStatus.model_validate({**_OLD_1_0_0, "connections": [{**_OLD_1_0_0["connections"][0],
                                                                     "origin": "other"}]})


def test_contract_1_0_1_payload_still_validates_and_1_0_2_fields_check_values():
    status = NetworkStatus.model_validate(_OLD_1_0_1)
    assert status.attempts_since_start == 1 and status.connections[0].origin == "ours"
    assert status.platform_seen_since_start is None and status.platform_attempts_since_start is None
    assert status.connections[0].component is None

    new = {**_OLD_1_0_1, "platform_seen_since_start": 5, "platform_attempts_since_start": 0,
           "connections": [{**_OLD_1_0_1["connections"][0], "component": "platform"}]}
    parsed = NetworkStatus.model_validate(new)
    assert parsed.platform_seen_since_start == 5 and parsed.connections[0].component == "platform"
    with pytest.raises(ValueError):
        NetworkStatus.model_validate({**new, "connections": [{**new["connections"][0], "component": "docker"}]})


@pytest.mark.parametrize("outcome", ["success", "timeout"])
def test_probe_endpoint(client, probe_monitor, monkeypatch, outcome):
    def fake_connect(ip, port, timeout_s):
        if outcome == "timeout":
            raise socket.timeout("timed out")
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(net_probe, "_connect", fake_connect)
    resp = client.post(f"{API_PREFIX}/network/probe", json={})               # contract default target
    assert resp.status_code == 200
    result = ProbeResult.model_validate(resp.json())
    assert result.target == "https://www.google.com"
    assert result.reachable is (outcome == "success")
    assert (result.error is None) is (outcome == "success")
    audit = client.get(f"{API_PREFIX}/audit", params={"limit": 20}).json()
    assert any(r["name"] == "probe" and r["kind"] == "network" for r in audit)
