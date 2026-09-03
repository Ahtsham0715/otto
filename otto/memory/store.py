"""Memory and personalisation.

One SQLite table, four scopes, no embeddings and no vector database (DECISIONS D-08).
Retrieval is scoped `LIKE` matching ordered by hit count and recency, capped — which
matters because every retrieved row is context, and context is time on a machine
generating at single-digit tokens per second.

Nothing secret-shaped is ever stored: `remember` refuses and says why.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..security.secrets import looks_like_secret

SCOPES = ("global", "workspace", "agent", "task")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    scope_key   TEXT NOT NULL DEFAULT '',
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user',
    confidence  REAL NOT NULL DEFAULT 1.0,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE(scope, scope_key, key)
);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, scope_key);
CREATE INDEX IF NOT EXISTS idx_mem_key ON memories(key);

CREATE TABLE IF NOT EXISTS usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    last_at     REAL NOT NULL,
    UNIQUE(kind, subject)
);
"""


class MemoryRefused(Exception):
    """A value was refused — it looked like a credential, or the scope was wrong."""


@dataclass(frozen=True)
class Memory:
    id: int
    scope: str
    scope_key: str
    key: str
    value: str
    source: str
    confidence: float
    hits: int
    created_at: float
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_key": self.scope_key,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "hits": self.hits,
            "updated_at": self.updated_at,
        }

    def as_line(self) -> str:
        where = f" ({self.scope_key})" if self.scope_key else ""
        return f"{self.key}{where}: {self.value}"


def _row(r: sqlite3.Row) -> Memory:
    return Memory(
        id=r["id"],
        scope=r["scope"],
        scope_key=r["scope_key"],
        key=r["key"],
        value=r["value"],
        source=r["source"],
        confidence=r["confidence"],
        hits=r["hits"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


_WORD = re.compile(r"[A-Za-z0-9_./~-]{3,}")
_STOPWORDS = frozenset(
    """the a an and or but for with from into that this these those you your my our
    is are was were be been being do does did doing have has had please can could
    would should what where when which who how why open run make create tell show
    about then than they them their there here also just very much more most""".split()
)


class MemoryStore:
    """SQLite-backed, thread-safe, inspectable and fully deletable."""

    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path else ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writing -----------------------------------------------------------

    def remember(
        self,
        key: str,
        value: str,
        *,
        scope: str = "global",
        scope_key: str = "",
        source: str = "user",
        confidence: float = 1.0,
    ) -> Memory:
        """Store or update one preference. Raises `MemoryRefused` for secrets."""
        if scope not in SCOPES:
            raise MemoryRefused(f"unknown scope {scope!r}; expected one of {SCOPES}")
        key = (key or "").strip()
        value = (value or "").strip()
        if not key:
            raise MemoryRefused("a memory needs a key")
        if not value:
            raise MemoryRefused("a memory needs a value")
        if looks_like_secret(value, key):
            raise MemoryRefused(
                f"refusing to remember {key!r}: the value looks like a credential. "
                "API keys belong in the Keychain, not in Otto's memory."
            )
        if len(value) > 4000:
            raise MemoryRefused("that value is too long to remember (4000 char limit)")

        stamp = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO memories
                    (scope, scope_key, key, value, source, confidence,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(scope, scope_key, key) DO UPDATE SET
                    value=excluded.value,
                    source=excluded.source,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at
                """,
                (scope, scope_key, key, value, source, confidence, stamp, stamp),
            )
            self._db.commit()
        found = self.get(key, scope=scope, scope_key=scope_key)
        assert found is not None  # just written
        return found

    def forget(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self._db.commit()
            return cur.rowcount > 0

    def forget_key(self, key: str, *, scope: str = "global", scope_key: str = "") -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM memories WHERE scope=? AND scope_key=? AND key=?",
                (scope, scope_key, key),
            )
            self._db.commit()
            return cur.rowcount > 0

    def clear(self, *, scope: str | None = None, scope_key: str | None = None) -> int:
        """Delete everything, or everything in one scope. The user can always do this."""
        query = "DELETE FROM memories"
        params: list[Any] = []
        clauses = []
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if scope_key is not None and scope:
            clauses.append("scope_key=?")
            params.append(scope_key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._lock:
            cur = self._db.execute(query, params)
            self._db.commit()
            return cur.rowcount

    # -- reading -----------------------------------------------------------

    def get(self, key: str, *, scope: str = "global", scope_key: str = "") -> Memory | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM memories WHERE scope=? AND scope_key=? AND key=?",
                (scope, scope_key, key),
            ).fetchone()
        return _row(row) if row else None

    def all(self, *, scope: str | None = None, limit: int = 500) -> list[Memory]:
        query = "SELECT * FROM memories"
        params: list[Any] = []
        if scope:
            query += " WHERE scope=?"
            params.append(scope)
        query += " ORDER BY scope, key LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [_row(r) for r in rows]

    def search(
        self,
        text: str,
        *,
        scopes: tuple[str, ...] = SCOPES,
        scope_key: str | None = None,
        limit: int = 8,
    ) -> list[Memory]:
        """Scoped LIKE search over keys and values.

        Scoping is what makes this work without embeddings: a workspace memory only
        matches when we are in that workspace, so the candidate set stays tiny.
        """
        terms = [
            w.lower()
            for w in _WORD.findall(text or "")
            if w.lower() not in _STOPWORDS
        ][:6]
        # An empty query means "show me everything" (the memory list in the UI).
        # A query that *had* words but none worth matching on means "nothing
        # matched" — otherwise "the a is" would return the user's whole profile.
        if (text or "").strip() and not terms:
            return []
        placeholders = ",".join("?" for _ in scopes)
        params: list[Any] = list(scopes)
        query = f"SELECT * FROM memories WHERE scope IN ({placeholders})"
        if scope_key is not None:
            # Global memories have no scope key; scoped ones must match ours.
            query += " AND (scope_key='' OR scope_key=?)"
            params.append(scope_key)
        if terms:
            likes = " OR ".join(["LOWER(key) LIKE ? OR LOWER(value) LIKE ?"] * len(terms))
            query += f" AND ({likes})"
            for term in terms:
                params.extend([f"%{term}%", f"%{term}%"])
        query += " ORDER BY hits DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        found = [_row(r) for r in rows]
        self._bump([m.id for m in found])
        return found

    def context_for(
        self, text: str, *, workspace: str | None = None, agent: str | None = None,
        limit: int = 8,
    ) -> list[Memory]:
        """The memory the planner sees. Global preferences always count; workspace
        and agent memories only count when they are the current ones."""
        results: list[Memory] = []
        seen: set[int] = set()
        for scope, key in (("global", ""), ("workspace", workspace), ("agent", agent)):
            if key is None:
                continue
            for memory in self.search(text, scopes=(scope,), scope_key=key, limit=limit):
                if memory.id not in seen:
                    seen.add(memory.id)
                    results.append(memory)
        return results[:limit]

    def _bump(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany(
                "UPDATE memories SET hits = hits + 1 WHERE id=?", [(i,) for i in ids]
            )
            self._db.commit()

    # -- implicit learning -------------------------------------------------

    def note_usage(self, kind: str, subject: str) -> int:
        """Count how often something is used — an app, a command, a folder.

        This is how Otto learns preferred apps and frequent tasks without anybody
        writing a preference down.
        """
        stamp = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO usage (kind, subject, count, last_at) VALUES (?,?,1,?)
                ON CONFLICT(kind, subject) DO UPDATE SET
                    count = count + 1, last_at = excluded.last_at
                """,
                (kind, subject, stamp),
            )
            self._db.commit()
            row = self._db.execute(
                "SELECT count FROM usage WHERE kind=? AND subject=?", (kind, subject)
            ).fetchone()
        return int(row["count"]) if row else 1

    def top_usage(self, kind: str, limit: int = 5) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT subject, count FROM usage WHERE kind=? "
                "ORDER BY count DESC, last_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [(r["subject"], r["count"]) for r in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
            by_scope = {
                r["scope"]: r["c"]
                for r in self._db.execute(
                    "SELECT scope, COUNT(*) c FROM memories GROUP BY scope"
                ).fetchall()
            }
        return {"total": total, **by_scope}
