"""
Soak test through the HTTP API only (Track B, ticket B9 / guide step 8.4).

    .venv\\Scripts\\python.exe scripts/e2e_run.py --runs 20
    .venv\\Scripts\\python.exe scripts/e2e_run.py --runs 1 --scenarios b
    .venv\\Scripts\\python.exe scripts/e2e_run.py --runs 3 --scenarios a,c --mode agent

Uses the exact prompts and demo files of the UI work orders (ui/scenarios.py) and the pass rules
in demo/expected.md. For every run and scenario it records status, time and the downloaded
artifacts (all non-empty), plus:
  A (inspection note): every expected finding with the expected severity, the exact cost figure,
                       and at least N of the expected SOP files cited, N as expected.md says
                       ("must cite at least 2"), all read from the .docx;
  B (code calc):       the printed result t = 7.246 mm in the final answer;
  C (P&ID tags):       all expected tags in the .xlsx (12/12), no invented tags (any tag outside the
                       expected list fails) and the right equipment type for every tag, as
                       expected.md says. Types are compared by equipment class, instruments also
                       by what they measure (see equipment_class).
NET-001 (core external connections since backend start) is read before and after.
Writes docs/soak/<date_time>/report.md + raw.json (+ the artifacts). Exit code 0 only if every
run passed. Ctrl+C cancels the running task, stops cleanly and still writes the report so far.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.contracts import (  # noqa: E402
    CONTRACT_VERSION,
    Artifact,
    Scenario,
    TaskCreate,
    TaskMode,
    TaskState,
    TaskStatus,
)
from ui.api_client import ApiClient  # noqa: E402
from ui.scenarios import SCENARIOS, DemoScenario  # noqa: E402

EXPECTED_MD = _REPO_ROOT / "demo" / "expected.md"
SOAK_DIR = _REPO_ROOT / "docs" / "soak"
POLL_S = 1.0
TASK_TIMEOUT_S = 900.0          # WB_AGENT_TIMEOUT_S (600) + queue time
B_RESULTS = ("7.246", "7.25")   # demo/expected.md: anything that rounds to 7.246 or 7.25 passes
EXIT_OK, EXIT_FAILED, EXIT_INTERRUPTED, EXIT_NO_BACKEND = 0, 1, 130, 2


# ---------------------------------------------------------------- data
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class RunResult:
    run: int
    scenario: str
    work_order: str
    task_id: Optional[str] = None
    status: str = "not started"
    seconds: float = 0.0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == TaskStatus.SUCCEEDED.value and bool(self.checks) and all(c.ok for c in self.checks)


# ---------------------------------------------------------------- scenario selection (from ui/scenarios.py)
def scenario_letter(scn: DemoScenario) -> str:
    return scn.work_order.split("-")[-1].lower()          # "WO-B" -> "b"


def select_scenarios(spec: str) -> list[DemoScenario]:
    wanted = [s.strip().lower() for s in spec.split(",") if s.strip()]
    by_letter = {scenario_letter(s): s for s in SCENARIOS}
    unknown = [w for w in wanted if w not in by_letter]
    if unknown:
        raise ValueError(f"unknown scenario(s) {unknown}; use {','.join(sorted(by_letter))}")
    return [by_letter[w] for w in wanted]


# ---------------------------------------------------------------- expected results (from demo/expected.md)
def expected_section(md: str, demo_file: str) -> str:
    """The '### ...' section whose heading names the demo file."""
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("### ") and demo_file in line:
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith(("### ", "## "))), len(lines))
            return "\n".join(lines[i:end])
    return ""


def expected_findings(section: str) -> list[tuple[str, str]]:
    rows = re.findall(r"^\|\s*\d+\s*\|\s*([^|]+?)\s*\|[^\n]*\|\s*\*\*(Low|Medium|High|Critical)\*\*\s*\|\s*$",
                      section, flags=re.M)
    return [(item.strip(), sev) for item, sev in rows]


def expected_cost(section: str) -> Optional[str]:
    m = re.search(r"Cost line: must be\*\*\s*`([^`]+)`", section)
    return m.group(1) if m else None


def expected_sops(section: str) -> list[str]:
    part = section.split("SOP references", 1)[-1] if "SOP references" in section else ""
    return sorted(set(re.findall(r"`([A-Za-z0-9_\-]+\.pdf)`", part)))


def expected_min_sops(section: str) -> int:
    """'must cite at least 2 of these files' -> 2 (the 'must' rule; 'should cite 3' is a quality note)."""
    m = re.search(r"must cite at least (\d+)", section)
    return int(m.group(1)) if m else 1


def expected_tags(section: str) -> list[str]:
    return re.findall(r"^\|\s*([A-Z]{1,4}-\d{2,4}[A-Z]?)\s*\|", section, flags=re.M)


def expected_tag_types(section: str) -> dict[str, str]:
    """Tag -> equipment type from the expected tag table, e.g. "V-201" -> "Vessel (separator)"."""
    rows = re.findall(r"^\|\s*([A-Z]{1,4}-\d{2,4}[A-Z]?)\s*\|\s*([^|]+?)\s*\|", section, flags=re.M)
    return {norm_tag(tag): kind for tag, kind in rows}


# Equipment classes for the loose type comparison. First match wins, so the more specific words come
# first ("pressure safety valve" is a valve, "vessel" is never a valve).
EQUIPMENT_CLASSES: list[tuple[str, tuple[str, ...]]] = [
    ("instrument", ("transmitter", "instrument", "indicator", "gauge", "sensor", "switch", "analyzer",
                    "analyser", "controller", "element")),
    ("valve", ("valve", "psv", "prv")),
    ("heat exchanger", ("exchanger", "cooler", "heater", "condenser", "reboiler")),
    ("pump", ("pump",)),
    ("tank", ("tank",)),
    ("vessel", ("vessel", "separator", "drum", "column", "reactor")),
]
# Instruments are split by what they measure. First match wins: flow, level and temperature come
# before pressure because a differential-pressure transmitter usually measures flow or level.
INSTRUMENT_KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("flow", ("flow",)),
    ("level", ("level",)),
    ("temperature", ("temperature", "temp")),
    ("pressure", ("pressure",)),
]


def equipment_class(kind: str) -> str:
    """Loose type key: lower case, text in brackets dropped, then the equipment class by keyword.
    "Tank (crude storage)" / "Storage tank" -> "tank"; "Shutdown (on/off) valve, actuated" /
    "On/off valve" -> "valve". Instruments are split by what they measure: "Level transmitter" /
    "Level indicator" -> "level instrument", "Pressure transmitter" -> "pressure instrument"; a bare
    "Instrument" -> "instrument" (kind unknown), which matches no expected instrument. A type with no
    class keyword falls back to its own cleaned text, so it only matches the same words."""
    text = re.sub(r"\([^)]*\)", " ", str(kind).lower())
    text = re.sub(r"[^a-z/ ]+", " ", text)
    for name, words in EQUIPMENT_CLASSES:
        if any(re.search(rf"\b{w}", text) for w in words):
            if name == "instrument":
                measured = next((k for k, keys in INSTRUMENT_KINDS if any(re.search(rf"\b{w}", text) for w in keys)),
                                None)
                return f"{measured} instrument" if measured else "instrument"
            return name
    return " ".join(text.split())


def norm_tag(tag: str) -> str:
    return re.sub(r"[\s–—_-]+", "-", str(tag).strip().upper())


_STOP = {"and", "the", "near", "side", "with", "for", "of"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if (len(t) >= 2 and t not in _STOP) or t.isdigit()}


def match_item(expected_item: str, found_items: list[str]) -> Optional[int]:
    """Index of the output finding that best matches the expected item (>= half of its words)."""
    want = _tokens(expected_item)
    best, best_score = None, 0.0
    for i, item in enumerate(found_items):
        score = len(want & _tokens(item)) / max(1, len(want))
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.5 else None


# ---------------------------------------------------------------- reading the deliverables
def docx_parts(content: bytes) -> tuple[list[tuple[str, str]], str]:
    """(findings as (item, severity) from the table with a Severity column, all text of the note)."""
    from docx import Document

    doc = Document(io.BytesIO(content))
    texts = [p.text for p in doc.paragraphs]
    findings: list[tuple[str, str]] = []
    for table in doc.tables:
        rows = [[c.text.strip() for c in r.cells] for r in table.rows]
        texts.extend(" | ".join(r) for r in rows)
        if not rows:
            continue
        header = [h.lower() for h in rows[0]]
        if "severity" in header and "item" in header:
            i_item, i_sev = header.index("item"), header.index("severity")
            findings.extend((r[i_item], r[i_sev]) for r in rows[1:] if len(r) > max(i_item, i_sev))
    return findings, "\n".join(texts)


def xlsx_rows(content: bytes) -> list[tuple[str, str]]:
    """(tag, equipment type) rows of the tag list's "Tags" sheet (type "" if there is no type column)."""
    import pandas as pd

    book = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    sheet = "Tags" if "Tags" in book.sheet_names else book.sheet_names[0]
    df = pd.read_excel(book, sheet_name=sheet)
    cols = {str(c).strip().lower(): c for c in df.columns}
    tag_col = cols.get("tag", df.columns[0])
    type_col = cols.get("equipment type") or cols.get("equipment_type") or cols.get("type")
    rows = []
    for _, row in df.iterrows():
        if pd.isna(row[tag_col]):
            continue
        kind = row[type_col] if type_col is not None and not pd.isna(row[type_col]) else ""
        rows.append((str(row[tag_col]), str(kind)))
    return rows


# ---------------------------------------------------------------- scenario checks
def check_inspection(section: str, files: dict[str, bytes]) -> list[Check]:
    docx = next((c for name, c in files.items() if name.lower().endswith(".docx")), None)
    if docx is None:
        return [Check("A: approval note .docx", False, "no .docx artifact")]
    try:
        findings, text = docx_parts(docx)
    except Exception as exc:
        return [Check("A: approval note .docx", False, f"cannot read the .docx: {type(exc).__name__}")]
    checks = []
    wanted = expected_findings(section)
    items = [f[0] for f in findings]
    problems = []
    for item, severity in wanted:
        idx = match_item(item, items)
        if idx is None:
            problems.append(f"{item}: missing")
        elif findings[idx][1].strip().lower() != severity.lower():
            problems.append(f"{item}: {findings[idx][1] or '?'} (expected {severity})")
    checks.append(Check("A: severities", bool(wanted) and not problems,
                        "; ".join(problems) if problems else f"{len(wanted)}/{len(wanted)} findings as expected"))
    cost = expected_cost(section)
    figure = re.sub(r"^\D+", "", cost or "")                     # "Rs 4,50,000" -> "4,50,000"
    checks.append(Check("A: cost", bool(figure) and figure in text,
                        f"{cost} found" if figure and figure in text else f"expected {cost}, not in the note"))
    sops = expected_sops(section)
    need = expected_min_sops(section)
    cited = [s for s in sops if s.lower() in text.lower()]
    if len(cited) >= need:
        detail = f"{len(cited)} cited (need {need}): " + ", ".join(cited)
    else:
        detail = f"only {len(cited)} cited, need {need}" + (f": {', '.join(cited)}" if cited else "")             + " (from: " + ", ".join(sops) + ")"
    checks.append(Check("A: SOPs cited", bool(sops) and len(cited) >= need, detail))
    return checks


def check_code(final: TaskState) -> list[Check]:
    answer = final.final_answer or ""
    found = next((r for r in B_RESULTS if r in answer), None)
    return [Check("B: result 7.246 mm", found is not None,
                  f"'{found}' in the answer" if found else "7.246 / 7.25 not in the final answer")]


def check_tags(section: str, files: dict[str, bytes]) -> list[Check]:
    xlsx = next((c for name, c in files.items() if name.lower().endswith(".xlsx")), None)
    if xlsx is None:
        return [Check("C: tag list .xlsx", False, "no .xlsx artifact")]
    try:
        rows = xlsx_rows(xlsx)
    except Exception as exc:
        return [Check("C: tag list .xlsx", False, f"cannot read the .xlsx: {type(exc).__name__}")]
    found_types: dict[str, str] = {}
    for tag, kind in rows:
        found_types.setdefault(norm_tag(tag), kind)   # the .xlsx lists each tag once; first row wins
    got = set(found_types)
    want = [norm_tag(t) for t in expected_tags(section)]
    missing = [t for t in want if t not in got]
    invented = sorted(got - set(want))
    detail = f"{len(want) - len(missing)}/{len(want)} tags"
    if missing:
        detail += "; missing " + ", ".join(missing)
    if invented:
        detail += "; invented " + ", ".join(invented)
    checks = [Check("C: tags", bool(want) and not missing and not invented, detail)]
    expected_types = expected_tag_types(section)
    wrong = [f"{tag} expected {expected_types[tag]}, found {found_types[tag] or 'no type'}"
             for tag in want if tag in found_types and tag in expected_types
             and equipment_class(found_types[tag]) != equipment_class(expected_types[tag])]
    checked = sum(1 for t in want if t in found_types)
    checks.append(Check("C: equipment types", bool(expected_types) and not wrong,
                        ("wrong type: " + "; ".join(wrong)) if wrong else f"{checked}/{checked} types match"))
    return checks


# ---------------------------------------------------------------- the soak runner
class Soak:
    def __init__(self, client: ApiClient, scenarios: list[DemoScenario], runs: int, mode: TaskMode,
                 out_dir: Path, expected_md: str, sleep: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.monotonic, task_timeout: float = TASK_TIMEOUT_S,
                 log: Callable[[str], None] = print) -> None:
        self.client, self.scenarios, self.runs, self.mode = client, scenarios, runs, mode
        self.out_dir, self.md, self.clock = out_dir, expected_md, clock
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))  # looked up per call (patchable)
        self.task_timeout, self.log = task_timeout, log
        self.results: list[RunResult] = []
        self.current_task: Optional[str] = None      # task of the run in progress (cancelled on Ctrl+C)
        self.partial: Optional[RunResult] = None     # result of the run in progress
        self.interrupted = False
        self.net_before: Optional[int] = None
        self.net_after: Optional[int] = None
        self.started = datetime.now()

    # ---- helpers
    def net001(self) -> Optional[int]:
        result = self.client.network_status()
        return result.data.external_seen_since_start if result.data else None

    def _wait(self, result: RunResult) -> Optional[TaskState]:
        after, deadline = 0, self.clock() + self.task_timeout
        while True:
            poll = self.client.poll_events(result.task_id, after)
            if poll.error is not None and poll.error.code == "TASK_NOT_FOUND":
                result.status, result.error = "lost", "the backend lost the task (restart?)"
                return None
            if poll.error is None:
                after = poll.next_seq
                if poll.done:
                    break
            if self.clock() > deadline:
                result.status, result.error = "timeout", f"not finished after {self.task_timeout:.0f} s"
                self.client.cancel_task(result.task_id)
                return None
            self.sleep(POLL_S)
        state = self.client.get_task(result.task_id)
        if state.data is None:
            result.status = "unknown"
            result.error = f"final state not readable: {state.error.message if state.error else '?'}"
        return state.data

    def _download(self, result: RunResult, artifacts: list[Artifact]) -> tuple[dict[str, bytes], Check]:
        files: dict[str, bytes] = {}
        problems = []
        art_dir = self.out_dir / "artifacts"
        for art in artifacts:
            got = self.client.download_artifact(art.artifact_id)
            entry = {"artifact_id": art.artifact_id, "filename": art.filename, "kind": art.kind.value,
                     "size_bytes": art.size_bytes, "downloaded": 0, "saved": None}
            if got.data is None or not got.data.content:
                problems.append(f"{art.filename}: {got.error.message if got.error else 'empty'}")
            else:
                files[art.filename] = got.data.content
                entry["downloaded"] = len(got.data.content)
                art_dir.mkdir(parents=True, exist_ok=True)
                target = art_dir / f"run{result.run:02d}_{result.scenario}__{Path(art.filename).name}"
                target.write_bytes(got.data.content)
                entry["saved"] = str(target.relative_to(self.out_dir))
            result.artifacts.append(entry)
        ok = bool(artifacts) and not problems
        detail = f"{len(files)}/{len(artifacts)} downloaded, all non-empty" if ok else \
            ("no artifacts" if not artifacts else "; ".join(problems))
        return files, Check("artifacts", ok, detail)

    # ---- one run of one scenario
    def run_one(self, run: int, scn: DemoScenario) -> RunResult:
        result = self.partial = RunResult(run=run, scenario=scn.key, work_order=scn.work_order)
        t0 = self.clock()
        try:
            file_ids: list[str] = []
            if scn.demo_file is not None:
                up = self.client.upload_file(scn.demo_file.name, scn.demo_file.read_bytes(),
                                             scn.mime_type or "application/octet-stream")
                if up.data is None:
                    result.error = f"upload failed: {up.error.message if up.error else '?'}"
                    return result
                file_ids.append(up.data.file_id)
            created = self.client.create_task(TaskCreate(message=scn.prompt, file_ids=file_ids, mode=self.mode,
                                                         scenario=scn.scenario))
            if created.data is None:
                result.error = f"task not created: {created.error.message if created.error else '?'}"
                return result
            result.task_id = self.current_task = created.data.task_id
            result.status = created.data.status.value
            final = self._wait(result)
            if final is None:
                return result
            result.status = final.status.value
            if final.status != TaskStatus.SUCCEEDED:
                result.error = f"{final.error.code}: {final.error.message}" if final.error else final.status.value
            files, art_check = self._download(result, final.artifacts)
            result.checks.append(art_check)
            if final.status == TaskStatus.SUCCEEDED:
                result.checks.extend(self._scenario_checks(scn, final, files))
            return result
        finally:
            result.seconds = round(self.clock() - t0, 1)

    def _scenario_checks(self, scn: DemoScenario, final: TaskState, files: dict[str, bytes]) -> list[Check]:
        section = expected_section(self.md, scn.demo_file.name) if scn.demo_file else ""
        if scn.scenario == Scenario.INSPECTION_NOTE:
            return check_inspection(section, files)
        if scn.scenario == Scenario.CODE_CALC:
            return check_code(final)
        if scn.scenario == Scenario.PID_TAGS:
            return check_tags(section, files)
        return []

    # ---- all runs
    def run_all(self) -> None:
        self.net_before = self.net001()
        try:
            for run in range(1, self.runs + 1):
                for scn in self.scenarios:
                    self.log(f"run {run}/{self.runs} {scn.work_order} {scn.key} ...")
                    result = self.run_one(run, scn)
                    self.partial, self.current_task = None, None
                    self.results.append(result)
                    self.log(f"  {'PASS' if result.passed else 'FAIL'} {result.status} {result.seconds:.0f} s "
                             + "; ".join(f"{c.name}: {'ok' if c.ok else 'NO'} ({c.detail})" for c in result.checks)
                             + (f" error: {result.error}" if result.error else ""))
        except KeyboardInterrupt:
            self.interrupted = True
            if self.current_task:
                self.client.cancel_task(self.current_task)
                self.log(f"Ctrl+C: cancelled task {self.current_task}; writing the report so far.")
            else:
                self.log("Ctrl+C: stopping; writing the report so far.")
            if self.partial is not None:            # the run that was going on: kept, marked interrupted
                self.partial.status, self.partial.error = "interrupted", "stopped with Ctrl+C (task cancelled)"
                self.results.append(self.partial)
        finally:
            self.net_after = self.net001()

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and not self.interrupted and all(r.passed for r in self.results) \
            and len(self.results) == self.runs * len(self.scenarios)

    # ---- report
    def summary_rows(self) -> list[list[str]]:
        rows = []
        for r in self.results:
            checks = ", ".join(f"{c.name} {'ok' if c.ok else 'NO'}" for c in r.checks) or "-"
            rows.append([str(r.run), r.work_order, r.scenario, r.status, f"{r.seconds:.0f} s",
                         str(sum(1 for a in r.artifacts if a["downloaded"])), checks,
                         "PASS" if r.passed else "FAIL"])
        return rows

    def report_md(self) -> str:
        head = ["Run", "WO", "Scenario", "Status", "Time", "Files", "Checks", "Result"]
        lines = [f"# Soak test {self.started:%Y-%m-%d %H:%M:%S}", "",
                 f"- Backend: {self.client.base_url} (UI contract {CONTRACT_VERSION})",
                 f"- Mode: {self.mode.value}; scenarios: {', '.join(s.work_order for s in self.scenarios)}; "
                 f"runs requested: {self.runs}",
                 f"- Result: **{'ALL PASSED' if self.all_passed else 'NOT ALL PASSED'}** "
                 f"({sum(r.passed for r in self.results)}/{len(self.results)} passed"
                 f"{', interrupted with Ctrl+C' if self.interrupted else ''})",
                 f"- NET-001 (core external connections since backend start): before {self._n(self.net_before)}, "
                 f"after {self._n(self.net_after)}{self._net_note()}", "",
                 "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(row) + " |" for row in self.summary_rows()]
        lines += ["", "## Per scenario", "", "| WO | Passed | Avg time | Max time |", "|---|---|---|---|"]
        for scn in self.scenarios:
            rs = [r for r in self.results if r.scenario == scn.key]
            if rs:
                secs = [r.seconds for r in rs]
                lines.append(f"| {scn.work_order} | {sum(r.passed for r in rs)}/{len(rs)} | "
                             f"{sum(secs) / len(secs):.0f} s | {max(secs):.0f} s |")
        failed = [r for r in self.results if not r.passed]
        if failed:
            lines += ["", "## Failures", ""]
            for r in failed:
                why = [f"{c.name}: {c.detail}" for c in r.checks if not c.ok]
                if r.error:
                    why.insert(0, r.error)
                lines.append(f"- run {r.run} {r.work_order} ({r.task_id or 'no task'}): " + "; ".join(why or [r.status]))
        lines += ["", "Raw data: `raw.json`. Downloaded files: `artifacts/`.", ""]
        return "\n".join(lines)

    @staticmethod
    def _n(value: Optional[int]) -> str:
        return "unknown" if value is None else str(value)

    def _net_note(self) -> str:
        if self.net_before is None or self.net_after is None:
            return ""
        delta = self.net_after - self.net_before
        return " (no new core connection)" if delta == 0 else f" (**+{delta} new core connection(s): check Network**)"

    def write_report(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "report.md").write_text(self.report_md(), encoding="utf-8")
        raw = {"started": self.started.isoformat(), "backend": self.client.base_url, "contract": CONTRACT_VERSION,
               "mode": self.mode.value, "runs": self.runs, "scenarios": [s.key for s in self.scenarios],
               "interrupted": self.interrupted, "all_passed": self.all_passed,
               "net001_before": self.net_before, "net001_after": self.net_after,
               "results": [{**asdict(r), "passed": r.passed} for r in self.results]}
        (self.out_dir / "raw.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return self.out_dir


def print_table(rows: list[list[str]]) -> None:
    head = ["Run", "WO", "Scenario", "Status", "Time", "Files", "Checks", "Result"]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(head)]
    for row in [head, ["-" * w for w in widths], *rows]:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Soak test the workbench through its HTTP API.")
    p.add_argument("--runs", type=int, default=1, help="how many rounds (default 1)")
    p.add_argument("--scenarios", default="a,b,c", help="work orders to run, e.g. a,b,c or b (default a,b,c)")
    p.add_argument("--mode", choices=[m.value for m in TaskMode], default=TaskMode.GUIDED.value)
    p.add_argument("--base-url", default=None, help="API URL (default WB_API_URL from .env)")
    p.add_argument("--out", type=Path, default=None, help="report folder (default docs/soak/<date_time>)")
    p.add_argument("--task-timeout", type=float, default=TASK_TIMEOUT_S, help="seconds per task (default 900)")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None, client: Optional[ApiClient] = None) -> int:
    args = parse_args(argv)
    try:
        scenarios = select_scenarios(args.scenarios)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return EXIT_FAILED
    client = client or ApiClient(base_url=args.base_url)
    health = client.health()
    if health.data is None:
        print(f"[FAIL] backend not reachable at {client.base_url}: {health.error.message if health.error else '?'}")
        print("       Start it first (scripts/start_demo.ps1).")
        return EXIT_NO_BACKEND
    out_dir = args.out or SOAK_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    soak = Soak(client, scenarios, max(1, args.runs), TaskMode(args.mode), out_dir,
                EXPECTED_MD.read_text(encoding="utf-8"), task_timeout=args.task_timeout)
    print(f"Soak test: {soak.runs} run(s) x {', '.join(s.work_order for s in scenarios)} ({args.mode} mode) "
          f"against {client.base_url}. Ctrl+C stops and still writes the report.")
    soak.run_all()
    folder = soak.write_report()
    print()
    print_table(soak.summary_rows())
    print()
    print(f"NET-001 before {soak._n(soak.net_before)}, after {soak._n(soak.net_after)}{soak._net_note()}")
    print(f"Report: {folder / 'report.md'}")
    if soak.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK if soak.all_passed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
