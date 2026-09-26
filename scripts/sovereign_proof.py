"""
One-command evidence collector for the sovereign network proof (Track A).

Run from the repo root while the backend is running:
    python scripts/sovereign_proof.py                  # full run: probes + 3 guided scenarios
    python scripts/sovereign_proof.py --skip-scenarios # quick check: probes + counters only
    python scripts/sovereign_proof.py --no-screenshot

It talks ONLY to the backend at 127.0.0.1 (host/port from backend/settings.py) and never
opens any other connection itself. The only other thing it runs is the local command
`netsh wlan show interfaces` to record the Wi-Fi state. It does not touch the firewall.

Output: docs/proof/<YYYY-MM-DD_HHMM>/ with report.md, raw.json, summary.png,
screenshot.png and the downloaded artifacts. Exit code 0 = PASS, 1 = FAIL, 2 = backend down.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.settings import settings  # noqa: E402
from shared.contracts import (  # noqa: E402
    API_PREFIX,
    TERMINAL_STATUSES,
    AuditRecord,
    NetworkStatus,
    ProbeResult,
    Scenario,
    TaskCreate,
    TaskMode,
    TaskState,
    TaskStatus,
)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
TASK_TIMEOUT_S = 20 * 60
POLL_INTERVAL_S = 2.0
HTTP_TIMEOUT_S = 60.0
AUDIT_LIMIT = 1000
PROBE_TARGETS: list[Optional[str]] = [None, "https://8.8.8.8"]  # None = backend default target
FIREWALL_WARNING = "FIREWALL NOT BLOCKED - this is NOT a sovereign run"

REPORT_PDF = _REPO_ROOT / "demo" / "inputs" / "scenario_a_report_1.pdf"
PID_PNG = _REPO_ROOT / "demo" / "inputs" / "scenario_c_pid_generated.png"

INSPECTION_MESSAGE = "Draft an approval note from this scanned inspection report, citing the relevant SOPs."
CODE_CALC_MESSAGE = (
    "Write a function for pipe wall thickness from design pressure, outside diameter and "
    "allowable stress, t = P*D / (2*S), with tests. Use P = 10 MPa, D = 200 mm, S = 138 MPa "
    "and print the calculation steps."
)
PID_MESSAGE = "Extract every equipment and instrument tag from this P&ID drawing into an Excel tag list."

COUNTER_HELP: list[tuple[str, str]] = [
    ("external_count", "Core external connections open right now (backend + its children, Ollama, UI)."),
    ("external_seen_since_start", "Unique core external connections since the backend started. "
                                  "The headline number: must be 0."),
    ("attempts_since_start", "Unique outbound attempts that never connected (SYN_SENT). Not leaks."),
    ("platform_seen_since_start", "Docker Desktop / WSL host services that connected out. Not workbench "
                                  "code; counted apart from core."),
    ("platform_attempts_since_start", "Docker Desktop / WSL attempts that never connected "
                                      "(also inside attempts_since_start)."),
    ("other_apps_since_start", "Other programs on this laptop (browser, updates). Info only."),
    ("probe_since_start", "Connections made on purpose by /network/probe (the 'try to reach the "
                          "internet' button)."),
]


# ---------------------------------------------------------------- data
@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    reason: str = ""  # short failure reason shown in the FAIL list


@dataclass
class ScenarioRun:
    scenario: str
    task_id: Optional[str] = None
    status: str = "not started"
    elapsed_s: Optional[float] = None
    artifacts: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class Evidence:
    started_at: datetime
    computer: str
    wifi: str
    base_url: str
    status_before: Optional[dict] = None
    status_after: Optional[dict] = None
    probes: list[dict] = field(default_factory=list)
    scenarios: list[ScenarioRun] = field(default_factory=list)
    scenarios_skipped: bool = False
    audit: list[dict] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    finished_at: Optional[datetime] = None


# ---------------------------------------------------------------- backend client
def backend_url() -> str:
    return f"http://{settings.WB_API_HOST}:{settings.WB_API_PORT}"


def assert_loopback(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS and not host.startswith("127."):
        raise SystemExit(f"Refusing to run: backend URL {url!r} is not loopback (sovereign rule).")


class Api:
    """Thin httpx wrapper that only talks to the loopback backend and keeps every raw response."""

    def __init__(self, base_url: str, transport: Optional[httpx.BaseTransport] = None) -> None:
        assert_loopback(base_url)
        self.client = httpx.Client(base_url=base_url, timeout=HTTP_TIMEOUT_S, transport=transport,
                                   trust_env=False)  # never pick up a system HTTP proxy
        self.raw: list[dict[str, Any]] = []

    def close(self) -> None:
        self.client.close()

    def _record(self, label: str, resp: httpx.Response) -> None:
        try:
            body: Any = resp.json()
        except ValueError:
            body = f"<{len(resp.content)} bytes>"
        self.raw.append({"label": label, "method": resp.request.method, "path": resp.request.url.path,
                         "status_code": resp.status_code, "body": body})

    def get_json(self, label: str, path: str, **params: Any) -> Any:
        resp = self.client.get(path, params=params or None)
        self._record(label, resp)
        resp.raise_for_status()
        return resp.json()

    def post_json(self, label: str, path: str, payload: dict) -> Any:
        resp = self.client.post(path, json=payload)
        self._record(label, resp)
        resp.raise_for_status()
        return resp.json()

    def upload(self, label: str, path: Path) -> str:
        with path.open("rb") as fh:
            resp = self.client.post(f"{API_PREFIX}/files", files={"file": (path.name, fh)})
        self._record(label, resp)
        resp.raise_for_status()
        return resp.json()["file_id"]

    def download(self, url_path: str, dest: Path) -> None:
        resp = self.client.get(url_path)
        self.raw.append({"label": f"download {dest.name}", "method": "GET", "path": url_path,
                         "status_code": resp.status_code, "body": f"<{len(resp.content)} bytes>"})
        resp.raise_for_status()
        dest.write_bytes(resp.content)


# ---------------------------------------------------------------- local facts
def read_wifi_state(run: Callable[..., Any] = subprocess.run) -> str:
    """Wi-Fi state from `netsh wlan show interfaces` (local command, no network). "unknown" on error."""
    try:
        proc = run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=10)
    except Exception:
        return "unknown"
    return parse_wifi_state(proc.stdout or "") if proc.returncode == 0 else "unknown"


def parse_wifi_state(output: str) -> str:
    state, ssid = None, None
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "state" and state is None:
            state = value
        elif key == "ssid" and ssid is None:
            ssid = value
    if state is None:
        return "unknown"
    return f"{state} (SSID {ssid})" if ssid and state.lower() == "connected" else state


def computer_name() -> str:
    return platform.node() or "unknown"


# ---------------------------------------------------------------- steps
def check_backend(api: Api) -> Optional[dict]:
    try:
        return api.get_json("health", f"{API_PREFIX}/health")
    except (httpx.HTTPError, ValueError):
        return None


def network_status(api: Api, label: str) -> dict:
    data = api.get_json(label, f"{API_PREFIX}/network/status")
    NetworkStatus.model_validate(data)  # fail loudly if the backend breaks the contract
    return data


def run_probes(api: Api) -> list[dict]:
    results = []
    for target in PROBE_TARGETS:
        payload = {} if target is None else {"target": target}
        label = f"probe {target or 'default'}"
        try:
            data = api.post_json(label, f"{API_PREFIX}/network/probe", payload)
            ProbeResult.model_validate(data)
        except httpx.HTTPError as exc:
            data = {"target": target or "default", "reachable": None, "error": f"probe call failed: {exc}",
                    "duration_ms": 0}
        results.append(data)
    return results


def scenario_requests(report_id: str, pid_id: str) -> list[tuple[Scenario, TaskCreate]]:
    return [
        (Scenario.INSPECTION_NOTE, TaskCreate(message=INSPECTION_MESSAGE, file_ids=[report_id],
                                              mode=TaskMode.GUIDED, scenario=Scenario.INSPECTION_NOTE)),
        (Scenario.CODE_CALC, TaskCreate(message=CODE_CALC_MESSAGE, mode=TaskMode.GUIDED,
                                        scenario=Scenario.CODE_CALC)),
        (Scenario.PID_TAGS, TaskCreate(message=PID_MESSAGE, file_ids=[pid_id], mode=TaskMode.GUIDED,
                                       scenario=Scenario.PID_TAGS)),
    ]


def start_scenarios(api: Api) -> list[ScenarioRun]:
    report_id = api.upload("upload report", REPORT_PDF)
    pid_id = api.upload("upload pid", PID_PNG)
    runs = []
    for scenario, request in scenario_requests(report_id, pid_id):
        created = api.post_json(f"create {scenario.value}", f"{API_PREFIX}/tasks", request.model_dump(mode="json"))
        runs.append(ScenarioRun(scenario=scenario.value, task_id=created["task_id"], status=created["status"]))
    return runs


def wait_for_tasks(api: Api, runs: list[ScenarioRun], timeout_s: float = TASK_TIMEOUT_S,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, TaskState]:
    deadline = time.monotonic() + timeout_s
    final: dict[str, TaskState] = {}
    while True:
        for run in runs:
            if run.task_id is None or run.task_id in final:
                continue
            state = TaskState.model_validate(api.get_json(f"task {run.scenario}", f"{API_PREFIX}/tasks/{run.task_id}"))
            run.status = state.status.value
            if state.status in TERMINAL_STATUSES:
                final[run.task_id] = state
                print(f"  {run.scenario}: {run.status}")
        if all(r.task_id in final for r in runs if r.task_id) or time.monotonic() >= deadline:
            break
        sleep(POLL_INTERVAL_S)
    for run in runs:
        if run.task_id and run.task_id not in final:
            run.status, run.error = "timeout", f"not finished after {timeout_s / 60:.0f} min"
    return final


def collect_artifacts(api: Api, runs: list[ScenarioRun], final: dict[str, TaskState], out_dir: Path) -> None:
    for run in runs:
        state = final.get(run.task_id or "")
        if state is None:
            continue
        run.elapsed_s = state.elapsed_s
        run.error = state.error.message if state.error else run.error
        for artifact in state.artifacts:
            dest = out_dir / f"{run.scenario}__{Path(artifact.filename).name}"
            try:
                api.download(artifact.download_url, dest)
                run.artifacts.append(dest.name)
            except httpx.HTTPError as exc:
                run.artifacts.append(f"{artifact.filename} (download failed: {exc})")


def network_audit_since(api: Api, since: datetime) -> list[dict]:
    records = api.get_json("audit", f"{API_PREFIX}/audit", limit=AUDIT_LIMIT)
    kept = []
    for data in records:
        record = AuditRecord.model_validate(data)
        ts = record.ts if record.ts.tzinfo else record.ts.replace(tzinfo=timezone.utc)
        if record.kind == "network" and ts >= since:
            kept.append(data)
    return sorted(kept, key=lambda r: r["ts"])


# ---------------------------------------------------------------- verdict
def evaluate(ev: Evidence) -> list[Check]:
    before, after = ev.status_before or {}, ev.status_after or {}
    checks = [Check("Firewall outbound blocked", before.get("firewall_outbound_blocked") is True
                    and after.get("firewall_outbound_blocked") is True,
                    f"before={before.get('firewall_outbound_blocked')}, after={after.get('firewall_outbound_blocked')}",
                    "firewall not blocked")]
    for probe in ev.probes:
        target = probe.get("target", "?")
        checks.append(Check(f"Probe {target} unreachable", probe.get("reachable") is False,
                            f"reachable={probe.get('reachable')}, error={probe.get('error')}",
                            f"probe {target} reachable={probe.get('reachable')}"))
    if len(ev.probes) < len(PROBE_TARGETS):
        checks.append(Check("Both probes ran", False, f"{len(ev.probes)} of {len(PROBE_TARGETS)}", "probes missing"))
    if ev.scenarios_skipped:
        checks.append(Check("3 guided scenarios succeeded", False, "skipped (--skip-scenarios)",
                            "scenarios skipped (--skip-scenarios): quick check only"))
    else:
        for run in ev.scenarios:
            checks.append(Check(f"Scenario {run.scenario} succeeded", run.status == TaskStatus.SUCCEEDED.value,
                                f"status={run.status}", f"task {run.scenario} {run.status}"))
        if len(ev.scenarios) < 3:
            checks.append(Check("3 guided scenarios ran", False, f"{len(ev.scenarios)} of 3", "scenarios missing"))
    for label, status in (("before", before), ("after", after)):
        count = status.get("external_seen_since_start")
        checks.append(Check(f"Core external_seen_since_start = 0 ({label})", count == 0,
                            f"{count}", f"core external_seen_since_start {label} = {count}"))
    return checks


def verdict(checks: list[Check]) -> tuple[bool, list[str]]:
    reasons = [c.reason for c in checks if not c.ok]
    return not reasons, reasons


# ---------------------------------------------------------------- report.md
def _fmt_time(dt: Optional[datetime]) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if dt else "-"


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(ev: Evidence, checks: list[Check]) -> str:
    passed, reasons = verdict(checks)
    before, after = ev.status_before or {}, ev.status_after or {}
    lines: list[str] = []
    if before.get("firewall_outbound_blocked") is not True:
        lines += [f"> **{FIREWALL_WARNING}**", ""]
    lines += [f"# Sovereign network proof: **{'PASS' if passed else 'FAIL'}**", ""]
    if reasons:
        lines += ["**Reasons for FAIL:**", ""] + [f"- {r}" for r in reasons] + [""]
    lines += ["| Item | Value |", "|---|---|",
              f"| Started | {_fmt_time(ev.started_at)} |", f"| Finished | {_fmt_time(ev.finished_at)} |",
              f"| Computer | {_md_cell(ev.computer)} |", f"| Wi-Fi | {_md_cell(ev.wifi)} |",
              f"| Backend | {ev.base_url} |",
              f"| Firewall outbound blocked | before {before.get('firewall_outbound_blocked')}, "
              f"after {after.get('firewall_outbound_blocked')} |", ""]
    lines += ["## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {_md_cell(c.name)} | {'PASS' if c.ok else 'FAIL'} | {_md_cell(c.detail)} |" for c in checks]
    lines += ["", "## Probes (the backend tries to reach the internet on purpose)", "",
              "| Target | Reachable | Error | Duration ms |", "|---|---|---|---|"]
    lines += [f"| {_md_cell(p.get('target'))} | {p.get('reachable')} | {_md_cell(p.get('error') or '-')} "
              f"| {p.get('duration_ms')} |" for p in ev.probes]
    lines += ["", "## Guided scenarios", ""]
    if ev.scenarios_skipped:
        lines += ["Skipped (`--skip-scenarios`)."]
    else:
        lines += ["| Scenario | Task | Status | Time (s) | Artifacts | Error |", "|---|---|---|---|---|---|"]
        for r in ev.scenarios:
            secs = f"{r.elapsed_s:.1f}" if r.elapsed_s is not None else "-"
            lines.append(f"| {r.scenario} | {r.task_id or '-'} | {r.status} | {secs} | "
                         f"{_md_cell(', '.join(r.artifacts) or '-')} | {_md_cell(r.error or '-')} |")
    lines += ["", "## Network counters (before / after)", "", "| Counter | Before | After | Meaning |",
              "|---|---|---|---|"]
    lines += [f"| {name} | {before.get(name, '-')} | {after.get(name, '-')} | {help_text} |"
              for name, help_text in COUNTER_HELP]
    lines += [f"| monitor_error | {_md_cell(before.get('monitor_error') or '-')} | "
              f"{_md_cell(after.get('monitor_error') or '-')} | Last psutil error; '-' = monitor healthy. |", ""]
    lines += ["Only the **core** counters decide the proof. Platform (Docker Desktop / WSL), other apps, "
              "attempts and probe connections are shown for transparency but are not workbench traffic.", ""]
    lines += render_audit(ev.audit)
    lines += ["", "Raw API responses: `raw.json`. Evidence card: `summary.png`. Screen: `screenshot.png`.", ""]
    return "\n".join(lines)


def render_audit(records: list[dict]) -> list[str]:
    lines = ["## Network audit records since the run started", ""]
    if not records:
        return lines + ["None."]
    lines += ["| Time | Name | Target | OK | Detail |", "|---|---|---|---|---|"]
    for r in records:
        detail = json.dumps(r.get("detail", {}), default=str)
        detail = detail if len(detail) <= 200 else detail[:197] + "..."
        lines.append(f"| {r.get('ts')} | {r.get('name')} | {_md_cell(r.get('target') or '-')} | {r.get('ok')} "
                     f"| `{_md_cell(detail)}` |")
    return lines


# ---------------------------------------------------------------- summary.png
_FONT_CANDIDATES = {
    "bold": [Path(r"C:\Windows\Fonts\segoeuib.ttf"), Path(r"C:\Windows\Fonts\consolab.ttf")],
    "regular": [Path(r"C:\Windows\Fonts\segoeui.ttf"), Path(r"C:\Windows\Fonts\consola.ttf")],
}


def _font(size: int, weight: str = "regular"):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES[weight]:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def card_lines(ev: Evidence) -> list[tuple[str, Optional[bool]]]:
    """(text, ok) rows for the evidence card; ok None = neutral."""
    before, after = ev.status_before or {}, ev.status_after or {}
    fw = before.get("firewall_outbound_blocked")
    rows: list[tuple[str, Optional[bool]]] = [
        (f"Computer: {ev.computer}", None), (f"Wi-Fi: {ev.wifi}", None),
        (f"Firewall outbound blocked: {fw}", fw is True),
    ]
    rows += [(f"Probe {p.get('target')}: reachable={p.get('reachable')}", p.get("reachable") is False)
             for p in ev.probes]
    if ev.scenarios_skipped:
        rows.append(("Scenarios: skipped (quick check)", False))
    for r in ev.scenarios:
        secs = f" in {r.elapsed_s:.0f} s" if r.elapsed_s is not None else ""
        rows.append((f"Scenario {r.scenario}: {r.status}{secs}", r.status == TaskStatus.SUCCEEDED.value))
    for label, status in (("before", before), ("after", after)):
        count = status.get("external_seen_since_start")
        rows.append((f"Core external connections ({label}): {count}", count == 0))
    return rows


def draw_summary(ev: Evidence, passed: bool, dest: Path) -> None:
    from PIL import Image, ImageDraw

    rows = card_lines(ev)
    width, height = 1200, 330 + 46 * len(rows)
    img = Image.new("RGB", (width, height), "#f7f7f5")
    draw = ImageDraw.Draw(img)
    accent = "#1b7f3b" if passed else "#b3261e"
    draw.rectangle([0, 0, width, 190], fill=accent)
    draw.text((50, 22), "PASS" if passed else "FAIL", fill="white", font=_font(110, "bold"))
    draw.text((420, 45), "Sovereign network proof", fill="white", font=_font(40, "bold"))
    draw.text((420, 105), _fmt_time(ev.started_at), fill="white", font=_font(30))
    y = 220
    if (ev.status_before or {}).get("firewall_outbound_blocked") is not True:
        draw.text((50, y), FIREWALL_WARNING, fill="#b3261e", font=_font(30, "bold"))
    y += 60
    font = _font(28)
    colors = {True: "#1b7f3b", False: "#b3261e", None: "#555555"}
    marks = {True: "OK ", False: "X  ", None: "   "}
    for text, ok in rows:
        draw.text((50, y), marks[ok], fill=colors[ok], font=_font(28, "bold"))
        draw.text((110, y), text, fill="#222222" if ok is None else colors[ok], font=font)
        y += 46
    img.save(dest)


# ---------------------------------------------------------------- screenshot
def take_screenshot(dest: Path) -> str:
    try:
        from PIL import ImageGrab

        ImageGrab.grab(all_screens=True).save(dest)
        return f"saved {dest.name}"
    except Exception as exc:  # no display, locked screen, non-Windows
        return f"screenshot skipped: {exc}"


# ---------------------------------------------------------------- orchestration
def make_out_dir(root: Path, now: datetime) -> Path:
    base = root / now.strftime("%Y-%m-%d_%H%M")
    out, n = base, 2
    while out.exists():
        out, n = base.with_name(f"{base.name}_{n}"), n + 1
    out.mkdir(parents=True)
    return out


def run_scenarios(api: Api, ev: Evidence, out_dir: Path, sleep: Callable[[float], None]) -> None:
    print("Running the 3 guided scenarios (this can take several minutes)...")
    try:
        ev.scenarios = start_scenarios(api)
        final = wait_for_tasks(api, ev.scenarios, sleep=sleep)
        collect_artifacts(api, ev.scenarios, final, out_dir)
    except (httpx.HTTPError, OSError) as exc:
        print(f"  scenario run error: {exc}")
        if not ev.scenarios:
            ev.scenarios = [ScenarioRun(scenario=s.value, status="error", error=str(exc)) for s in Scenario]


def write_outputs(ev: Evidence, checks: list[Check], out_dir: Path, screenshot: bool) -> bool:
    passed, _ = verdict(checks)
    (out_dir / "report.md").write_text(render_report(ev, checks), encoding="utf-8")
    (out_dir / "raw.json").write_text(json.dumps(ev.raw, indent=2, default=str), encoding="utf-8")
    draw_summary(ev, passed, out_dir / "summary.png")
    if screenshot:
        print(f"Screenshot: {take_screenshot(out_dir / 'screenshot.png')}")
    return passed


def collect(api: Api, ev: Evidence, out_dir: Path, skip_scenarios: bool,
            sleep: Callable[[float], None] = time.sleep) -> None:
    ev.status_before = network_status(api, "status before")
    if ev.status_before.get("firewall_outbound_blocked") is not True:
        print(f"WARNING: {FIREWALL_WARNING} (firewall_outbound_blocked="
              f"{ev.status_before.get('firewall_outbound_blocked')}). Continuing to collect evidence.")
    print("Probing the internet through the backend...")
    ev.probes = run_probes(api)
    ev.scenarios_skipped = skip_scenarios
    if not skip_scenarios:
        run_scenarios(api, ev, out_dir, sleep)
    ev.status_after = network_status(api, "status after")
    ev.audit = network_audit_since(api, ev.started_at)


def run(skip_scenarios: bool = False, screenshot: bool = True, out_root: Optional[Path] = None,
        transport: Optional[httpx.BaseTransport] = None, wifi_reader: Callable[[], str] = read_wifi_state,
        sleep: Callable[[float], None] = time.sleep) -> int:
    """Collect the evidence. Returns 0 = PASS, 1 = FAIL, 2 = backend not reachable."""
    base = backend_url()
    api = Api(base, transport=transport)
    try:
        health = check_backend(api)
        if health is None:
            print(f"Backend is not reachable at {base}/api/health. Start it first:\n"
                  f"  uvicorn backend.main:app --host {settings.WB_API_HOST} --port {settings.WB_API_PORT}")
            return 2
        started = datetime.now(timezone.utc)
        ev = Evidence(started_at=started, computer=computer_name(), wifi=wifi_reader(), base_url=base)
        out_dir = make_out_dir(out_root or _REPO_ROOT / "docs" / "proof", started.astimezone())
        collect(api, ev, out_dir, skip_scenarios, sleep)
        ev.finished_at = datetime.now(timezone.utc)
        ev.raw = {"health": health, "calls": api.raw}
        checks = evaluate(ev)
        passed = write_outputs(ev, checks, out_dir, screenshot)
    finally:
        api.close()
    _, reasons = verdict(checks)
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"Evidence folder: {out_dir}")
    return 0 if passed else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect evidence for the sovereign network proof.")
    parser.add_argument("--skip-scenarios", action="store_true", help="only probes + counters (quick check)")
    parser.add_argument("--no-screenshot", action="store_true", help="do not grab a screenshot at the end")
    args = parser.parse_args(argv)
    return run(skip_scenarios=args.skip_scenarios, screenshot=not args.no_screenshot)


if __name__ == "__main__":
    sys.exit(main())
