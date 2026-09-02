"""The permission engine.

Three levels, an agent ceiling that a human cannot override, and an approval broker
that blocks on an event rather than polling.

The rules live here and are enforced inside `tools.registry.dispatch`. They are never
enforced by prompt wording: an agent that asks nicely gets exactly the same answer as
an agent that has been prompt-injected into asking rudely.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum

from .state import Approval, Task


class Permission(str, Enum):
    """Ordered least- to most-dangerous."""

    SAFE = "SAFE"
    CONFIRM = "CONFIRM"
    ALWAYS_CONFIRM = "ALWAYS_CONFIRM"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Permission):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Permission):
            return NotImplemented
        return self.rank <= other.rank


_RANK = {Permission.SAFE: 0, Permission.CONFIRM: 1, Permission.ALWAYS_CONFIRM: 2}


class PermissionDenied(Exception):
    """Raised when a call is refused outright — by a ceiling, or by the human."""

    def __init__(self, message: str, *, by_ceiling: bool = False) -> None:
        super().__init__(message)
        self.by_ceiling = by_ceiling


def exceeds_ceiling(required: Permission, ceiling: Permission) -> bool:
    """True when `required` is above what this agent may ever do.

    A ceiling is not "what the human has approved so far"; it is the maximum this
    agent is permitted to reach *at all*. The Research agent has a SAFE ceiling
    precisely because it reads untrusted text: even a human clicking Approve cannot
    let it write a file.
    """
    return required.rank > ceiling.rank


#: Signature of the UI hook that surfaces an approval to the human.
AskHuman = Callable[[Approval], None]


class ApprovalBroker:
    """Turns a required-permission decision into a human yes/no.

    `ask` is called with the pending Approval so the UI can display it; the broker
    then blocks on the approval's event. Cancelling the task releases the waiter
    (see `Task.cancel`). If no `ask` hook is installed, the broker denies rather
    than silently granting — failing closed is the only safe default.
    """

    def __init__(self, ask: AskHuman | None = None, *, timeout: float | None = None):
        self._ask = ask
        self._timeout = timeout
        self._lock = threading.Lock()
        self._auto: bool | None = None

    def set_ask(self, ask: AskHuman | None) -> None:
        with self._lock:
            self._ask = ask

    def set_auto(self, verdict: bool | None) -> None:
        """Test/headless hook: answer every approval with `verdict`.

        `None` restores normal behaviour. This is deliberately not reachable from
        the model or from any tool argument.
        """
        with self._lock:
            self._auto = verdict

    def request(
        self,
        task: Task,
        *,
        tool: str,
        args: dict,
        agent_id: str,
        level: Permission,
        reason: str,
    ) -> Approval:
        approval = Approval(
            tool=tool,
            args=args,
            agent_id=agent_id,
            level=level.value,
            reason=reason,
        )
        task.add_approval(approval)

        with self._lock:
            auto, ask = self._auto, self._ask

        if approval.pending:
            if auto is not None:
                approval.decide(auto)
            elif ask is None:
                approval.decide(False)
            else:
                try:
                    ask(approval)
                except Exception as exc:  # a broken UI must not hang the worker
                    task.log("approval_error", f"ask hook failed: {exc}")
                    approval.decide(False)

        approval.wait(self._timeout)
        if approval.pending:  # timed out
            approval.decide(False)
            task.log("approval_timeout", f"{tool} timed out waiting for a human")
        return approval
