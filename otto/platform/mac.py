"""The macOS boundary.

Everything macOS-specific lives behind `MacBridge`. There are two implementations:

* `OsascriptMac` — the real one. It refuses to even construct off Darwin, so a test
  can never accidentally reach it.
* `FakeMac` — an in-memory model of a Mac: installed apps, running processes,
  windows, a UI element tree, a clipboard and a Trash. Deterministic, and it is what
  the entire test suite runs against.

**AppleScript injection.** `osascript` compiles its input as *source*, and AppleScript
can `do shell script`. So untrusted values never touch script source: every script is
written with `on run argv` and the values are passed as trailing process arguments.
`run_script` is the only function that calls osascript, and it has no string-formatting
path at all — there is nowhere to interpolate even by mistake.
"""

from __future__ import annotations

import abc
import re
import shlex  # only for quoting in error messages, never for building a command
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

MAX_OUTPUT = 200_000
DEFAULT_TIMEOUT = 20.0


class MacError(Exception):
    """A macOS operation failed or was refused."""


class PermissionMissing(MacError):
    """macOS refused because Accessibility/Automation consent was not granted."""


@dataclass
class Element:
    """One accessibility element. Named, never a coordinate."""

    role: str
    name: str
    enabled: bool = True
    children: list["Element"] = field(default_factory=list)
    value: str = ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "name": self.name,
            "enabled": self.enabled,
            "value": self.value,
            "children": [c.as_dict() for c in self.children],
        }


@dataclass
class Window:
    app: str
    title: str


#: An app name we will accept. Rejects paths, metacharacters and empty names, so a
#: name can never smuggle anything into a script argument.
APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")


def valid_app_name(name: str) -> bool:
    return bool(isinstance(name, str) and APP_NAME.match(name.strip()))


class MacBridge(abc.ABC):
    """The whole macOS surface Otto is allowed to use.

    Note what is *not* here: there is no `click_at(x, y)`. Semantic operations only.
    """

    # -- apps and windows --------------------------------------------------
    @abc.abstractmethod
    def list_apps(self) -> list[str]:
        """Applications actually installed on this Mac."""

    @abc.abstractmethod
    def running_apps(self) -> list[str]: ...

    @abc.abstractmethod
    def open_app(self, name: str) -> None: ...

    @abc.abstractmethod
    def resolve_app(self, name: str) -> str:
        """Map a spoken/typed name to an installed app's real name, or raise.

        Separate from `open_app` so a verifier can know which app it is checking
        for without asking the screen what happens to be in front of it.
        """

    @abc.abstractmethod
    def frontmost_app(self) -> str | None: ...

    @abc.abstractmethod
    def get_active_window(self) -> Window | None: ...

    @abc.abstractmethod
    def open_url(self, url: str) -> None: ...

    # -- accessibility -----------------------------------------------------
    @abc.abstractmethod
    def accessibility_tree(self, app: str) -> Element: ...

    @abc.abstractmethod
    def find_element(self, app: str, name: str, role: str | None = None) -> Element | None: ...

    @abc.abstractmethod
    def click_element(self, app: str, name: str, role: str | None = None) -> None: ...

    @abc.abstractmethod
    def type_into_element(self, app: str, name: str, text: str) -> None: ...

    @abc.abstractmethod
    def select_menu_item(self, app: str, menu: str, item: str) -> None: ...

    # -- misc --------------------------------------------------------------
    @abc.abstractmethod
    def read_clipboard(self) -> str: ...

    @abc.abstractmethod
    def write_clipboard(self, text: str) -> None: ...

    @abc.abstractmethod
    def notify(self, title: str, message: str) -> None: ...

    @abc.abstractmethod
    def speak(self, text: str, voice: str | None = None, rate: int | None = None) -> None: ...

    @abc.abstractmethod
    def stop_speaking(self) -> None: ...

    @abc.abstractmethod
    def move_to_trash(self, path: str) -> None: ...

    @abc.abstractmethod
    def accessibility_trusted(self) -> bool: ...

    @property
    def is_real(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# The real implementation
# ---------------------------------------------------------------------------

#: Every script takes its untrusted values from `argv`. There is no formatting.
_SCRIPTS: dict[str, str] = {
    "open_app": 'on run argv\n tell application (item 1 of argv) to activate\nend run',
    "frontmost": (
        'on run argv\n tell application "System Events" to return name of first '
        "application process whose frontmost is true\nend run"
    ),
    "active_window": (
        'on run argv\n tell application "System Events"\n'
        "  set p to first application process whose frontmost is true\n"
        "  set n to name of p\n"
        '  set t to ""\n'
        "  try\n    set t to name of front window of p\n  end try\n"
        '  return n & "\\t" & t\n'
        "end tell\nend run"
    ),
    "running_apps": (
        'on run argv\n tell application "System Events" to return name of every '
        "application process whose background only is false\nend run"
    ),
    "open_url": (
        "on run argv\n open location (item 1 of argv)\nend run"
    ),
    # Elements are found by walking the window's contents and comparing names,
    # rather than with a `whose` clause on the process — a `whose` filter only
    # sees the process's direct children, so a button nested inside a group or a
    # toolbar (which is most buttons) would never be found.
    "click_element": (
        "on run argv\n"
        " set appName to item 1 of argv\n"
        " set elName to item 2 of argv\n"
        ' tell application "System Events"\n'
        "  tell process appName\n"
        "   set hit to missing value\n"
        "   repeat with w in windows\n"
        "    repeat with e in (entire contents of w)\n"
        "     try\n"
        "      if (name of e as text) is elName then\n"
        "       set hit to e\n"
        "       exit repeat\n"
        "      end if\n"
        "     end try\n"
        "    end repeat\n"
        "    if hit is not missing value then exit repeat\n"
        "   end repeat\n"
        "   if hit is missing value then\n"
        '    error "no element named " & elName & " in " & appName\n'
        "   end if\n"
        "   click hit\n"
        "  end tell\n"
        " end tell\n"
        "end run"
    ),
    "type_into": (
        "on run argv\n"
        " set appName to item 1 of argv\n"
        " set elName to item 2 of argv\n"
        " set theText to item 3 of argv\n"
        ' tell application "System Events"\n'
        "  tell process appName\n"
        "   set hit to missing value\n"
        "   repeat with w in windows\n"
        "    repeat with e in (entire contents of w)\n"
        "     try\n"
        "      if (name of e as text) is elName then\n"
        "       set hit to e\n"
        "       exit repeat\n"
        "      end if\n"
        "     end try\n"
        "    end repeat\n"
        "    if hit is not missing value then exit repeat\n"
        "   end repeat\n"
        "   if hit is missing value then\n"
        '    error "no element named " & elName & " in " & appName\n'
        "   end if\n"
        "   try\n"
        "    set focused of hit to true\n"
        "   end try\n"
        "   set value of hit to theText\n"
        "  end tell\n"
        " end tell\n"
        "end run"
    ),
    "select_menu": (
        "on run argv\n"
        " set appName to item 1 of argv\n"
        " set menuName to item 2 of argv\n"
        " set itemName to item 3 of argv\n"
        ' tell application "System Events"\n'
        "  tell process appName\n"
        "   click menu item itemName of menu 1 of menu bar item menuName "
        "of menu bar 1\n"
        "  end tell\n"
        " end tell\n"
        "end run"
    ),
    # Bounded on purpose. `entire contents` of a complex window can take many
    # seconds and pin a core — on a machine that throttles, an inspection tool
    # that spins the fans is a tool nobody uses. Two levels reach the toolbars,
    # groups and buttons that matter; `find_element` still searches deeply.
    "tree": (
        "on run argv\n"
        " set appName to item 1 of argv\n"
        " set out to {}\n"
        ' tell application "System Events"\n'
        "  tell process appName\n"
        "   repeat with w in windows\n"
        '    set end of out to "window" & tab & (name of w as text)\n'
        "    repeat with e in (UI elements of w)\n"
        "     try\n"
        '      set end of out to ((class of e) as text) & tab & (name of e as text)\n'
        "     end try\n"
        "     try\n"
        "      repeat with c in (UI elements of e)\n"
        "       try\n"
        '        set end of out to ((class of c) as text) & tab & '
        "(name of c as text)\n"
        "       end try\n"
        "      end repeat\n"
        "     end try\n"
        "    end repeat\n"
        "   end repeat\n"
        "  end tell\n"
        " end tell\n"
        " set AppleScript's text item delimiters to linefeed\n"
        " return out as text\n"
        "end run"
    ),
    "read_clipboard": "on run argv\n return (the clipboard as text)\nend run",
    "write_clipboard": "on run argv\n set the clipboard to (item 1 of argv)\nend run",
    "notify": (
        "on run argv\n display notification (item 2 of argv) with title "
        "(item 1 of argv)\nend run"
    ),
    "trash": (
        'on run argv\n tell application "Finder" to delete (POSIX file '
        "(item 1 of argv) as alias)\nend run"
    ),
    "trusted": (
        'on run argv\n tell application "System Events" to return (count of '
        "application processes) > 0\nend run"
    ),
}

_PERMISSION_MARKERS = (
    "not allowed assistive access",
    "-1743",
    "not authorized",
    "-25211",
    "osascript is not allowed",
)


class OsascriptMac(MacBridge):
    """`osascript` + System Events. Constructing this off Darwin is an error."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, allow_non_darwin: bool = False):
        if sys.platform != "darwin" and not allow_non_darwin:
            raise MacError(
                "OsascriptMac only runs on macOS. Otto uses FakeMac elsewhere so "
                "that nothing pretends to have driven a Mac that was never there."
            )
        self.timeout = timeout
        self._speaking: subprocess.Popen[bytes] | None = None

    @property
    def is_real(self) -> bool:
        return True

    # -- the only place osascript is invoked -------------------------------

    def run_script(self, key: str, *values: str) -> str:
        """Run a named script with `values` bound to `argv`.

        `values` are passed as separate process arguments. They are never
        concatenated into the script, so an AppleScript payload inside one of them
        is data: `item 1 of argv` yields the literal text.
        """
        script = _SCRIPTS[key]
        argv = ["osascript", "-e", script, "--"]
        for value in values:
            if not isinstance(value, str):
                raise MacError(f"script argument {value!r} is not a string")
            if "\x00" in value:
                raise MacError("script argument contains a null byte")
            argv.append(value)
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, shell=False
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MacError(f"{key} timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise MacError(f"could not run osascript: {exc}") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            lowered = stderr.lower()
            if any(marker in lowered for marker in _PERMISSION_MARKERS):
                raise PermissionMissing(
                    "macOS refused this automation. Grant Accessibility to the app "
                    "running Otto: System Settings → Privacy & Security → "
                    f"Accessibility. (osascript said: {stderr[:200]})"
                )
            raise MacError(f"{key} failed: {stderr[:400]}")
        return (proc.stdout or "")[:MAX_OUTPUT].rstrip("\n")

    # -- apps --------------------------------------------------------------

    def list_apps(self) -> list[str]:
        import os

        names: set[str] = set()
        for folder in ("/Applications", "/System/Applications",
                       os.path.expanduser("~/Applications"),
                       "/System/Applications/Utilities"):
            try:
                for entry in os.listdir(folder):
                    if entry.endswith(".app"):
                        names.add(entry[:-4])
            except OSError:
                continue
        return sorted(names)

    def running_apps(self) -> list[str]:
        return [line.strip() for line in self.run_script("running_apps").split(",") if line.strip()]

    def _require_installed(self, name: str) -> str:
        clean = (name or "").strip()
        if not valid_app_name(clean):
            raise MacError(
                f"{name!r} is not a plain application name (no paths, no punctuation)"
            )
        installed = self.list_apps()
        exact = [a for a in installed if a.lower() == clean.lower()]
        if exact:
            return exact[0]
        partial = [a for a in installed if clean.lower() in a.lower()]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise MacError(f"{clean!r} is not installed on this Mac")
        raise MacError(
            f"{clean!r} matches several installed apps: {', '.join(sorted(partial)[:5])}"
        )

    def resolve_app(self, name: str) -> str:
        return self._require_installed(name)

    def open_app(self, name: str) -> None:
        self.run_script("open_app", self._require_installed(name))

    def frontmost_app(self) -> str | None:
        return self.run_script("frontmost").strip() or None

    def get_active_window(self) -> Window | None:
        raw = self.run_script("active_window")
        if not raw:
            return None
        app, _, title = raw.partition("\t")
        return Window(app=app.strip(), title=title.strip())

    def open_url(self, url: str) -> None:
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise MacError(f"refusing URL {url!r}: only http and https are allowed")
        self.run_script("open_url", url)

    # -- accessibility -----------------------------------------------------

    def accessibility_tree(self, app: str) -> Element:
        name = self._require_installed(app)
        raw = self.run_script("tree", name)
        root = Element(role="application", name=name)
        current = root
        for line in raw.splitlines():
            role, _, label = line.partition("\t")
            role, label = role.strip(), label.strip()
            if not role:
                continue
            element = Element(role=role, name=label)
            if role == "window":
                root.children.append(element)
                current = element
            else:
                current.children.append(element)
        return root

    def find_element(self, app: str, name: str, role: str | None = None) -> Element | None:
        tree = self.accessibility_tree(app)
        wanted = name.strip().lower()
        for element in tree.walk():
            if element.name.strip().lower() == wanted and (
                role is None or element.role == role
            ):
                return element
        return None

    def click_element(self, app: str, name: str, role: str | None = None) -> None:
        self.run_script("click_element", self._require_installed(app), name)

    def type_into_element(self, app: str, name: str, text: str) -> None:
        self.run_script("type_into", self._require_installed(app), name, text)

    def select_menu_item(self, app: str, menu: str, item: str) -> None:
        self.run_script("select_menu", self._require_installed(app), menu, item)

    # -- misc --------------------------------------------------------------

    def read_clipboard(self) -> str:
        return self.run_script("read_clipboard")

    def write_clipboard(self, text: str) -> None:
        self.run_script("write_clipboard", text)

    def notify(self, title: str, message: str) -> None:
        self.run_script("notify", title, message)

    def speak(self, text: str, voice: str | None = None, rate: int | None = None) -> None:
        """macOS `say`, as a background process so a long sentence never blocks.

        `say` takes the text as an argument, so nothing is interpolated here either.
        """
        self.stop_speaking()
        argv = ["say"]
        if voice:
            if not re.match(r"^[A-Za-z ]{1,32}$", voice):
                raise MacError(f"refusing voice name {shlex.quote(voice)}")
            argv += ["-v", voice]
        if rate:
            argv += ["-r", str(int(rate))]
        argv.append(text)
        try:
            self._speaking = subprocess.Popen(  # noqa: S603 - argv list, shell=False
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False
            )
        except OSError as exc:
            raise MacError(f"could not run say: {exc}") from exc

    def stop_speaking(self) -> None:
        proc, self._speaking = self._speaking, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def move_to_trash(self, path: str) -> None:
        self.run_script("trash", str(path))

    def accessibility_trusted(self) -> bool:
        try:
            self.run_script("trusted")
        except PermissionMissing:
            return False
        except MacError:
            return False
        return True


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


class FakeMac(MacBridge):
    """A deterministic in-memory Mac.

    It models the things a verifier needs to re-read: which app is frontmost, which
    windows exist, what is in the clipboard, what has been trashed. Failures are
    injectable so the tests can cover the unhappy paths that matter (an app that
    will not launch, a missing accessibility permission).
    """

    def __init__(
        self,
        installed: list[str] | None = None,
        *,
        trusted: bool = True,
    ):
        self.installed = list(
            installed
            if installed is not None
            else ["Safari", "Visual Studio Code", "Terminal", "Finder", "Notes", "Mail"]
        )
        self.running: list[str] = ["Finder"]
        self.frontmost: str | None = "Finder"
        self.windows: dict[str, list[str]] = {"Finder": ["Desktop"]}
        self.trees: dict[str, Element] = {}
        self.clipboard: str = ""
        self.notifications: list[tuple[str, str]] = []
        self.spoken: list[str] = []
        self.trashed: list[str] = []
        self.clicks: list[tuple[str, str]] = []
        self.typed: list[tuple[str, str, str]] = []
        self.menu_selections: list[tuple[str, str, str]] = []
        self.script_arguments: list[tuple[str, tuple[str, ...]]] = []
        self.trusted = trusted
        #: app name → exception to raise on open_app, for failure-path tests.
        self.open_failures: dict[str, Exception] = {}
        self.speaking = False

    # -- helpers used by tests --------------------------------------------

    def set_tree(self, app: str, root: Element) -> None:
        self.trees[app] = root

    def _check_trusted(self, what: str) -> None:
        if not self.trusted:
            raise PermissionMissing(
                f"{what}: Accessibility permission has not been granted"
            )

    def _require_installed(self, name: str) -> str:
        clean = (name or "").strip()
        if not valid_app_name(clean):
            raise MacError(
                f"{name!r} is not a plain application name (no paths, no punctuation)"
            )
        exact = [a for a in self.installed if a.lower() == clean.lower()]
        if exact:
            return exact[0]
        partial = [a for a in self.installed if clean.lower() in a.lower()]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise MacError(f"{clean!r} is not installed on this Mac")
        raise MacError(f"{clean!r} matches several installed apps: {', '.join(partial)}")

    def record(self, key: str, *values: str) -> None:
        """Mirrors OsascriptMac.run_script's argument handling, so injection tests
        can assert the values arrive as separate, literal arguments."""
        self.script_arguments.append((key, tuple(values)))

    # -- MacBridge ---------------------------------------------------------

    def list_apps(self) -> list[str]:
        return sorted(self.installed)

    def running_apps(self) -> list[str]:
        return list(self.running)

    def resolve_app(self, name: str) -> str:
        return self._require_installed(name)

    def open_app(self, name: str) -> None:
        resolved = self._require_installed(name)
        self.record("open_app", resolved)
        failure = self.open_failures.get(resolved)
        if failure is not None:
            raise failure
        if resolved not in self.running:
            self.running.append(resolved)
        self.windows.setdefault(resolved, [f"{resolved} — window"])
        self.frontmost = resolved

    def frontmost_app(self) -> str | None:
        return self.frontmost

    def get_active_window(self) -> Window | None:
        if self.frontmost is None:
            return None
        titles = self.windows.get(self.frontmost) or [""]
        return Window(app=self.frontmost, title=titles[0])

    def open_url(self, url: str) -> None:
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise MacError(f"refusing URL {url!r}: only http and https are allowed")
        self.record("open_url", url)
        browser = "Safari" if "Safari" in self.installed else self.installed[0]
        self.running.append(browser) if browser not in self.running else None
        self.frontmost = browser
        self.windows.setdefault(browser, []).insert(0, url)

    def accessibility_tree(self, app: str) -> Element:
        resolved = self._require_installed(app)
        self._check_trusted("inspect_accessibility_tree")
        if resolved in self.trees:
            return self.trees[resolved]
        return Element(
            role="application",
            name=resolved,
            children=[
                Element(
                    role="window",
                    name=(self.windows.get(resolved) or [resolved])[0],
                    children=[Element(role="button", name="OK")],
                )
            ],
        )

    def find_element(self, app: str, name: str, role: str | None = None) -> Element | None:
        wanted = name.strip().lower()
        for element in self.accessibility_tree(app).walk():
            if element.name.strip().lower() == wanted and (
                role is None or element.role == role
            ):
                return element
        return None

    def click_element(self, app: str, name: str, role: str | None = None) -> None:
        resolved = self._require_installed(app)
        self._check_trusted("click_element")
        self.record("click_element", resolved, name)
        if self.find_element(resolved, name, role) is None:
            raise MacError(f"no element named {name!r} in {resolved}")
        self.clicks.append((resolved, name))

    def type_into_element(self, app: str, name: str, text: str) -> None:
        resolved = self._require_installed(app)
        self._check_trusted("type_into_element")
        self.record("type_into", resolved, name, text)
        element = self.find_element(resolved, name)
        if element is None:
            raise MacError(f"no element named {name!r} in {resolved}")
        element.value = text
        self.typed.append((resolved, name, text))

    def select_menu_item(self, app: str, menu: str, item: str) -> None:
        resolved = self._require_installed(app)
        self._check_trusted("select_menu_item")
        self.record("select_menu", resolved, menu, item)
        self.menu_selections.append((resolved, menu, item))

    def read_clipboard(self) -> str:
        return self.clipboard

    def write_clipboard(self, text: str) -> None:
        self.record("write_clipboard", text)
        self.clipboard = text

    def notify(self, title: str, message: str) -> None:
        self.record("notify", title, message)
        self.notifications.append((title, message))

    def speak(self, text: str, voice: str | None = None, rate: int | None = None) -> None:
        self.record("say", text)
        self.spoken.append(text)
        self.speaking = True

    def stop_speaking(self) -> None:
        self.speaking = False

    def move_to_trash(self, path: str) -> None:
        self.record("trash", str(path))
        self.trashed.append(str(path))

    def accessibility_trusted(self) -> bool:
        return self.trusted


def build_mac_bridge(*, force_fake: bool = False) -> MacBridge:
    """Pick an implementation. Off Darwin there is only ever the fake."""
    if force_fake or sys.platform != "darwin":
        return FakeMac()
    return OsascriptMac()
