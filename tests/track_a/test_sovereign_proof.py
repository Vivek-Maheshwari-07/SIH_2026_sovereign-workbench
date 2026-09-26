"""Tests for scripts/sovereign_proof.py. All HTTP goes to an in-process fake backend (httpx.MockTransport)."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("sovereign_proof", _REPO_ROOT / "scripts" / "sovereign_proof.py")
sp = importlib.util.module_from_spec(_spec)
sys.modules["sovereign_proof"] = sp  # dataclasses look the module up by name
_spec.loader.exec_module(sp)


# ---------------------------------------------------------------- fake backend
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status(blocked=True, core_seen=0) -> dict:
    return {"checked_at": _now(), "external_count": 0, "external_seen_since_start": core_seen,
            "total_connections": 3, "firewall_outbound_blocked": blocked, "connections": [],
            "since": _now(), "attempts_since_start": 2, "other_apps_since_start": 5, "probe_since_start": 0,
            "monitor_error": None, "platform_seen_since_start": 1, "platform_attempts_since_start": 1}


class FakeBackend:
    """Answers the contract endpoints the script uses; records every request URL."""

    def __init__(self, blocked=True, reachable=(False, False), failed_scenario=None, core_seen=(0, 0)):
        self.blocked, self.reachable, self.failed_scenario = blocked, list(reachable), failed_scenario
        self.core_seen = list(core_seen)
        self.urls: list[httpx.URL] = []
        self.tasks: dict[str, dict] = {}
        self.polls: dict[str, int] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(request.url)
        path, method = request.url.path, request.method
        if path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/network/status":
            return httpx.Response(200, json=_status(self.blocked, self.core_seen.pop(0)))
        if path == "/api/network/probe":
            body = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"target": body.get("target", "https://www.google.com"),
                                             "reachable": self.reachable.pop(0), "error": "blocked",
                                             "duration_ms": 12})
        if path == "/api/files":
            return httpx.Response(200, json={"file_id": f"f_{len(self.urls):012x}"})
        if path == "/api/tasks" and method == "POST":
            return self._create(json.loads(request.content))
        if path.startswith("/api/tasks/"):
            return self._get_task(path.rsplit("/", 1)[1])
        if path.startswith("/api/artifacts/"):
            return httpx.Response(200, content=b"artifact-bytes")
        if path == "/api/audit":
            old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            return httpx.Response(200, json=[
                {"ts": _now(), "kind": "network", "name": "probe", "target": "https://8.8.8.8",
                 "detail": {"reachable": False}},
                {"ts": _now(), "kind": "llm", "name": "general", "target": "http://127.0.0.1:11434"},
                {"ts": old, "kind": "network", "name": "probe", "target": "old"},
            ])
        return httpx.Response(404, json={"error": {"code": "BAD_REQUEST", "message": path}})

    def _create(self, body: dict) -> httpx.Response:
        task_id = f"t_{len(self.tasks):012x}"
        self.tasks[task_id] = body
        return httpx.Response(202, json={"task_id": task_id, "status": "queued"})

    def _get_task(self, task_id: str) -> httpx.Response:
        body = self.tasks[task_id]
        self.polls[task_id] = self.polls.get(task_id, 0) + 1
        done = self.polls[task_id] >= 2
        status = "running"
        if done:
            status = "failed" if body["scenario"] == self.failed_scenario else "succeeded"
        artifacts = []
        if status == "succeeded":
            ext = {"inspection_note": "docx", "code_calc": "py", "pid_tags": "xlsx"}[body["scenario"]]
            artifacts = [{"artifact_id": "a_000000000001", "task_id": task_id, "filename": f"out.{ext}",
                          "kind": ext, "size_bytes": 14, "created_at": _now(),
                          "download_url": "/api/artifacts/a_000000000001"}]
        error = {"code": "INTERNAL", "message": "boom"} if status == "failed" else None
        return httpx.Response(200, json={
            "task_id": task_id, "status": status, "mode": "guided", "scenario": body["scenario"],
            "message": body["message"], "file_ids": body["file_ids"], "artifacts": artifacts, "error": error,
            "created_at": _now(), "elapsed_s": 42.0 if done else None})


def _run(tmp_path: Path, backend: FakeBackend, skip_scenarios=False) -> tuple[int, Path]:
    code = sp.run(skip_scenarios=skip_scenarios, screenshot=False, out_root=tmp_path,
                  transport=backend.transport(), wifi_reader=lambda: "disconnected", sleep=lambda s: None)
    (out_dir,) = list(tmp_path.iterdir())
    return code, out_dir


# ---------------------------------------------------------------- end-to-end over the fake backend
def test_all_good_is_pass_and_writes_every_output(tmp_path, network_guard):
    backend = FakeBackend()
    code, out = _run(tmp_path, backend)
    assert code == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "**PASS**" in report and sp.FIREWALL_WARNING not in report
    assert "inspection_note__out.docx" in report and "external_seen_since_start" in report
    assert sorted(p.name for p in out.iterdir()) == [
        "code_calc__out.py", "inspection_note__out.docx", "pid_tags__out.xlsx", "raw.json", "report.md",
        "summary.png"]
    with Image.open(out / "summary.png") as img:
        assert img.size[0] == 1200
    raw = json.loads((out / "raw.json").read_text(encoding="utf-8"))
    assert raw["health"]["status"] == "ok" and raw["calls"]
    # only the "network" audit record from this run is kept
    assert report.count("| probe |") == 1


def test_only_loopback_is_ever_called(tmp_path, network_guard):
    backend = FakeBackend()
    _run(tmp_path, backend)
    assert backend.urls and {u.host for u in backend.urls} == {"127.0.0.1"}


def test_firewall_not_blocked_is_fail_with_warning(tmp_path, network_guard):
    code, out = _run(tmp_path, FakeBackend(blocked=None))
    report = (out / "report.md").read_text(encoding="utf-8")
    assert code == 1
    assert report.startswith(f"> **{sp.FIREWALL_WARNING}**")
    assert "- firewall not blocked" in report


def test_reachable_probe_is_fail(tmp_path, network_guard):
    code, out = _run(tmp_path, FakeBackend(reachable=(False, True)))
    assert code == 1
    assert "probe https://8.8.8.8 reachable=True" in (out / "report.md").read_text(encoding="utf-8")


def test_failed_task_is_fail(tmp_path, network_guard):
    code, out = _run(tmp_path, FakeBackend(failed_scenario="code_calc"))
    assert code == 1
    assert "task code_calc failed" in (out / "report.md").read_text(encoding="utf-8")


def test_core_external_count_one_is_fail(tmp_path, network_guard):
    code, out = _run(tmp_path, FakeBackend(core_seen=(0, 1)))
    assert code == 1
    assert "core external_seen_since_start after = 1" in (out / "report.md").read_text(encoding="utf-8")


def test_skip_scenarios_runs_no_tasks(tmp_path, network_guard):
    backend = FakeBackend()
    code, out = _run(tmp_path, backend, skip_scenarios=True)
    assert code == 1 and not backend.tasks
    assert not any(u.path.startswith("/api/tasks") for u in backend.urls)
    assert "scenarios skipped" in (out / "report.md").read_text(encoding="utf-8")


def test_backend_down_stops_with_code_2(tmp_path, network_guard, capsys):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    code = sp.run(screenshot=False, out_root=tmp_path, transport=httpx.MockTransport(down),
                  wifi_reader=lambda: "x", sleep=lambda s: None)
    assert code == 2 and not list(tmp_path.iterdir())
    assert "not reachable" in capsys.readouterr().out


# ---------------------------------------------------------------- unit pieces
def _evidence(**overrides) -> "sp.Evidence":
    ev = sp.Evidence(started_at=datetime.now(timezone.utc), computer="PC", wifi="off", base_url="http://127.0.0.1:8000",
                     status_before=_status(), status_after=_status(),
                     probes=[{"target": "a", "reachable": False}, {"target": "b", "reachable": False}],
                     scenarios=[sp.ScenarioRun(s.value, status="succeeded") for s in sp.Scenario])
    for key, value in overrides.items():
        setattr(ev, key, value)
    return ev


def test_evaluate_all_good_passes():
    assert sp.verdict(sp.evaluate(_evidence())) == (True, [])


@pytest.mark.parametrize("overrides, reason", [
    ({"status_before": _status(blocked=False)}, "firewall not blocked"),
    ({"probes": [{"target": "a", "reachable": True}, {"target": "b", "reachable": False}]}, "probe a reachable=True"),
    ({"probes": [{"target": "a", "reachable": None}, {"target": "b", "reachable": False}]}, "probe a reachable=None"),
    ({"scenarios": [sp.ScenarioRun("pid_tags", status="failed")]}, "task pid_tags failed"),
    ({"status_before": _status(core_seen=1)}, "core external_seen_since_start before = 1"),
    ({"scenarios_skipped": True, "scenarios": []}, "scenarios skipped (--skip-scenarios): quick check only"),
])
def test_evaluate_failures(overrides, reason):
    passed, reasons = sp.verdict(sp.evaluate(_evidence(**overrides)))
    assert not passed and reason in reasons


def test_parse_wifi_state():
    out = "There is 1 interface on the system:\n    Name  : Wi-Fi\n    State : connected\n    SSID  : Lab\n"
    assert sp.parse_wifi_state(out) == "connected (SSID Lab)"
    assert sp.parse_wifi_state("    State : disconnected\n") == "disconnected"
    assert sp.parse_wifi_state("The Wireless AutoConfig Service (wlansvc) is not running.") == "unknown"


def test_read_wifi_state_unknown_on_error():
    def boom(*a, **k):
        raise FileNotFoundError("netsh")

    assert sp.read_wifi_state(boom) == "unknown"
    assert sp.read_wifi_state(lambda *a, **k: SimpleNamespace(returncode=1, stdout="")) == "unknown"


def test_api_refuses_non_loopback_url():
    with pytest.raises(SystemExit):
        sp.Api("http://8.8.8.8:8000")
    with pytest.raises(SystemExit):
        sp.assert_loopback("http://example.com")
    sp.assert_loopback("http://127.0.0.1:8000")


def test_make_out_dir_never_overwrites(tmp_path):
    now = datetime(2026, 9, 26, 14, 5)
    first, second = sp.make_out_dir(tmp_path, now), sp.make_out_dir(tmp_path, now)
    assert first.name == "2026-09-26_1405" and second.name == "2026-09-26_1405_2"


def test_take_screenshot_skips_with_note(tmp_path, monkeypatch):
    import PIL.ImageGrab

    def fail(**kwargs):
        raise OSError("no display")

    monkeypatch.setattr(PIL.ImageGrab, "grab", fail)
    assert sp.take_screenshot(tmp_path / "s.png").startswith("screenshot skipped")


def test_wait_for_tasks_times_out():
    backend = FakeBackend()
    api = sp.Api("http://127.0.0.1:8000", transport=backend.transport())
    backend.tasks["t_1"] = {"scenario": "code_calc", "message": "m", "file_ids": []}
    backend.polls["t_1"] = -100  # stays running
    runs = [sp.ScenarioRun("code_calc", task_id="t_1")]
    assert sp.wait_for_tasks(api, runs, timeout_s=0, sleep=lambda s: None) == {}
    assert runs[0].status == "timeout"
