"""
Tests for backend.audit (A9): rotation at a size limit, thread-safe appends,
reading across rotated files, plus the new task and tool-call audit records.
Every test writes to a temp logs dir.
"""
from __future__ import annotations

import threading
import time

import pytest

from backend import agent_tools, audit
from backend.audit import AUDIT_FILE_NAME, read_audit_records, write_audit_record
from backend.settings import settings
from backend.task_store import TaskStore
from shared.contracts import AuditRecord, TaskMode, TaskStatus


@pytest.fixture(autouse=True)
def logs_dir(tmp_path, monkeypatch):
    path = tmp_path / "logs"
    monkeypatch.setattr(settings, "WB_LOG_DIR", path)
    return path


def test_record_is_appended_as_one_json_line(logs_dir):
    write_audit_record(kind="system", name="hello", detail={"x": 1})
    lines = (logs_dir / AUDIT_FILE_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and AuditRecord.model_validate_json(lines[0]).detail == {"x": 1}


def test_rotation_caps_files_and_reads_newest_first(logs_dir, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_MAX_BYTES", 1000)
    for i in range(60):
        write_audit_record(kind="system", name=f"r{i}")
    files = sorted(p.name for p in logs_dir.iterdir())
    assert files == [AUDIT_FILE_NAME] + [f"{AUDIT_FILE_NAME}.{i}" for i in range(1, audit.AUDIT_BACKUP_COUNT + 1)]
    assert all(p.stat().st_size <= 1000 for p in logs_dir.iterdir())
    newest = read_audit_records(limit=10)
    assert [r.name for r in newest] == [f"r{i}" for i in range(59, 49, -1)]
    everything = read_audit_records(limit=1000)
    assert everything[0].name == "r59" and len(everything) < 60                # oldest records were dropped
    names = [int(r.name[1:]) for r in everything]
    assert names == sorted(names, reverse=True)                                # contiguous across files


def test_concurrent_writes_are_all_whole_lines(logs_dir):
    def writer(n):
        for i in range(50):
            write_audit_record(kind="tool", name=f"t{n}", detail={"i": i, "pad": "x" * 200})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (logs_dir / AUDIT_FILE_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    for line in lines:
        AuditRecord.model_validate_json(line)


def test_write_failure_never_raises(logs_dir, monkeypatch):
    logs_dir.parent.mkdir(parents=True, exist_ok=True)
    logs_dir.write_text("a file where the logs folder should be", encoding="utf-8")
    record = write_audit_record(kind="system", name="still returns")
    assert record.name == "still returns"


def test_read_filters_by_task_id():
    write_audit_record(kind="tool", name="a", task_id="t_1")
    write_audit_record(kind="tool", name="b", task_id="t_2")
    assert [r.name for r in read_audit_records(task_id="t_2")] == ["b"]


# ---------------------------------------------------------------- task + tool records
def _wait_done(store: TaskStore, task_id: str) -> None:
    deadline = time.monotonic() + 10
    while store.get(task_id).status not in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.parametrize("fails", [False, True])
def test_task_created_and_finished_are_audited(fails):
    def agent_fn(handle):
        if fails:
            raise RuntimeError("boom")
        handle.set_final_answer("done")

    store = TaskStore()
    store.start(agent_fn)
    try:
        state = store.create("hello", [], TaskMode.AGENT, None)
        _wait_done(store, state.task_id)
    finally:
        store.stop()
    # read_audit_records is newest first; reverse before the stable sort so equal timestamps (Windows
    # clock ticks ~15 ms, a fast task can finish in the same tick) keep the order they were written in.
    records = sorted(reversed(read_audit_records(task_id=state.task_id)), key=lambda r: r.ts)
    assert [r.name for r in records] == ["task_created", "task_finished"]
    finished = records[1]
    assert finished.kind == "system" and finished.ok is (not fails)
    assert finished.detail["status"] == ("failed" if fails else "succeeded")


def test_task_cancelled_while_queued_is_audited():
    store = TaskStore()                                                        # worker never started
    state = store.create("hello", [], TaskMode.AGENT, None)
    store.cancel(state.task_id)
    finished = [r for r in read_audit_records(task_id=state.task_id) if r.name == "task_finished"]
    assert len(finished) == 1 and finished[0].detail["status"] == "cancelled" and finished[0].ok is False


def test_tool_calls_are_audited(monkeypatch):
    ctx = agent_tools.ToolContext(task_id="t_tool", emit=lambda *a, **k: None, add_artifact=lambda a: None)
    monkeypatch.setattr(agent_tools.knowledge, "search", lambda query, top_k=4: [])
    agent_tools.execute_tool(ctx, "search_knowledge", {"query": "hot work " * 100})
    agent_tools.execute_tool(ctx, "no_such_tool", {})
    records = {r.name: r for r in read_audit_records(task_id="t_tool")}
    assert records["search_knowledge"].kind == "tool" and records["search_knowledge"].ok is True
    assert len(records["search_knowledge"].detail["args"]) <= agent_tools.AUDIT_ARGS_CHARS
    assert records["no_such_tool"].ok is False
