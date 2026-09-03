"""The no-model fast path.

This is the single biggest concession to the user's hardware (DECISIONS D-06). The
commands people actually say all day — "open Safari", "create a folder called Test on
my Desktop", "remember that my projects live in ~/Projects" — are matched by an
ordered list of regexes and turned straight into a validated `Plan` with concrete
tool calls.

Consequences that matter:

* **Otto is useful with no LLM installed at all**, which is the state of the user's
  Mac this morning.
* Those commands cost zero model calls, so no fan spin-up on a machine that throttles.
* Everything the fast path produces still goes through the same dispatch path, the
  same permission engine and the same verifiers. It is a shortcut around the *model*,
  never around the *safety*.

Anything not matched here falls through to the LLM planner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .planner import Plan, PlanStep

if TYPE_CHECKING:
    from ..services import Services

#: Spoken transcripts arrive without punctuation and with filler; normalise first.
_FILLER = re.compile(
    r"^(hey |ok |okay |otto[,: ]+|please |could you |can you |would you |i want you to |"
    r"i'd like you to )+",
    re.I,
)
_TRAILING = re.compile(r"[.!?,\s]+$")

FOLDER_WORDS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "document": "Documents",
    "downloads": "Downloads",
    "download": "Downloads",
    "projects": "Projects",
    "project folder": "Projects",
}

#: Spoken app names people say vs what the app is actually called.
APP_ALIASES = {
    "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
    "visual studio": "Visual Studio Code",
    "code": "Visual Studio Code",
    "chrome": "Google Chrome",
    "terminal": "Terminal",
    "the terminal": "Terminal",
    "system settings": "System Settings",
    "system preferences": "System Settings",
    "activity monitor": "Activity Monitor",
}

TEST_COMMANDS = {
    "pytest": ["pytest", "-q"],
    "npm": ["npm", "test"],
    "cargo": ["cargo", "test"],
    "go": ["go", "test", "./..."],
    "make": ["make", "test"],
}


@dataclass
class FastMatch:
    plan: Plan
    #: What Otto will say when it is done, if the plan succeeds.
    spoken: str = ""
    intent: str = ""


def normalise(text: str) -> str:
    cleaned = _FILLER.sub("", (text or "").strip())
    cleaned = _TRAILING.sub("", cleaned)
    return cleaned.strip()


def _step(n: int, agent: str, description: str, tool: str, args: dict,
          depends: tuple[str, ...] = ()) -> PlanStep:
    return PlanStep(
        id=f"f{n}",
        description=description,
        agent=agent,
        depends_on=depends,
        tool=tool,
        args=args,
    )


def resolve_folder(services: "Services", phrase: str) -> Path | None:
    """Turn 'my Desktop' / 'Documents' / '~/Projects/app' into a real path.

    Consults memory first, so "my project" means whatever the user told Otto it
    means — the personalisation the brief cares about, applied without a model.
    """
    text = (phrase or "").strip().strip("\"'")
    if not text:
        return None
    home = Path(services.config.home)

    lowered = text.lower().strip()
    lowered = re.sub(r"^(the |my |our )+", "", lowered)
    if lowered in FOLDER_WORDS:
        return home / FOLDER_WORDS[lowered]

    if text.startswith(("~", "/")):
        return Path(text).expanduser()

    # "my project", "the project", "my projects folder" → remembered location.
    if "project" in lowered:
        for memory in services.memory.search("projects folder location", limit=5):
            candidate = _path_in(memory.value)
            if candidate is not None:
                remainder = re.sub(r".*project(s)?\b", "", lowered).strip(" /")
                return (candidate / remainder) if remainder else candidate
        return home / "Projects"

    for key, folder in FOLDER_WORDS.items():
        if lowered.startswith(key):
            rest = lowered[len(key):].strip(" /")
            return (home / folder / rest) if rest else (home / folder)

    return home / "Desktop" / text


def _path_in(value: str) -> Path | None:
    match = re.search(r"(~[\w./-]*|/[\w./-]+)", value or "")
    return Path(match.group(1)).expanduser() if match else None


def _app_name(raw: str) -> str:
    name = normalise(raw).strip().strip("\"'")
    name = re.sub(r"^(the |my |up )+", "", name, flags=re.I)
    name = re.sub(r"\s+(app|application)$", "", name, flags=re.I)
    return APP_ALIASES.get(name.lower(), name).strip()


# ---------------------------------------------------------------------------
# The intent table. Order matters: the first match wins.
# ---------------------------------------------------------------------------

_URL = re.compile(
    r"^(?:open|go to|visit|browse to|launch)\s+(?P<url>https?://\S+|[\w-]+\.[a-z]{2,}\S*)$",
    re.I,
)
_OPEN_APP = re.compile(r"^(?:open|launch|start|bring up|switch to)\s+(?P<app>.{1,48}?)$", re.I)
_MAKE_FOLDER = re.compile(
    r"^(?:create|make|add|new)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory|dir)\s+"
    r"(?:called|named|@)?\s*(?P<name>[\w .'-]{1,60}?)\s*"
    r"(?:(?:on|in|inside|under)\s+(?P<where>.{1,60}?))?$",
    re.I,
)
_REMEMBER = re.compile(
    r"^(?:remember|note|keep in mind|don'?t forget)(?:\s+that)?\s*[:,]?\s*(?P<fact>.{3,400})$",
    re.I,
)
_FORGET_ALL = re.compile(r"^forget everything(?: you know)?(?: about me)?$", re.I)
_WHAT_REMEMBER = re.compile(
    r"^(?:what do you (?:remember|know)(?:\s+about\s+(?P<about>.{1,80}))?|"
    r"show me (?:my )?(?:memories|preferences)|list (?:my )?(?:memories|preferences))$",
    re.I,
)
_SUMMARISE = re.compile(
    r"^(?:read|open|summarise|summarize)\s+(?:this file\s+|the file\s+|file\s+)?"
    r"(?P<path>[~/][^\s]+|[\w .-]+\.\w{1,6})"
    r"(?:\s+and\s+(?:summarise|summarize|sum it up|tell me what.*))?$",
    re.I,
)
_LIST_DIR = re.compile(
    r"^(?:list|show|what'?s in)\s+(?:the\s+|my\s+)?(?:contents of\s+)?(?P<where>.{1,60}?)"
    r"(?:\s+folder)?$",
    re.I,
)
_RUN_TESTS = re.compile(
    r"^(?:(?:open\s+(?P<proj>.{1,40}?)\s+and\s+)?)?(?:run|execute)\s+(?:the\s+)?tests?"
    r"(?:\s+(?:in|for|on)\s+(?P<where>.{1,60}))?$",
    re.I,
)
_ACTIVE_WINDOW = re.compile(
    r"^(?:what(?:'s| is)?\s+(?:the\s+)?(?:active|frontmost|current)\s+(?:window|app)|"
    r"what am i looking at|which app is (?:active|open|frontmost))\??$",
    re.I,
)
_TRASH = re.compile(
    r"^(?:delete|remove|trash|bin)\s+(?P<path>[~/][^\s]+|[\w .-]+\.\w{1,6}|[\w .-]{1,60}?)"
    r"(?:\s+(?:from|on|in)\s+(?P<where>.{1,40}))?$",
    re.I,
)


def match(request: str, services: "Services") -> FastMatch | None:
    """Return a ready-to-run plan, or None to fall through to the LLM planner."""
    text = normalise(request)
    if not text:
        return None
    home = Path(services.config.home)

    # -- URLs before apps: "open github.com" is not an app -----------------
    m = _URL.match(text)
    if m:
        url = m.group("url")
        if not url.lower().startswith("http"):
            url = "https://" + url
        return FastMatch(
            Plan([_step(1, "mac", f"Open {url}", "open_url", {"url": url})]),
            spoken=f"Opening {url}",
            intent="open_url",
        )

    m = _ACTIVE_WINDOW.match(text)
    if m:
        return FastMatch(
            Plan([_step(1, "mac", "Report the frontmost window", "get_active_window", {})]),
            intent="active_window",
        )

    m = _FORGET_ALL.match(text)
    if m:
        # Deliberately not automated: wiping memory is the user's call, made in the
        # UI where they can see what they are deleting.
        return FastMatch(
            Plan([_step(1, "memory", "Explain how to clear memory", "speak",
                        {"text": "You can clear what I remember from the Memory "
                                 "window, where you can see every row before you "
                                 "delete it."})]),
            intent="forget_all",
        )

    m = _WHAT_REMEMBER.match(text)
    if m:
        # No topic means "show me everything": an empty query matches every row
        # rather than searching for the literal word "everything".
        about = (m.group("about") or "").strip()
        return FastMatch(
            Plan([_step(1, "memory", f"Recall memories about {about or 'everything'}",
                        "recall_memory", {"query": about})]),
            intent="recall",
        )

    m = _REMEMBER.match(text)
    if m:
        fact = m.group("fact").strip()
        key = _key_for(fact)
        return FastMatch(
            Plan([_step(1, "memory", f"Remember: {fact}", "remember",
                        {"key": key, "value": fact})]),
            spoken="Noted.",
            intent="remember",
        )

    m = _MAKE_FOLDER.match(text)
    if m:
        name = m.group("name").strip()
        where = m.group("where")
        parent = resolve_folder(services, where) if where else (home / "Desktop")
        target = (parent or home / "Desktop") / name
        return FastMatch(
            Plan([_step(1, "files", f"Create the folder {target}", "make_folder",
                        {"path": str(target)})]),
            spoken=f"Created {name}.",
            intent="make_folder",
        )

    m = _SUMMARISE.match(text)
    if m:
        path = _resolve_file(services, m.group("path"))
        if path is not None:
            return FastMatch(
                Plan([_step(1, "research", f"Summarise {path}", "summarise_file",
                            {"path": str(path)})]),
                intent="summarise",
            )

    m = _RUN_TESTS.match(text)
    if m:
        where = m.group("where") or m.group("proj") or "my project"
        folder = resolve_folder(services, where)
        if folder is not None:
            argv = _test_command(folder)
            steps = [
                _step(1, "coder", f"Run the tests in {folder}", "run_command",
                      {"argv": argv, "cwd": str(folder)})
            ]
            if m.group("proj"):
                steps.insert(
                    0,
                    _step(2, "mac", "Open the editor", "open_app",
                          {"name": "Visual Studio Code"}),
                )
                steps[1] = _step(
                    1, "coder", f"Run the tests in {folder}", "run_command",
                    {"argv": argv, "cwd": str(folder)},
                )
            return FastMatch(
                Plan(steps),
                spoken="Running the tests.",
                intent="run_tests",
            )

    m = _TRASH.match(text)
    if m:
        raw = m.group("path")
        where = m.group("where")
        parent = resolve_folder(services, where) if where else None
        target = (parent / raw) if parent else _resolve_file(services, raw)
        if target is not None:
            return FastMatch(
                Plan([_step(1, "files", f"Move {target} to the Trash", "move_to_trash",
                            {"path": str(target)})]),
                intent="trash",
            )

    m = _OPEN_APP.match(text)
    if m:
        app = _app_name(m.group("app"))
        if app and _plausible_app(app, services):
            return FastMatch(
                Plan([_step(1, "mac", f"Open {app}", "open_app", {"name": app})]),
                spoken=f"Opening {app}.",
                intent="open_app",
            )

    m = _LIST_DIR.match(text)
    if m:
        folder = resolve_folder(services, m.group("where"))
        if folder is not None and folder.is_dir():
            return FastMatch(
                Plan([_step(1, "files", f"List {folder}", "list_dir",
                            {"path": str(folder)})]),
                intent="list_dir",
            )

    return None


def _plausible_app(name: str, services: "Services") -> bool:
    """Only claim an app intent if the Mac actually has something matching.

    Without this, "open the pod bay doors" becomes a failed open_app instead of
    falling through to the planner, which can at least say something sensible.
    """
    try:
        installed = services.mac.list_apps()
    except Exception:
        return True
    lowered = name.lower()
    return any(lowered == a.lower() or lowered in a.lower() for a in installed)


def _resolve_file(services: "Services", raw: str) -> Path | None:
    text = (raw or "").strip().strip("\"'")
    if not text:
        return None
    if text.startswith(("~", "/")):
        return Path(text).expanduser()
    home = Path(services.config.home)
    for folder in ("Desktop", "Documents", "Downloads"):
        candidate = home / folder / text
        if candidate.exists():
            return candidate
    return home / "Desktop" / text


def _test_command(folder: Path) -> list[str]:
    """Pick the test command from what is actually in the folder."""
    try:
        names = {p.name for p in folder.iterdir()}
    except OSError:
        names = set()
    if "package.json" in names:
        return TEST_COMMANDS["npm"]
    if "Cargo.toml" in names:
        return TEST_COMMANDS["cargo"]
    if "go.mod" in names:
        return TEST_COMMANDS["go"]
    if any(n in names for n in ("pytest.ini", "pyproject.toml", "tests", "setup.cfg")):
        return TEST_COMMANDS["pytest"]
    if "Makefile" in names:
        return TEST_COMMANDS["make"]
    return TEST_COMMANDS["pytest"]


_KEY_HINTS = (
    (re.compile(r"\bproject", re.I), "projects"),
    (re.compile(r"\beditor|\bvs ?code|\bvim|\bemacs", re.I), "preferred editor"),
    (re.compile(r"\bbrowser|\bsafari|\bchrome|\bfirefox", re.I), "preferred browser"),
    (re.compile(r"\bwork(ing)? hours?|\bmornings?|\bevenings?", re.I), "working hours"),
    (re.compile(r"\bmy name is|\bcall me", re.I), "name"),
    (re.compile(r"\bterminal|\biterm", re.I), "preferred terminal"),
)


def _key_for(fact: str) -> str:
    """A stable, human-readable key, so re-stating a preference updates it rather
    than piling up near-duplicates the user has to weed out later."""
    for pattern, key in _KEY_HINTS:
        if pattern.search(fact):
            return key
    words = [w for w in re.findall(r"[A-Za-z]{3,}", fact)[:4]]
    return " ".join(words).lower() or "note"
