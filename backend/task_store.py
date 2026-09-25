"""
In-memory task store: task_id -> TaskState + its AgentEvent history, backed
by one queue and one worker thread (AGENTS.md rule 9: plain threads, no
async magic in the agent loop). Every read and write goes through one lock.
"""
from __future__ import annotations

import queue
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from shared.contracts import (
    TERMINAL_STATUSES,
    AgentEvent,
    Artifact,
    ErrorInfo,
    EventType,
    PlanStep,
    RouteDecision,
    Scenario,
    TaskMode,
    TaskState,
    TaskStatus,
)


def new_task_id() -> str:
    return "t_" + secrets.token_hex(6)


@dataclass
class _TaskRecord:
    task_id: str
    status: TaskStatus
    mode: TaskMode
    scenario: Optional[Scenario]
    message: str
    file_ids: list[str]
    route: Optional[RouteDecision] = None
    plan: list[PlanStep] = field(default_factory=list)
    final_answer: Optional[str] = None
    artifacts: list[Artifact] = field(default_factory=list)
    error: Optional[ErrorInfo] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cancel_requested: bool = False
    events: list[AgentEvent] = field(default_factory=list)
    next_seq: int = 1

    def to_state(self) -> TaskState:
        elapsed_s = None
        if self.started_at is not None:
            end = self.finished_at or datetime.now(timezone.utc)
            elapsed_s = (end - self.started_at).total_seconds()
        return TaskState(
            task_id=self.task_id,
            status=self.status,
            mode=self.mode,
            scenario=self.scenario,
            message=self.message,
            file_ids=list(self.file_ids),
            route=self.route,
            plan=list(self.plan),
            final_answer=self.final_answer,
            artifacts=list(self.artifacts),
            error=self.error,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            elapsed_s=elapsed_s,
        )


class TaskHandle:
    """
    Passed into the agent function that runs a task. Lets the agent read the
    original request, emit events, record its route/plan/answer, and check
    for cancellation between its own steps.
    """

    def __init__(self, store: "TaskStore", task_id: str, message: str, file_ids: list[str]) -> None:
        self._store = store
        self.task_id = task_id
        self.message = message
        self.file_ids = list(file_ids)

    def emit(
        self,
        event_type: EventType,
        title: str,
        data: Optional[dict] = None,
        *,
        step: Optional[int] = None,
    ) -> None:
        self._store._emit(self.task_id, event_type, title, data or {}, step)

    def is_cancelled(self) -> bool:
        return self._store._is_cancel_requested(self.task_id)

    def set_route(self, route: RouteDecision) -> None:
        self._store._update(self.task_id, route=route)

    def set_plan(self, plan: list[PlanStep]) -> None:
        self._store._update(self.task_id, plan=plan)

    def set_final_answer(self, answer: str) -> None:
        self._store._update(self.task_id, final_answer=answer)

    def add_artifact(self, artifact: Artifact) -> None:
        self._store._append_artifact(self.task_id, artifact)


class TaskStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, _TaskRecord] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._agent_fn: Optional[Callable[[TaskHandle], None]] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()

    # ------------------------------------------------------------ lifecycle
    def start(self, agent_fn: Callable[[TaskHandle], None]) -> None:
        """Start the single worker thread. Call once, at app startup."""
        self._agent_fn = agent_fn
        self._stop_requested.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="task-worker", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        """Stop the worker thread. Call once, at app shutdown."""
        self._stop_requested.set()
        self._queue.put("")  # wake a blocked queue.get() so the stop flag gets checked
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None

    # ------------------------------------------------------------ create / read
    def create(self, message: str, file_ids: list[str], mode: TaskMode, scenario: Optional[Scenario]) -> TaskState:
        task_id = new_task_id()
        record = _TaskRecord(
            task_id=task_id,
            status=TaskStatus.QUEUED,
            mode=mode,
            scenario=scenario,
            message=message,
            file_ids=list(file_ids),
        )
        with self._lock:
            self._tasks[task_id] = record
        self._queue.put(task_id)
        return record.to_state()

    def get(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            record = self._tasks.get(task_id)
            return record.to_state() if record else None

    def events(self, task_id: str, after: int) -> Optional[tuple[list[AgentEvent], int, bool]]:
        """
        Returns (events with seq > after, next_seq, done), or None if task_id
        is unknown. `next_seq` is the highest seq already recorded (0 if none
        yet) — that's the cursor value the caller passes back as `after` next
        time, per the contract. It is NOT `record.next_seq` (the store's
        internal "next seq to assign" counter): using that instead would be
        off by one and would make the caller's very next poll skip the event
        that gets that seq once it's created.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            page = [e for e in record.events if e.seq > after]
            done = record.status in TERMINAL_STATUSES
            last_seq = record.events[-1].seq if record.events else 0
            return page, last_seq, done

    def cancel(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            if record.status == TaskStatus.QUEUED:
                # Never starts: the worker checks status before running a task.
                record.status = TaskStatus.CANCELLED
                record.cancel_requested = True
                record.error = ErrorInfo(code="CANCELLED", message="Task cancelled by user.")
                record.finished_at = datetime.now(timezone.utc)
            elif record.status == TaskStatus.RUNNING:
                # The agent checks is_cancelled() between its own steps.
                record.cancel_requested = True
            return record.to_state()

    # ------------------------------------------------------------ internals used by TaskHandle
    def _is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            record = self._tasks.get(task_id)
            return bool(record and record.cancel_requested)

    def _emit(self, task_id: str, event_type: EventType, title: str, data: dict, step: Optional[int]) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            self._append_event_locked(record, event_type, title, data, step)

    def _append_event_locked(
        self,
        record: _TaskRecord,
        event_type: EventType,
        title: str,
        data: dict,
        step: Optional[int] = None,
    ) -> None:
        event = AgentEvent(
            seq=record.next_seq,
            task_id=record.task_id,
            ts=datetime.now(timezone.utc),
            type=event_type,
            step=step,
            title=title,
            data=data,
        )
        record.events.append(event)
        record.next_seq += 1

    def _update(self, task_id: str, **fields) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            for key, value in fields.items():
                setattr(record, key, value)

    def _append_artifact(self, task_id: str, artifact: Artifact) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.artifacts.append(artifact)

    # ------------------------------------------------------------ worker
    def _worker_loop(self) -> None:
        while not self._stop_requested.is_set():
            task_id = self._queue.get()
            if self._stop_requested.is_set():
                break
            if not task_id:
                continue
            self._run_task(task_id)

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            if record.status == TaskStatus.CANCELLED:
                return  # cancelled while queued: never start
            record.status = TaskStatus.RUNNING
            record.started_at = datetime.now(timezone.utc)
            message, file_ids = record.message, list(record.file_ids)

        handle = TaskHandle(self, task_id, message, file_ids)
        try:
            if self._agent_fn is None:
                raise RuntimeError("TaskStore.start() was never called")
            self._agent_fn(handle)
        except Exception as exc:  # the worker thread must survive every task failure
            with self._lock:
                record = self._tasks.get(task_id)
                if record is not None:
                    record.status = TaskStatus.FAILED
                    record.error = ErrorInfo(code="INTERNAL", message=str(exc))
                    record.finished_at = datetime.now(timezone.utc)
            return

        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            if record.cancel_requested:
                record.status = TaskStatus.CANCELLED
                record.error = ErrorInfo(code="CANCELLED", message="Task cancelled by user.")
            else:
                record.status = TaskStatus.SUCCEEDED
            record.finished_at = datetime.now(timezone.utc)


task_store = TaskStore()
