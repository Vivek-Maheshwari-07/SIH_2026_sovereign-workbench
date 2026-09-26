"""
Minimal audit log writer. Appends one JSON line per call to logs/audit.jsonl,
shaped as shared.contracts.AuditRecord. Ticket A9 extends this later
(rotation, task linking, http/tool/network record helpers, etc.) — keep this
small for now.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from backend.settings import settings
from shared.contracts import AuditRecord

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


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

    log_path = _resolve(settings.WB_LOG_DIR) / "audit.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")

    return record
