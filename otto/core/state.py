"""Structured orchestration state.

Every object here is a real typed record. Orchestration state is never parsed back
out of model prose: the model proposes, `agentloop.planner` validates, and the
objects below are the only thing the rest of Otto reads.

Stdlib only, and cheap to import (see DECISIONS D-26).
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    """The lifecycle of a task, subtask or tool call."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {Status.COMPLETED, Status.FAILED, Status.CANCELLED, Status.REQUIRES_HUMAN}
)

#: Transitions the state machine allows. Anything else raises.
_ALLOWED: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset(
        {Status.RUNNING, Status.WAITING, Status.CANCELLED, Status.FAILED}
    ),
    Status.RUNNING: frozenset(
        {
            Status.WAITING,
            Status.COMPLETED,
            Status.FAILED,
            Status.CANCELLED,
            Status.REQUIRES_HUMAN,
        }
    ),
    Status.WAITING: frozenset(
        {
            Status.RUNNING,
            Status.COMPLETED,
            Status.FAILED,
            Status.CANCELLED,
            Status.REQUIRES_HUMAN,
        }
    ),
    # Terminal states go nowhere. A cancelled task never resurrects.
    Status.COMPLETED: frozenset(),
    Status.FAILED: frozenset(),
    Status.CANCELLED: frozenset(),
    Status.REQUIRES_HUMAN: frozenset({Status.CANCELLED, Status.FAILED}),
}


class IllegalTransition(RuntimeError):
    """Raised when code tries to move a record into a state it cannot reach."""


def can_transition(old: Status, new: Status) -> bool:
    return new in _ALLOWED[old]


_ids = itertools.count(1)


def new_id(prefix: str) -> str:
    """Short, sortable, human-readable id. Unique within a process run."""
    return f"{prefix}-{next(_ids):04d}-{uuid.uuid4().hex[:6]}"


def now() -> float:
    return time.time()


@dataclass
class TimelineEvent:
    """One entry in a task's execution timeline. Append-only."""

    kind: str
    detail: str
    at: float = field(default_factory=now)
    subtask_id: str | None = None
    agent_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "at": self.at,
            "subtask_id": self.subtask_id,
            "agent_id": self.agent_id,
            "data": self.data,
        }


@dataclass
class AgentMessage:
    """A message between agents, or from an agent to the human."""

    sender: str
    recipient: str
    content: str
    at: float = field(default_factory=now)
    task_id: str | None = None
    subtask_id: str | None = None
    kind: str = "note"  # note | delegation | result | question | answer

    def as_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "at": self.at,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "kind": self.kind,
        }


@dataclass
class Artifact:
    """Something a task produced: a path, a transcript, a command's output."""

    kind: str  # file | text | url | command_output
    name: str
    value: str
    at: float = field(default_factory=now)
    subtask_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "value": self.value,
            "at": self.at,
            "subtask_id": self.subtask_id,
        }


@dataclass
class ToolCall:
    """One attempt to run one tool. Records the verification, not just the result."""

    tool: str
    args: dict[str, Any]
    agent_id: str
    id: str = field(default_factory=lambda: new_id("call"))
    status: Status = Status.PENDING
    result: Any = None
    error: str | None = None
    verified: bool | None = None
    verification_detail: str = ""
    permission_level: str | None = None
    approval_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    attempt: int = 1
    subtask_id: str | None = None

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "result": _shorten(self.result),
            "error": self.error,
            "verified": self.verified,
            "verification_detail": self.verification_detail,
            "permission_level": self.permission_level,
            "approval_id": self.approval_id,
            "attempt": self.attempt,
            "subtask_id": self.subtask_id,
            "duration": self.duration,
        }


@dataclass
class Approval:
    """A pending request for human consent.

    Waiting is event-driven: a waiter blocks on `event`, and cancellation sets every
    outstanding event with a CANCELLED verdict so no waiter can leak (D-17).
    """

    tool: str
    args: dict[str, Any]
    agent_id: str
    level: str
    reason: str
    id: str = field(default_factory=lambda: new_id("appr"))
    task_id: str | None = None
    granted: bool | None = None
    cancelled: bool = False
    decided_at: float | None = None
    created_at: float = field(default_factory=now)
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def decide(self, granted: bool) -> None:
        if self.event.is_set():
            return
        self.granted = granted
        self.decided_at = now()
        self.event.set()

    def cancel(self) -> None:
        if self.event.is_set():
            return
        self.granted = False
        self.cancelled = True
        self.decided_at = now()
        self.event.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until decided. Returns True only on an explicit grant."""
        self.event.wait(timeout)
        return bool(self.granted)

    @property
    def pending(self) -> bool:
        return not self.event.is_set()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "agent_id": self.agent_id,
            "level": self.level,
            "reason": self.reason,
            "task_id": self.task_id,
            "granted": self.granted,
            "cancelled": self.cancelled,
            "pending": self.pending,
        }


@dataclass
class Subtask:
    """One step of a plan, assigned to one agent."""

    description: str
    agent_id: str
    id: str = field(default_factory=lambda: new_id("step"))
    depends_on: list[str] = field(default_factory=list)
    status: Status = Status.PENDING
    result: str = ""
    error: str | None = None
    calls: list[ToolCall] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None

    def set_status(self, new: Status) -> None:
        if self.status is new:
            return
        if not can_transition(self.status, new):
            raise IllegalTransition(
                f"subtask {self.id}: {self.status.value} -> {new.value}"
            )
        self.status = new
        if new is Status.RUNNING and self.started_at is None:
            self.started_at = now()
        if new.terminal:
            self.finished_at = now()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "agent_id": self.agent_id,
            "depends_on": list(self.depends_on),
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "calls": [c.as_dict() for c in self.calls],
        }


@dataclass
class Task:
    """The unit of work. Thread-safe for the fields the UI reads while the
    supervisor writes."""

    request: str
    id: str = field(default_factory=lambda: new_id("task"))
    status: Status = Status.PENDING
    subtasks: list[Subtask] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    summary: str = ""
    error: str | None = None
    source: str = "text"  # text | voice
    created_at: float = field(default_factory=now)
    finished_at: float | None = None
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    _cancel: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    # -- lifecycle ---------------------------------------------------------

    def set_status(self, new: Status) -> None:
        with self._lock:
            if self.status is new:
                return
            if not can_transition(self.status, new):
                raise IllegalTransition(
                    f"task {self.id}: {self.status.value} -> {new.value}"
                )
            self.status = new
            if new.terminal:
                self.finished_at = now()
            self.timeline.append(TimelineEvent("status", new.value))

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self, reason: str = "cancelled by user") -> None:
        """Cancel the task and release every waiting approval.

        Idempotent, and safe to call from the UI thread while the supervisor runs.
        """
        with self._lock:
            if self._cancel.is_set():
                return
            self._cancel.set()
            for approval in self.approvals:
                approval.cancel()
            for subtask in self.subtasks:
                if not subtask.status.terminal:
                    subtask.status = Status.CANCELLED
                    subtask.finished_at = now()
                    subtask.error = reason
            if not self.status.terminal:
                self.status = Status.CANCELLED
                self.finished_at = now()
            self.error = self.error or reason
            self.timeline.append(TimelineEvent("cancelled", reason))

    # -- recording ---------------------------------------------------------

    def log(self, kind: str, detail: str, **kw: Any) -> TimelineEvent:
        event = TimelineEvent(kind, detail, **kw)
        with self._lock:
            self.timeline.append(event)
        return event

    def send(self, message: AgentMessage) -> None:
        message.task_id = self.id
        with self._lock:
            self.messages.append(message)
            self.timeline.append(
                TimelineEvent(
                    "message",
                    f"{message.sender} → {message.recipient}: {message.content[:120]}",
                    subtask_id=message.subtask_id,
                    agent_id=message.sender,
                )
            )

    def add_artifact(self, artifact: Artifact) -> None:
        with self._lock:
            self.artifacts.append(artifact)
            self.timeline.append(
                TimelineEvent(
                    "artifact", f"{artifact.kind}: {artifact.name}",
                    subtask_id=artifact.subtask_id,
                )
            )

    def add_approval(self, approval: Approval) -> Approval:
        approval.task_id = self.id
        with self._lock:
            if self._cancel.is_set():
                # Never let an approval created during a cancellation block forever.
                approval.cancel()
            self.approvals.append(approval)
            self.timeline.append(
                TimelineEvent("approval_requested", f"{approval.tool}: {approval.reason}")
            )
        return approval

    def subtask(self, subtask_id: str) -> Subtask | None:
        for s in self.subtasks:
            if s.id == subtask_id:
                return s
        return None

    @property
    def pending_approvals(self) -> list[Approval]:
        with self._lock:
            return [a for a in self.approvals if a.pending]

    @property
    def calls(self) -> list[ToolCall]:
        return [c for s in self.subtasks for c in s.calls]

    @property
    def active_agents(self) -> list[str]:
        with self._lock:
            return sorted(
                {s.agent_id for s in self.subtasks if s.status is Status.RUNNING}
            )

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "request": self.request,
                "status": self.status.value,
                "source": self.source,
                "summary": self.summary,
                "error": self.error,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "subtasks": [s.as_dict() for s in self.subtasks],
                "timeline": [e.as_dict() for e in self.timeline],
                "messages": [m.as_dict() for m in self.messages],
                "artifacts": [a.as_dict() for a in self.artifacts],
                "approvals": [a.as_dict() for a in self.approvals],
                "active_agents": [
                    s.agent_id for s in self.subtasks if s.status is Status.RUNNING
                ],
            }


def _shorten(value: Any, limit: int = 2000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… [{len(value) - limit} more chars]"
    return value
