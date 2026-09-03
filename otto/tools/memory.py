"""Memory tools.

`remember` is SAFE on purpose (DECISIONS D-29): "remember that my projects live in
~/Projects" is the user asking for exactly this, and prompting for confirmation on the
thing they just asked for makes the feature annoying enough to go unused. The safety
comes from elsewhere — the store refuses secret-shaped values outright, everything is
listed in the UI, and `forget` is available. Deleting a memory is ALWAYS_CONFIRM,
because that is the irreversible direction.
"""

from __future__ import annotations

from typing import Any

from ..core.permissions import Permission
from ..memory.store import SCOPES, MemoryRefused
from .registry import ToolContext, ToolSpec


def _remember(
    ctx: ToolContext,
    key: str,
    value: str,
    scope: str = "global",
    scope_key: str = "",
) -> dict[str, Any]:
    memory = ctx.memory.remember(
        key, value, scope=scope, scope_key=scope_key, source=f"agent:{ctx.agent.id}"
    )
    return {
        "id": memory.id,
        "key": memory.key,
        "value": memory.value,
        "scope": memory.scope,
        "scope_key": memory.scope_key,
    }


def _verify_remember(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    """Read it back out of SQLite. If it is not there, it was not remembered."""
    stored = ctx.memory.get(
        result["key"], scope=result["scope"], scope_key=result["scope_key"]
    )
    if stored is None:
        return False, "the memory was not stored"
    if stored.value != result["value"]:
        return False, "the stored value does not match"
    return True, f"remembered {stored.key!r} in {stored.scope} memory"


REMEMBER = ToolSpec(
    name="remember",
    description=(
        "Store a standing preference the user has stated. Never store credentials."
    ),
    schema={
        "key": {"type": "string", "max_length": 120},
        "value": {"type": "string", "max_length": 4000},
        "scope": {"type": "string", "enum": list(SCOPES), "default": "global"},
        "scope_key": {"type": "string", "default": ""},
    },
    required=("key", "value"),
    handler=_remember,
    verifier=_verify_remember,
    permission=Permission.SAFE,
)


def _recall(ctx: ToolContext, query: str, limit: int = 8) -> dict[str, Any]:
    found = ctx.memory.search(query, limit=limit)
    return {
        "query": query,
        "memories": [m.as_dict() for m in found],
        "lines": [m.as_line() for m in found],
    }


def _verify_recall(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    return True, f"{len(result['memories'])} memories matched {result['query']!r}"


RECALL_MEMORY = ToolSpec(
    name="recall_memory",
    description="Look up what Otto remembers about something.",
    schema={
        "query": {"type": "string", "max_length": 400},
        "limit": {"type": "integer", "default": 8},
    },
    required=("query",),
    handler=_recall,
    verifier=_verify_recall,
    permission=Permission.SAFE,
)


def _forget(ctx: ToolContext, memory_id: int) -> dict[str, Any]:
    removed = ctx.memory.forget(memory_id)
    return {"id": memory_id, "removed": removed}


def _verify_forget(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    if not result["removed"]:
        return False, f"no memory with id {result['id']}"
    return True, f"memory {result['id']} deleted"


FORGET_MEMORY = ToolSpec(
    name="forget_memory",
    description="Delete one stored memory by id.",
    schema={"memory_id": {"type": "integer"}},
    required=("memory_id",),
    handler=_forget,
    verifier=_verify_forget,
    permission=Permission.ALWAYS_CONFIRM,
    confirm_template="Forget memory {memory_id}?",
    destructive=True,
)


MEMORY_TOOLS = (REMEMBER, RECALL_MEMORY, FORGET_MEMORY)

__all__ = ["MEMORY_TOOLS", "MemoryRefused"]
