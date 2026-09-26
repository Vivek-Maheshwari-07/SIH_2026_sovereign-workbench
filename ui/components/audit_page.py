"""
B7: audit page. Every model call, tool run, network event and system event the backend recorded,
newest first. Filters: task id, kind, limit, hide other apps' connections, failures only.
Network leak records (core external connection, ok false) are highlighted red, platform amber.
"""
from __future__ import annotations

import json
from typing import Any

import streamlit as st

from shared.contracts import AuditRecord
from ui import api_client, messages
from ui.components.theme import esc

KINDS = ["All", "llm", "tool", "http", "network", "system"]
LIMITS = [50, 100, 200, 500, 1000]
DETAIL_CHARS = 140
ARGS_CHARS = 90                 # tool arguments are long JSON; the first words are enough in the table


def is_leak(rec: AuditRecord) -> bool:
    return rec.kind == "network" and not rec.ok and rec.detail.get("component") == "core"


def is_platform(rec: AuditRecord) -> bool:
    return rec.kind == "network" and rec.detail.get("component") == "platform"


def is_other_app(rec: AuditRecord) -> bool:
    return rec.kind == "network" and rec.detail.get("origin") == "other_app"


def key_detail(rec: AuditRecord) -> str:
    """The one or two facts that matter per kind, in plain text."""
    d: dict[str, Any] = rec.detail or {}
    if rec.kind == "network":
        return f"{d.get('process', '?')} ({d.get('label', d.get('origin', '?'))}, {d.get('status', '')})"
    if rec.kind == "llm":
        tokens = d.get("tokens_out")
        return f"{d.get('purpose', '')}" + (f", {tokens} tokens" if tokens is not None else "")
    if rec.kind == "tool":
        if "exit_code" in d:
            tests = ("" if d.get("tests_passed") is None
                     else f", tests {d.get('tests_passed')} passed, {d.get('tests_failed')} failed")
            return f"exit code {d.get('exit_code')}{tests}" + (" (timed out)" if d.get("timed_out") else "")
        if d.get("error_code"):
            return f"error {d['error_code']}"
        args = d.get("args")
        text = str(args or "")
        return text if len(text) <= ARGS_CHARS else text[: ARGS_CHARS - 1] + "…"
    if rec.kind == "system":
        if rec.name == "prewarm":
            items = d.get("items") or []
            return f"{sum(1 for i in items if i.get('ok'))}/{len(items)} items ready"
        return ", ".join(f"{k} {v}" for k, v in d.items() if v not in (None, ""))[:DETAIL_CHARS]
    text = json.dumps(d, ensure_ascii=False)
    return text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 1] + "…"


def filter_records(records: list[AuditRecord], kind: str, hide_other_apps: bool, failures_only: bool) -> list[AuditRecord]:
    out = [r for r in records if kind == "All" or r.kind == kind]
    if hide_other_apps:
        out = [r for r in out if not is_other_app(r)]
    if failures_only:
        out = [r for r in out if not r.ok]
    return sorted(out, key=lambda r: r.ts, reverse=True)


def row_class(rec: AuditRecord) -> str:
    if is_leak(rec):
        return "leak"
    if is_platform(rec) and not rec.ok:
        return "platform"
    return "fail" if not rec.ok else ""


def records_html(records: list[AuditRecord]) -> str:
    rows = []
    for r in records:
        ok = '<span class="c-ok">ok</span>' if r.ok else '<span class="c-alarm">no</span>'
        dur = f"{r.duration_ms / 1000:.1f} s" if r.duration_ms else ""
        rows.append(
            f'<tr class="{row_class(r)}"><td class="t">{r.ts.astimezone().strftime("%H:%M:%S")}</td>'
            f'<td>{esc(r.kind)}</td><td>{esc(r.name)}</td><td class="mono">{esc(r.target or "")}</td>'
            f'<td>{ok}</td><td>{esc(key_detail(r))}</td><td class="d">{dur}</td>'
            f'<td class="task">{esc(r.task_id or "")}</td></tr>')
    head = ('<tr><th>Time</th><th>Kind</th><th>Name</th><th>Target</th><th>OK</th><th>Detail</th>'
            '<th>Time taken</th><th>Task</th></tr>')
    return f'<table class="wb-conn wb-audit">{head}{"".join(rows)}</table>'


def render(current_task: str | None = None) -> None:
    st.markdown('<div class="wb-h">Audit log</div>', unsafe_allow_html=True)
    st.caption("Everything the backend recorded: model calls (all to 127.0.0.1), tool runs, network events "
               "and system events. Red rows are core leaks, amber rows are platform connections.")
    c1, c2, c3, c4 = st.columns([2.2, 1.2, 1, 1.2], vertical_alignment="bottom")
    task_id = c1.text_input("Task id", key="audit_task", placeholder=current_task or "t_... (empty = all tasks)")
    kind = c2.selectbox("Kind", KINDS, key="audit_kind")
    limit = c3.selectbox("Limit", LIMITS, index=3, key="audit_limit")
    c4.button("Refresh", key="audit_refresh", width="stretch")
    t1, t2 = st.columns(2)
    hide = t1.toggle("Hide other apps' connections", value=True, key="audit_hide_other")
    failures = t2.toggle("Only records that failed", value=False, key="audit_failures")

    result = api_client.get_client().audit(task_id=task_id.strip() or None, limit=int(limit))
    if result.data is None:
        messages.show("The audit log could not be loaded", result.error, result.failure)
        return
    records = filter_records(result.data, kind, hide, failures)
    leaks = sum(1 for r in records if is_leak(r))
    summary = f"{len(records)} of {len(result.data)} records shown"
    if leaks:
        summary += f", {leaks} core leak record(s) highlighted"
    st.markdown(f'<div class="wb-meta">{summary}</div>', unsafe_allow_html=True)
    with st.container(height=460, border=False, key="audit_table", autoscroll=False):
        if records:
            st.markdown(records_html(records), unsafe_allow_html=True)
        else:
            st.markdown('<div class="wb-empty">No records match these filters.</div>', unsafe_allow_html=True)
