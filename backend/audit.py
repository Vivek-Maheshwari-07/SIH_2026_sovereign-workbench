"""
Audit log. Appends one JSON line per event to logs/audit.jsonl, shaped as
shared.contracts.AuditRecord. Written for LLM calls (llm_client), sandbox
runs, tasks (created / finished), agent tool calls, network probes and every
new external connection the network monitor sees.

Thread safe: the task worker, the network monitor thread and request
handlers all write here, so appends and rotation happen under one lock.

Size limit: when audit.jsonl would pass AUDIT_MAX_BYTES it is renamed to
audit.jsonl.1 (older files shift to .2, .3; the oldest beyond
AUDIT_BACKUP_COUNT is dropped). So the audit trail on disk is capped at
about (1 + AUDIT_BACKUP_COUNT) * AUDIT_MAX_BYTES = 20 MB.

Writing never raises: an audit failure is logged to logs/backend.log and the
caller carries on (a full disk must not kill a task or the monitor thread).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from backend.settings import settings
from shared.contracts import AuditRecord

_REPO_ROOT = Path(__file__).resolve().parent.parent

AUDIT_FILE_NAME = "audit.jsonl"
AUDIT_MAX_BYTES = 5_000_000   # ~15-20k records per file
AUDIT_BACKUP_COUNT = 3        # audit.jsonl.1 .. .3 kept

_lock = threading.Lock()
logger = logging.getLogger("backend.audit")


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


def audit_path() -> Path:
    return _resolve(settings.WB_LOG_DIR) / AUDIT_FILE_NAME


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotate_if_needed(path: Path, incoming_bytes: int) -> None:
    """Caller holds _lock."""
    if not path.exists() or path.stat().st_size + incoming_bytes <= AUDIT_MAX_BYTES:
        return
    oldest = _backup_path(path, AUDIT_BACKUP_COUNT)
    if oldest.exists():
        oldest.unlink()
    for index in range(AUDIT_BACKUP_COUNT - 1, 0, -1):
        source = _backup_path(path, index)
        if source.exists():
            os.replace(source, _backup_path(path, index + 1))
    os.replace(path, _backup_path(path, 1))


def write_audit_record(
    *,
    kind: Literal["llm", "tool", "http", "network", "system"],
    name: str,
    target: Optional[str] = None,
    duration_ms: int = 0,
    ok: bool = True,
    detail: Optional[dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> AuditRecord:
    record = AuditRecord(
        ts=datetime.now(timezone.utc),
        task_id=task_id,
        kind=kind,
        name=name,
        target=target,
        duration_ms=duration_ms,
        ok=ok,
        detail=detail or {},
    )
    line = (record.model_dump_json() + "\n").encode("utf-8")
    try:
        with _lock:
            path = audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path, len(line))
            with path.open("ab") as f:
                f.write(line)
    except Exception:
        logger.warning("could not write audit record %s/%s", kind, name, exc_info=True)
    return record


def _records_in(path: Path) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(AuditRecord.model_validate_json(line))
        except Exception:
            continue  # skip a malformed line rather than failing the whole read
    return records


def read_audit_records(*, task_id: Optional[str] = None, limit: int = 100) -> list[AuditRecord]:
    """Most recent first, across audit.jsonl and its rotated backups, up to `limit`."""
    path = audit_path()
    newest_first: list[AuditRecord] = []
    with _lock:
        files = [path] + [_backup_path(path, i) for i in range(1, AUDIT_BACKUP_COUNT + 1)]
        for file in files:
            if not file.exists():
                continue
            for record in reversed(_records_in(file)):
                if task_id is None or record.task_id == task_id:
                    newest_first.append(record)
                    if len(newest_first) >= limit:
                        return newest_first
    return newest_first
