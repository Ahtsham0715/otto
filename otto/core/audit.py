"""Append-only audit log.

Every tool call is recorded — including the refused ones, which are the interesting
ones. Values are redacted before they are written: an audit log that leaks the key it
was auditing is worse than no audit log.

Writes are line-buffered JSONL so a crash cannot corrupt earlier entries, and the
file is opened per-write rather than held open, so an idle Otto holds no handle.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .state import now

#: Keys whose values are always replaced, whatever they contain.
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "private_key",
        "session",
        "cookie",
    }
)

#: Value shapes that are redacted wherever they appear, including inside prose.
_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
)

REDACTED = "[redacted]"


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-shaped values. Structure is preserved so the log
    still shows *what* was attempted, just not with what credential."""
    if _depth > 8:
        return "[too deep]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower().strip("_") in SENSITIVE_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _VALUE_PATTERNS:
            redacted = pattern.sub(REDACTED, redacted)
        if len(redacted) > 4000:
            redacted = redacted[:4000] + "…"
        return redacted
    return value


class AuditLog:
    """Thread-safe JSONL audit. `path=None` keeps entries in memory only (tests)."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, keep: int = 500):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._recent: list[dict[str, Any]] = []
        self._keep = keep
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {"at": now(), "event": event, **redact(fields)}
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with self._lock:
            self._recent.append(entry)
            if len(self._recent) > self._keep:
                del self._recent[: len(self._recent) - self._keep]
            if self.path is not None:
                try:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    # An unwritable audit path must never take Otto down, but it
                    # must not silently vanish either: it stays in `recent`.
                    pass
        return entry

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent[-limit:])

    def count(self, event: str | None = None) -> int:
        with self._lock:
            if event is None:
                return len(self._recent)
            return sum(1 for e in self._recent if e["event"] == event)
