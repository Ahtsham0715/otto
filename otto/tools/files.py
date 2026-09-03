"""Filesystem tools.

Every path argument goes through `PathSandbox.resolve` before it touches disk, so
traversal, absolute escapes, symlink escapes and credential files are all refused in
one place. Deletes move to the Trash through the macOS bridge; there is no `unlink`
anywhere in this file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..core.permissions import Permission
from ..core.state import Artifact
from ..security.paths import PathRefused
from .registry import ToolContext, ToolSpec

MAX_READ_BYTES = 400_000
MAX_LIST_ENTRIES = 300


def _read_text(path: Path, limit: int = MAX_READ_BYTES) -> str:
    data = path.read_bytes()[: limit + 1]
    truncated = len(data) > limit
    text = data[:limit].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n… [truncated at {limit} bytes]"
    return text


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def _read_file(ctx: ToolContext, path: str) -> dict[str, Any]:
    resolved = ctx.sandbox.resolve(path)
    if not resolved.exists():
        raise FileNotFoundError(f"{resolved} does not exist")
    if resolved.is_dir():
        raise IsADirectoryError(f"{resolved} is a folder, not a file")
    content = _read_text(resolved)
    ctx.task.add_artifact(
        Artifact(kind="file", name=str(resolved), value=str(resolved),
                 subtask_id=ctx.subtask_id)
    )
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "content": content}


def _verify_read(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    path = Path(result["path"])
    if not path.is_file():
        return False, f"{path} is not a readable file"
    return True, f"read {result['bytes']} bytes from {path.name}"


READ_FILE = ToolSpec(
    name="read_file",
    description="Read a text file inside the allowed folders.",
    schema={"path": {"type": "string"}},
    required=("path",),
    handler=_read_file,
    verifier=_verify_read,
    permission=Permission.SAFE,
)


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


def _list_dir(ctx: ToolContext, path: str) -> dict[str, Any]:
    resolved = ctx.sandbox.resolve(path)
    if not resolved.is_dir():
        raise NotADirectoryError(f"{resolved} is not a folder")
    entries = []
    for entry in sorted(resolved.iterdir())[:MAX_LIST_ENTRIES]:
        entries.append(
            {
                "name": entry.name,
                "kind": "folder" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            }
        )
    return {"path": str(resolved), "entries": entries}


def _verify_list(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    path = Path(result["path"])
    if not path.is_dir():
        return False, f"{path} is not a folder"
    return True, f"{len(result['entries'])} entries in {path.name}"


LIST_DIR = ToolSpec(
    name="list_dir",
    description="List the contents of a folder inside the allowed folders.",
    schema={"path": {"type": "string"}},
    required=("path",),
    handler=_list_dir,
    verifier=_verify_list,
    permission=Permission.SAFE,
)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


def _write_file(ctx: ToolContext, path: str, content: str, append: bool = False) -> dict:
    resolved = ctx.sandbox.resolve(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with resolved.open(mode, encoding="utf-8") as fh:
        fh.write(content)
    ctx.task.add_artifact(
        Artifact(kind="file", name=str(resolved), value=str(resolved),
                 subtask_id=ctx.subtask_id)
    )
    return {"path": str(resolved), "written": len(content), "append": append}


def _verify_write(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    """Re-read the file. A write that did not land is a failure, whatever the
    handler returned."""
    path = Path(result["path"])
    if not path.is_file():
        return False, f"{path} does not exist after the write"
    text = _read_text(path)
    expected = args.get("content", "")
    if args.get("append"):
        ok = text.endswith(expected)
    else:
        ok = text == expected or text.startswith(expected)
    return (ok, f"{path.name} now holds {path.stat().st_size} bytes") if ok else (
        False,
        f"{path.name} does not contain what was written",
    )


WRITE_FILE = ToolSpec(
    name="write_file",
    description="Write text to a file inside the allowed folders.",
    schema={
        "path": {"type": "string"},
        "content": {"type": "string", "max_length": 200_000},
        "append": {"type": "boolean", "default": False},
    },
    required=("path", "content"),
    handler=_write_file,
    verifier=_verify_write,
    permission=Permission.CONFIRM,
    confirm_template="Save changes to {path_spoken}?",
)


# ---------------------------------------------------------------------------
# make_folder
# ---------------------------------------------------------------------------


def _make_folder(ctx: ToolContext, path: str) -> dict[str, Any]:
    resolved = ctx.sandbox.resolve(path)
    existed = resolved.exists()
    resolved.mkdir(parents=True, exist_ok=True)
    ctx.task.add_artifact(
        Artifact(kind="file", name=str(resolved), value=str(resolved),
                 subtask_id=ctx.subtask_id)
    )
    return {"path": str(resolved), "created": not existed}


def _verify_folder(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    path = Path(result["path"])
    if not path.is_dir():
        return False, f"{path} does not exist after mkdir"
    return True, f"{path} exists"


MAKE_FOLDER = ToolSpec(
    name="make_folder",
    description="Create a folder (and any missing parents) inside the allowed folders.",
    schema={"path": {"type": "string"}},
    required=("path",),
    handler=_make_folder,
    verifier=_verify_folder,
    permission=Permission.CONFIRM,
    confirm_template="Create the folder {path_spoken}?",
)


# ---------------------------------------------------------------------------
# move_to_trash  — never unlink
# ---------------------------------------------------------------------------


def _move_to_trash(ctx: ToolContext, path: str) -> dict[str, Any]:
    resolved = ctx.sandbox.resolve(path)
    if not resolved.exists():
        raise FileNotFoundError(f"{resolved} does not exist")
    ctx.mac.move_to_trash(str(resolved))
    return {"path": str(resolved)}


def _verify_trash(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    """On a real Mac the Finder has moved the item, so it is gone from its old
    location. On the fake it is recorded rather than moved, so we check the record —
    which is exactly the distinction STATUS.md flags as needing a Mac."""
    path = Path(result["path"])
    trashed = str(path) in getattr(ctx.mac, "trashed", [])
    if ctx.mac.is_real:
        if path.exists():
            return False, f"{path} is still there — the Trash move did not happen"
        return True, f"{path.name} is in the Trash"
    if not trashed:
        return False, "the bridge did not record a Trash move"
    return True, f"{path.name} was moved to the Trash"


MOVE_TO_TRASH = ToolSpec(
    name="move_to_trash",
    description="Move a file or folder to the Trash. Never deletes permanently.",
    schema={"path": {"type": "string"}},
    required=("path",),
    handler=_move_to_trash,
    verifier=_verify_trash,
    permission=Permission.ALWAYS_CONFIRM,
    confirm_template="Move {path_spoken} to the Trash?",
    destructive=True,
)


# ---------------------------------------------------------------------------
# summarise_file — deterministic and extractive, so it costs no model call
# ---------------------------------------------------------------------------


def summarise_text(text: str, name: str = "", max_sentences: int = 5) -> str:
    """A cheap extractive summary.

    Deliberately not a model call: "read this file and summarise it" is a frequent
    command, and on this hardware a local model would take a minute to say what the
    first few sentences already say. The supervisor may still ask a model to rewrite
    this when one is configured.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    non_empty = [ln for ln in lines if ln]
    if not non_empty:
        return f"{name or 'The file'} is empty."

    headings = [ln for ln in non_empty if ln.startswith("#")][:3]
    prose: list[str] = []
    for line in non_empty:
        if line.startswith(("#", "```", "|", "-", "*", ">")):
            continue
        prose.append(line)
        if len(" ".join(prose)) > 600:
            break

    sentences: list[str] = []
    buffer = " ".join(prose)
    for chunk in buffer.replace("! ", ". ").replace("? ", ". ").split(". "):
        chunk = chunk.strip()
        if len(chunk) > 25:
            sentences.append(chunk.rstrip("."))
        if len(sentences) >= max_sentences:
            break

    # Prose only. Otto reads this out loud, and "6 non-empty lines, about 56 words"
    # is noise when spoken — the counts go in the tool result instead, where the
    # console can show them.
    parts = []
    if headings:
        parts.append("It's about " + ", ".join(
            h.lstrip("# ").strip() for h in headings
        ) + ".")
    if sentences:
        parts.append(" ".join(s + "." for s in sentences[:max_sentences]))
    elif not headings:
        parts.append(non_empty[0][:200])
    return " ".join(parts)


def _summarise_file(ctx: ToolContext, path: str) -> dict[str, Any]:
    resolved = ctx.sandbox.resolve(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"{resolved} is not a file")
    text = _read_text(resolved, 120_000)
    summary = summarise_text(text, resolved.name)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    ctx.task.add_artifact(
        Artifact(kind="text", name=f"summary of {resolved.name}", value=summary,
                 subtask_id=ctx.subtask_id)
    )
    return {
        "path": str(resolved),
        "summary": summary,
        "chars": len(text),
        "lines": len(lines),
        "words": len(text.split()),
    }


def _verify_summary(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    if not result.get("summary"):
        return False, "no summary was produced"
    if not Path(result["path"]).is_file():
        return False, "the file disappeared while summarising"
    return True, f"summarised {result['chars']} chars"


SUMMARISE_FILE = ToolSpec(
    name="summarise_file",
    description="Read a file and produce a short summary of it.",
    schema={"path": {"type": "string"}},
    required=("path",),
    handler=_summarise_file,
    verifier=_verify_summary,
    permission=Permission.SAFE,
)


FILE_TOOLS = (
    READ_FILE,
    LIST_DIR,
    WRITE_FILE,
    MAKE_FOLDER,
    MOVE_TO_TRASH,
    SUMMARISE_FILE,
)

__all__ = ["FILE_TOOLS", "summarise_text", "PathRefused", "os"]
