"""Global push-to-talk hotkey.

`pynput` over Quartz. The important detail from the research (docs/RESEARCH.md §4):
on macOS pynput **fails silently** when the host process is not a trusted
accessibility client — no exception, just a listener that never fires. That is the
worst possible failure for a hotkey, because Otto looks fine and does nothing.

So this module reports its own health, and the menu bar shows a red state with the
exact settings pane to open rather than pretending to be armed.

`pynput` is imported inside `start()`, never at module level.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

PERMISSION_HELP = (
    "Otto's hotkey needs two macOS permissions, granted to the app that launches "
    "it (Terminal, iTerm, or the python3 binary):\n"
    "  • System Settings → Privacy & Security → Input Monitoring\n"
    "  • System Settings → Privacy & Security → Accessibility\n"
    "Quit and relaunch Otto after granting them — macOS only re-reads the "
    "permission at process start."
)


class HotkeyManager:
    """Owns the listener thread. Safe to start and stop repeatedly."""

    def __init__(self, combination: str, on_trigger: Callable[[], None]):
        self.combination = combination
        self.on_trigger = on_trigger
        self._listener: Any = None
        self._lock = threading.Lock()
        self.error: str = ""
        self.triggered = 0

    @property
    def active(self) -> bool:
        return self._listener is not None and self.error == ""

    def _fire(self) -> None:
        self.triggered += 1
        try:
            self.on_trigger()
        except Exception as exc:  # never let a handler kill the listener
            self.error = f"hotkey handler failed: {exc}"

    def start(self) -> bool:
        with self._lock:
            if self._listener is not None:
                return True
            try:
                from pynput import keyboard
            except ImportError as exc:
                self.error = (
                    f"pynput is not installed ({exc}). Run ./setup.sh, or use the "
                    "menu item instead of the hotkey."
                )
                return False
            try:
                listener = keyboard.GlobalHotKeys({self.combination: self._fire})
                listener.daemon = True
                listener.start()
            except Exception as exc:
                self.error = (
                    f"could not register {self.combination!r}: {exc}\n\n"
                    + PERMISSION_HELP
                )
                return False
            self._listener = listener
            self.error = ""
            return True

    def stop(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def health(self) -> tuple[bool, str]:
        """(ok, message) for the menu bar."""
        if self.error:
            return False, self.error
        if self._listener is None:
            return False, "the hotkey listener is not running"
        if not accessibility_trusted():
            return False, (
                "macOS has not granted this process Accessibility/Input Monitoring, "
                "so the hotkey will silently never fire.\n\n" + PERMISSION_HELP
            )
        return True, f"listening for {self.combination}"


def accessibility_trusted() -> bool:
    """Ask macOS directly whether this process is a trusted accessibility client.

    Returns True off macOS so tests and the Linux path do not report a false alarm.
    """
    try:
        import sys

        if sys.platform != "darwin":
            return True
        from ApplicationServices import AXIsProcessTrusted  # type: ignore

        return bool(AXIsProcessTrusted())
    except Exception:
        # If we cannot ask, do not claim it is broken — the menu will still show
        # the hotkey's own error if registration failed.
        return True
