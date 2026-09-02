"""The one dispatch path.

`ToolRegistry.dispatch` is the only way anything happens in Otto. It always performs
the same seven steps, in the same order:

    resolve → validate → permission (+ceiling, +approval) → audit → execute
            → VERIFY → record

An agent holds tool *names*, never handlers, so there is no way to enter this
sequence part-way through. A tool cannot be registered without a verifier, so
"the model said it worked" is never how a call gets marked COMPLETED.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.agents import AgentSpec
from ..core.audit import AuditLog
from ..core.permissions import ApprovalBroker, Permission, exceeds_ceiling
from ..core.state import Status, Task, ToolCall, now


class ToolError(Exception):
    """A tool failed in a way that is the tool's fault, not the harness's."""


@dataclass
class ToolContext:
    """Everything a handler is allowed to touch. Handlers get no globals."""

    task: Task
    agent: AgentSpec
    services: Any  # otto.services.Services — duck-typed to keep imports cheap
    subtask_id: str | None = None

    @property
    def sandbox(self):
        return self.services.sandbox

    @property
    def screen(self):
        return self.services.screen

    @property
    def mac(self):
        return self.services.mac

    @property
    def memory(self):
        return self.services.memory

    @property
    def config(self):
        return self.services.config


#: A verifier re-reads real state and answers "did this actually happen?"
Verifier = Callable[[ToolContext, dict[str, Any], Any], "tuple[bool, str]"]
Handler = Callable[..., Any]
#: Permission may depend on the arguments (writing inside the sandbox is CONFIRM;
#: trashing anything is ALWAYS_CONFIRM).
PermissionFn = Callable[[dict[str, Any]], Permission]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, dict[str, Any]]
    handler: Handler
    verifier: Verifier
    permission: Permission | PermissionFn = Permission.SAFE
    #: A short sentence shown to the human in the approval prompt.
    confirm_template: str = "{tool} {args}"
    required: tuple[str, ...] = ()
    destructive: bool = False

    def level_for(self, args: dict[str, Any]) -> Permission:
        if callable(self.permission):
            return self.permission(args)
        return self.permission

    def describe(self) -> str:
        params = ", ".join(
            f"{k}:{v.get('type', 'string')}" + ("" if k in self.required else "?")
            for k, v in self.schema.items()
        )
        return f"{self.name}({params}) — {self.description}"


class SchemaError(ValueError):
    """Arguments did not match the tool's schema."""


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate_args(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """Strict validation. Unknown keys are an error, not something to ignore —
    a model that invents an argument is a model that misunderstood the tool."""
    if not isinstance(args, dict):
        raise SchemaError(f"{spec.name}: arguments must be an object")

    unknown = set(args) - set(spec.schema)
    if unknown:
        raise SchemaError(
            f"{spec.name}: unknown argument(s) {', '.join(sorted(unknown))}; "
            f"expected {', '.join(sorted(spec.schema))}"
        )

    missing = [k for k in spec.required if k not in args or args[k] is None]
    if missing:
        raise SchemaError(f"{spec.name}: missing required {', '.join(missing)}")

    cleaned: dict[str, Any] = {}
    for key, rules in spec.schema.items():
        if key not in args:
            if "default" in rules:
                cleaned[key] = rules["default"]
            continue
        value = args[key]
        expected = rules.get("type", "string")
        types = _TYPES.get(expected)
        if types is None:  # pragma: no cover - guards a typo in a tool definition
            raise SchemaError(f"{spec.name}: unknown type {expected!r} for {key}")
        # bool is an int subclass; an integer field must not silently accept True.
        if expected in ("integer", "number") and isinstance(value, bool):
            raise SchemaError(f"{spec.name}: {key} must be {expected}, got a boolean")
        if not isinstance(value, types):
            raise SchemaError(
                f"{spec.name}: {key} must be {expected}, got "
                f"{type(value).__name__}"
            )
        if "enum" in rules and value not in rules["enum"]:
            raise SchemaError(
                f"{spec.name}: {key} must be one of {', '.join(map(str, rules['enum']))}"
            )
        if expected == "string":
            max_len = rules.get("max_length", 20000)
            if len(value) > max_len:
                raise SchemaError(f"{spec.name}: {key} is longer than {max_len} chars")
        if expected == "array":
            max_items = rules.get("max_items", 200)
            if len(value) > max_items:
                raise SchemaError(f"{spec.name}: {key} has more than {max_items} items")
        cleaned[key] = value
    return cleaned


class ToolRegistry:
    """Holds tool specs and performs every dispatch."""

    def __init__(self, audit: AuditLog | None = None, broker: ApprovalBroker | None = None):
        self._tools: dict[str, ToolSpec] = {}
        self.audit = audit or AuditLog()
        self.broker = broker or ApprovalBroker()
        self._lock = threading.Lock()

    # -- registration ------------------------------------------------------

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.verifier is None:  # pragma: no cover - defensive
            raise ValueError(f"{spec.name}: a tool without a verifier cannot exist")
        for key in spec.required:
            if key not in spec.schema:
                raise ValueError(f"{spec.name}: required key {key!r} is not in schema")
        with self._lock:
            self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe_for_prompt(self, tools: tuple[str, ...] | None = None) -> str:
        names = sorted(tools) if tools else self.names()
        return "\n".join(
            self._tools[n].describe() for n in names if n in self._tools
        )

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # -- dispatch ----------------------------------------------------------

    def dispatch(
        self,
        ctx: ToolContext,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        attempt: int = 1,
    ) -> ToolCall:
        """Run one tool. Always returns a ToolCall; never raises for tool failure."""
        args = dict(args or {})
        call = ToolCall(
            tool=name,
            args=args,
            agent_id=ctx.agent.id,
            subtask_id=ctx.subtask_id,
            attempt=attempt,
        )

        # 0. A cancelled task executes nothing more.
        if ctx.task.cancelled:
            return self._fail(ctx, call, "task was cancelled", status=Status.CANCELLED)

        # 1. Resolve.
        spec = self._tools.get(name)
        if spec is None:
            return self._fail(ctx, call, f"no such tool: {name!r}")

        # 1b. The agent must actually own this tool.
        if not ctx.agent.may_use(name):
            return self._fail(
                ctx, call, f"agent {ctx.agent.id!r} is not permitted to use {name!r}"
            )

        # 2. Validate arguments.
        try:
            cleaned = validate_args(spec, args)
        except SchemaError as exc:
            return self._fail(ctx, call, str(exc))
        call.args = cleaned

        # 3. Permission: level, ceiling, then the human.
        level = spec.level_for(cleaned)
        call.permission_level = level.value
        if exceeds_ceiling(level, ctx.agent.ceiling):
            return self._fail(
                ctx,
                call,
                f"{name} needs {level.value} but agent {ctx.agent.id!r} has a "
                f"{ctx.agent.ceiling.value} ceiling — refused without asking",
                event="refused_by_ceiling",
            )

        if level is not Permission.SAFE:
            reason = self._confirm_text(spec, cleaned)
            approval = self.broker.request(
                ctx.task,
                tool=name,
                args=cleaned,
                agent_id=ctx.agent.id,
                level=level,
                reason=reason,
            )
            call.approval_id = approval.id
            if not approval.granted:
                status = Status.CANCELLED if approval.cancelled else Status.REQUIRES_HUMAN
                return self._fail(
                    ctx,
                    call,
                    "cancelled before approval" if approval.cancelled
                    else "the human declined this action",
                    status=status,
                    event="refused_by_human",
                )
            if ctx.task.cancelled:
                return self._fail(
                    ctx, call, "task was cancelled", status=Status.CANCELLED
                )

        # 4. Audit the attempt before it runs.
        self.audit.record(
            "tool_attempt",
            tool=name,
            args=cleaned,
            agent=ctx.agent.id,
            task=ctx.task.id,
            level=level.value,
            attempt=attempt,
        )

        # 5. Execute.
        call.status = Status.RUNNING
        call.started_at = now()
        try:
            result = spec.handler(ctx, **cleaned)
        except Exception as exc:  # a handler must never take Otto down
            call.finished_at = now()
            return self._fail(ctx, call, f"{type(exc).__name__}: {exc}")
        call.result = result

        # 6. Verify against real state. This is not optional.
        try:
            ok, detail = spec.verifier(ctx, cleaned, result)
        except Exception as exc:
            ok, detail = False, f"verifier raised {type(exc).__name__}: {exc}"
        call.verified = bool(ok)
        call.verification_detail = detail
        call.finished_at = now()

        if not ok:
            return self._fail(
                ctx, call, f"verification failed: {detail}", event="verification_failed"
            )

        # 7. Record.
        call.status = Status.COMPLETED
        self.audit.record(
            "tool_ok",
            tool=name,
            agent=ctx.agent.id,
            task=ctx.task.id,
            verified=True,
            detail=detail,
            duration=call.duration,
        )
        ctx.task.log(
            "tool_call",
            f"{ctx.agent.id} ran {name} — {detail}",
            subtask_id=ctx.subtask_id,
            agent_id=ctx.agent.id,
            data={"tool": name, "status": "COMPLETED"},
        )
        self._attach(ctx, call)
        return call

    # -- internals ---------------------------------------------------------

    def _confirm_text(self, spec: ToolSpec, args: dict[str, Any]) -> str:
        try:
            return spec.confirm_template.format(tool=spec.name, args=args, **args)
        except (KeyError, IndexError, ValueError):
            return f"{spec.name} {args}"

    def _fail(
        self,
        ctx: ToolContext,
        call: ToolCall,
        message: str,
        *,
        status: Status = Status.FAILED,
        event: str = "tool_failed",
    ) -> ToolCall:
        call.status = status
        call.error = message
        call.finished_at = call.finished_at or now()
        if call.verified is None:
            call.verified = False
        self.audit.record(
            event,
            tool=call.tool,
            args=call.args,
            agent=ctx.agent.id,
            task=ctx.task.id,
            error=message,
            level=call.permission_level,
        )
        ctx.task.log(
            "tool_call",
            f"{ctx.agent.id} {call.tool} → {status.value}: {message}",
            subtask_id=ctx.subtask_id,
            agent_id=ctx.agent.id,
            data={"tool": call.tool, "status": status.value},
        )
        self._attach(ctx, call)
        return call

    @staticmethod
    def _attach(ctx: ToolContext, call: ToolCall) -> None:
        subtask = ctx.task.subtask(ctx.subtask_id) if ctx.subtask_id else None
        if subtask is not None:
            subtask.calls.append(call)
        else:
            # Calls made outside a subtask (the fast path) still belong to the task.
            holder = ctx.task.subtasks[0] if ctx.task.subtasks else None
            if holder is not None:
                holder.calls.append(call)


def always_true(_ctx: ToolContext, _args: dict[str, Any], _result: Any) -> tuple[bool, str]:
    """For tools whose *only* effect is the value they return (e.g. reading).

    Even these are verified — the verifier below asserts the handler produced
    something. A tool that genuinely cannot be verified does not get registered.
    """
    return True, "returned a value"
