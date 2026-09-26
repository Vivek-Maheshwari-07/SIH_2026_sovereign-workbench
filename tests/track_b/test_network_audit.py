"""B7 tests: network page (ui/components/network_panel.py) and audit page (ui/components/audit_page.py)."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fake_client import T0, TASK_ID, FakeClient, net_status, ok
from streamlit.testing.v1 import AppTest

from shared.contracts import AuditRecord, Connection, ErrorInfo, NetworkStatus, ProbeResult
from ui import api_client
from ui.api_client import ApiResult
from ui.components import audit_page, network_panel

APP = str(Path(__file__).resolve().parents[2] / "ui" / "app.py")


def connection(process: str, remote: str, component=None, origin="ours", group="established") -> Connection:
    return Connection(pid=42, process=process, local="10.0.0.5:5000", remote=remote, status="ESTABLISHED",
                      external=True, group=group, origin=origin, component=component, first_seen=T0)


CORE = connection("ollama.exe [ours: core]", "34.36.133.15:443", "core")
PLATFORM = connection("com.docker.backend.exe [ours: platform]", "52.1.2.3:443", "platform")
OTHER = connection("chrome.exe [other app]", "142.250.1.1:443", None, "other_app")


def status(seen=0, now=0, platform=0, conns=None, firewall=True) -> NetworkStatus:
    base = net_status(seen=seen, now=now, firewall=firewall)
    return base.model_copy(update={"platform_seen_since_start": platform, "attempts_since_start": 3,
                                   "other_apps_since_start": 9, "probe_since_start": 1, "since": T0,
                                   "connections": conns or []})


def open_view(monkeypatch, fake: FakeClient, view: str) -> AppTest:
    monkeypatch.setattr(api_client, "get_client", lambda: fake)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.segmented_control(key="view").set_value(view).run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def html(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


# ---------------------------------------------------------------- network: pure
def test_headline_green_at_zero():
    text = network_panel.headline_html(status(seen=0, firewall=True))
    assert 'class="val wb-num c-ok">0<' in text and "Blocked" in text
    assert "No workbench process has reached the internet." in text


def test_headline_red_above_zero():
    text = network_panel.headline_html(status(seen=2, now=1, firewall=False))
    assert 'class="val wb-num c-alarm">2<' in text and "Open" in text and "firewall_block.ps1" in text
    assert "red rows below" in text


def test_platform_shown_separately_never_in_headline():
    st_ = status(seen=0, platform=4, conns=[PLATFORM])
    head = network_panel.headline_html(st_)
    assert 'c-ok">0<' in head                                         # headline stays 0
    readings = {tag: (value, css) for tag, _, value, css, _ in network_panel.readings(st_)}
    assert readings["NET-002"] == (4, "c-warn")                       # platform counted apart, amber
    text = network_panel.readings_html(st_)
    assert "Docker Desktop / WSL / Ollama tray app" in text and "never in NET-001" in text


def test_connection_filter_and_row_colours():
    conns = [CORE, PLATFORM, OTHER]
    assert network_panel.filter_connections(conns, "Core") == [CORE]
    assert network_panel.filter_connections(conns, "Platform") == [PLATFORM]
    assert network_panel.filter_connections(conns, "All") == conns
    assert network_panel.filter_connections(conns, None) == conns     # deselected control = all
    text = network_panel.connections_html(conns)
    assert '<tr class="core"><td>ollama.exe' in text                  # "[ours: core]" suffix dropped
    assert '<tr class="platform">' in text and '<tr class="other">' in text


def test_probe_blocked_vs_reachable():
    blocked = network_panel.probe_html(ProbeResult(target="https://www.google.com", reachable=False,
                                                   error="timed out", duration_ms=3002))
    assert "c-ok" in blocked and "Blocked - not reachable (3002 ms)" in blocked
    reachable = network_panel.probe_html(ProbeResult(target="https://www.google.com", reachable=True,
                                                     duration_ms=41))
    assert "c-alarm" in reachable and "Reachable (41 ms)" in reachable and "Wi-Fi off" in reachable


# ---------------------------------------------------------------- network: in the app
def test_network_page_green(monkeypatch):
    fake = FakeClient()
    fake.network_result = ok(status(seen=0, platform=2, conns=[PLATFORM, OTHER]))
    at = open_view(monkeypatch, fake, "Network")
    text = html(at)
    assert 'class="val wb-num c-ok">0<' in text and "Core external connections since" in text
    assert "NET-002" in text and "Current connections" in text and '<tr class="platform">' in text
    assert "No data is sent." in "\n".join(c.value for c in at.caption)


def test_network_page_red_and_core_filter(monkeypatch):
    fake = FakeClient()
    fake.network_result = ok(status(seen=1, now=1, conns=[CORE, PLATFORM, OTHER]))
    at = open_view(monkeypatch, fake, "Network")
    assert 'class="val wb-num c-alarm">1<' in html(at)
    at.segmented_control(key="conn_filter").set_value("Core").run()
    table = next(m.value for m in at.markdown if '<table class="wb-conn"' in m.value)
    assert "ollama.exe" in table and "com.docker" not in table and "chrome.exe" not in table


def test_probe_button_shows_blocked_result(monkeypatch):
    fake = FakeClient()
    at = open_view(monkeypatch, fake, "Network")
    at.button(key="probe").click().run()
    assert fake.probe_calls == 1
    assert "Blocked - not reachable (3002 ms)" in html(at)


def test_probe_button_shows_reachable_result(monkeypatch):
    fake = FakeClient()
    fake.probe_result = ok(ProbeResult(target="https://www.google.com", reachable=True, duration_ms=41))
    at = open_view(monkeypatch, fake, "Network")
    at.button(key="probe").click().run()
    assert "Reachable (41 ms)" in html(at) and "wb-probe alarm" in html(at)


def test_probe_error_is_friendly(monkeypatch):
    fake = FakeClient()
    fake.probe_result = ApiResult(error=ErrorInfo(code="INTERNAL", message="boom"), failure="api", status_code=500)
    at = open_view(monkeypatch, fake, "Network")
    at.button(key="probe").click().run()
    assert any("The probe could not run: boom. Something failed inside the backend" in e.value for e in at.error)


def test_network_readings_unavailable_is_friendly(monkeypatch):
    fake = FakeClient()
    fake.network_result = ApiResult(error=ErrorInfo(code="INTERNAL", message="x", retryable=True),
                                    failure="timeout")
    at = open_view(monkeypatch, fake, "Network")
    assert any("Network readings are not available" in w.value and "Wait a moment" in w.value for w in at.warning)


# ---------------------------------------------------------------- audit
def rec(minute: int, kind: str, name: str, ok_: bool = True, task: str | None = None, **detail) -> AuditRecord:
    return AuditRecord(ts=T0 + timedelta(minutes=minute), task_id=task, kind=kind, name=name, ok=ok_,
                       target="http://127.0.0.1:11434" if kind == "llm" else None, detail=detail)


RECORDS = [
    rec(1, "system", "task_created", task=TASK_ID, mode="guided", scenario="code_calc", files=0),
    rec(2, "llm", "qwen2.5-coder:3b", task=TASK_ID, purpose="write code", tokens_out=437),
    rec(3, "tool", "sandbox", task=TASK_ID, exit_code=0, tests_passed=4, tests_failed=0),
    rec(4, "network", "external_connection", ok_=False, process="ollama.exe", label="ours: core",
        component="core", origin="ours", status="ESTABLISHED"),
    rec(5, "network", "platform_connection", ok_=False, process="com.docker.backend.exe", label="ours: platform",
        component="platform", origin="ours", status="ESTABLISHED"),
    rec(6, "network", "external_connection", ok_=True, process="chrome.exe", label="other app",
        component=None, origin="other_app", status="ESTABLISHED"),
    rec(7, "system", "task_finished", ok_=False, task=TASK_ID, status="failed", error_code="AGENT_TIMEOUT"),
]


def test_audit_filters_and_order():
    shown = audit_page.filter_records(RECORDS, "All", hide_other_apps=True, failures_only=False)
    assert [r.ts for r in shown] == sorted((r.ts for r in shown), reverse=True)     # newest first
    assert not any(audit_page.is_other_app(r) for r in shown) and len(shown) == 6
    assert [r.name for r in audit_page.filter_records(RECORDS, "llm", True, False)] == ["qwen2.5-coder:3b"]
    failed = audit_page.filter_records(RECORDS, "All", False, True)
    assert {r.name for r in failed} == {"external_connection", "platform_connection", "task_finished"}


def test_audit_leak_highlight_and_details():
    text = audit_page.records_html(RECORDS)
    assert text.count('<tr class="leak">') == 1 and '<tr class="platform">' in text and '<tr class="fail">' in text
    assert audit_page.key_detail(RECORDS[2]) == "exit code 0, tests 4 passed, 0 failed"
    assert audit_page.key_detail(RECORDS[1]) == "write code, 437 tokens"
    assert "ollama.exe (ours: core" in audit_page.key_detail(RECORDS[3])


def test_audit_page_in_app(monkeypatch):
    fake = FakeClient()
    fake.audit_records = RECORDS
    at = open_view(monkeypatch, fake, "Audit")
    text = html(at)
    assert '<tr class="leak">' in text and "1 core leak record(s) highlighted" in text
    assert "chrome.exe" not in text                                   # other apps hidden by default
    at.selectbox(key="audit_kind").set_value("system").run()
    table = next(m.value for m in at.markdown if '<table class="wb-conn wb-audit"' in m.value)
    assert "task_finished" in table and "qwen2.5" not in table
    at.text_input(key="audit_task").input(TASK_ID).run()
    assert fake.audit_calls[-1] == (TASK_ID, 500)                     # task id + default limit reach the API
    at.selectbox(key="audit_limit").set_value(50).run()
    assert fake.audit_calls[-1] == (TASK_ID, 50)


def test_audit_error_is_friendly(monkeypatch):
    fake = FakeClient()
    fake.audit = lambda task_id=None, limit=100: ApiResult(
        error=ErrorInfo(code="INTERNAL", message="refused", retryable=True), failure="unreachable")
    at = open_view(monkeypatch, fake, "Audit")
    assert any("The audit log could not be loaded" in e.value and "Start it with" in e.value for e in at.error)
