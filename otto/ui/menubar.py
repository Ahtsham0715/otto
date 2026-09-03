"""The menu-bar app.

`rumps` over PyObjC — tens of MB rather than Electron's hundreds
(docs/RESEARCH.md §1). All rumps contact is confined to this file so it can be
replaced with direct PyObjC if rumps ever breaks on a future macOS.

The seven UI states from the brief are the status-item title: idle, listening,
thinking, executing, waiting for confirmation, speaking, error.

`rumps` is imported inside `run()`. Importing this module on Linux is free, which is
what lets the test suite import the app package without a Mac.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..app import ERROR, EXECUTING, IDLE, LISTENING, SPEAKING, THINKING, WAITING

if TYPE_CHECKING:
    from ..app import Otto

#: Status-item title per state. Text, not images: an emoji costs nothing and shows
#: up correctly on every macOS version.
STATE_TITLES = {
    IDLE: "🎙",
    LISTENING: "🔴",
    THINKING: "💭",
    EXECUTING: "⚙️",
    WAITING: "❓",
    SPEAKING: "🔊",
    ERROR: "⚠️",
}

STATE_LABELS = {
    IDLE: "Ready",
    LISTENING: "Listening…",
    THINKING: "Thinking…",
    EXECUTING: "Working…",
    WAITING: "Waiting for you",
    SPEAKING: "Speaking",
    ERROR: "Something went wrong",
}


class MenuBarApp:
    """Wires Otto, the hotkey and the voice pipeline into a status item."""

    def __init__(self, otto: "Otto"):
        self.otto = otto
        self.app: Any = None
        self.console: Any = None
        self.hotkey: Any = None
        self.voice: Any = None
        self._rumps: Any = None

    # -- construction ------------------------------------------------------

    def build(self) -> Any:
        import rumps

        self._rumps = rumps
        config = self.otto.services.config

        self.app = rumps.App("Otto", title=STATE_TITLES[IDLE], quit_button=None)
        self.status_item = rumps.MenuItem(STATE_LABELS[IDLE])
        self.status_item.set_callback(None)

        self.app.menu = [
            self.status_item,
            None,
            rumps.MenuItem(f"Talk to Otto  ({config.hotkey})", callback=self.on_talk),
            rumps.MenuItem("Type a command…", callback=self.on_type),
            rumps.MenuItem("Cancel", callback=self.on_cancel),
            None,
            rumps.MenuItem("Last result…", callback=self.on_last_result),
            rumps.MenuItem("Recent tasks…", callback=self.on_recent),
            rumps.MenuItem("What Otto remembers…", callback=self.on_memory),
            None,
            rumps.MenuItem("Developer console", callback=self.on_console),
            rumps.MenuItem(self._model_label(), callback=self.on_models),
            rumps.MenuItem("Check permissions…", callback=self.on_permissions),
            None,
            rumps.MenuItem("Quit Otto", callback=self.on_quit),
        ]

        self.otto.on_state_change(self._on_state)
        self.otto.set_approval_hook(self._ask_approval)

        from ..voice.pipeline import VoicePipeline

        self.voice = VoicePipeline(self.otto)

        from .hotkey import HotkeyManager

        self.hotkey = HotkeyManager(config.hotkey, self.on_hotkey)
        self.hotkey.start()
        return self.app

    # -- state -------------------------------------------------------------

    def _on_state(self, state: str, _otto: "Otto") -> None:
        if self.app is None:
            return
        self.app.title = STATE_TITLES.get(state, STATE_TITLES[IDLE])
        label = STATE_LABELS.get(state, state)
        task = self.otto.current
        if state in (THINKING, EXECUTING) and task is not None:
            agents = task.active_agents
            if agents:
                label = f"{label} ({', '.join(agents)})"
            elif task.request:
                label = f"{label} “{task.request[:32]}”"
        if state is ERROR and self.otto.last_error:
            label = f"⚠️ {self.otto.last_error[:48]}"
        try:
            self.status_item.title = label
        except Exception:
            pass

    # -- actions -----------------------------------------------------------

    def on_hotkey(self) -> None:
        if self.voice is not None:
            self.voice.toggle()

    def on_talk(self, _sender: Any = None) -> None:
        self.on_hotkey()

    def on_type(self, _sender: Any = None) -> None:
        response = self._rumps.Window(
            title="Otto",
            message="What would you like me to do?",
            default_text="",
            ok="Run",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if response.clicked and response.text.strip():
            self.otto.submit(response.text.strip(), source="text")

    def on_cancel(self, _sender: Any = None) -> None:
        if self.voice is not None and self.voice.recording:
            self.voice.cancel()
            return
        if not self.otto.cancel():
            self._notify("Otto", "Nothing to cancel.")

    def on_last_result(self, _sender: Any = None) -> None:
        task = self.otto.current
        if task is None:
            self._alert("Otto", "Nothing has run yet.")
            return
        lines = [f"{task.request}", "", task.summary or task.error or "(no result)", ""]
        for subtask in task.subtasks:
            lines.append(
                f"• [{subtask.status.value}] {subtask.description}"
                + (f" — {subtask.result or subtask.error or ''}")
            )
        self._alert("Otto — last result", "\n".join(lines)[:1800])

    def on_recent(self, _sender: Any = None) -> None:
        recent = self.otto.snapshot()["recent"]
        if not recent:
            self._alert("Otto", "No tasks yet.")
            return
        body = "\n".join(
            f"[{t['status']}] {t['request']} — {t['summary'][:60]}" for t in recent
        )
        self._alert("Otto — recent tasks", body[:1800])

    def on_memory(self, _sender: Any = None) -> None:
        """Memory is edited in the console, where every row is visible and
        deletable — not through a series of modal dialogs."""
        self.on_console()

    def on_console(self, _sender: Any = None) -> None:
        from .console import DevConsole

        if self.console is None:
            self.console = DevConsole(self.otto)
        try:
            url = self.console.start()
            self.otto.services.mac.open_url(url)
        except Exception as exc:
            self._alert("Otto", f"Could not open the console: {exc}")

    def on_models(self, _sender: Any = None) -> None:
        status = self.otto.model_status()
        lines = [
            f"{tier}: {info['kind']} {info['model']}".strip()
            for tier, info in status["tiers"].items()
        ]
        if not status["any_configured"]:
            lines.append("")
            lines.append(
                "No model is configured. Otto still handles simple commands "
                "directly. See SETUP.md to add Ollama (local) or a Groq/Cerebras "
                "key (fast, free tier)."
            )
        if status["cloud_tiers"]:
            lines.append("")
            lines.append(
                f"⚠️ A cloud model is in use for: {', '.join(status['cloud_tiers'])}. "
                "Audio and file contents are never sent to it unless you turn that "
                "on explicitly."
            )
        lines.append("")
        lines.append(f"Edit ~/.otto/config.json to change this.")
        self._alert("Otto — models", "\n".join(lines))

    def on_permissions(self, _sender: Any = None) -> None:
        lines = []
        ok, message = (self.hotkey.health() if self.hotkey else (False, "no hotkey"))
        lines.append(("✅ " if ok else "❌ ") + f"Hotkey: {message}")
        trusted = self.otto.services.mac.accessibility_trusted()
        lines.append(
            ("✅ " if trusted else "❌ ")
            + "Accessibility (needed to drive other apps' menus and buttons)"
        )
        lines.append("")
        lines.append(
            "Microphone is requested the first time you record. If no prompt "
            "appears, add your terminal under System Settings → Privacy & "
            "Security → Microphone."
        )
        self._alert("Otto — permissions", "\n".join(lines))

    def on_quit(self, _sender: Any = None) -> None:
        try:
            if self.hotkey:
                self.hotkey.stop()
            if self.voice:
                self.voice.shutdown()
            if self.console:
                self.console.stop()
            self.otto.close()
        finally:
            self._rumps.quit_application()

    # -- approvals ---------------------------------------------------------

    def _ask_approval(self, approval: Any) -> None:
        """Blocking modal on the main thread; the worker waits on the event."""
        window = self._rumps.alert(
            title="Otto needs your OK",
            message=f"{approval.reason}\n\nAgent: {approval.agent_id}\n"
                    f"Level: {approval.level}",
            ok="Allow",
            cancel="Deny",
        )
        approval.decide(bool(window))

    # -- helpers -----------------------------------------------------------

    def _model_label(self) -> str:
        status = self.otto.model_status()
        if not status["any_configured"]:
            return "Model: none (simple commands only)"
        strong = status["tiers"]["strong"]
        cloud = " ☁️" if status["cloud_tiers"] else ""
        return f"Model: {strong['model'] or strong['kind']}{cloud}"

    def _alert(self, title: str, message: str) -> None:
        try:
            self._rumps.alert(title=title, message=message)
        except Exception:
            pass

    def _notify(self, title: str, message: str) -> None:
        try:
            self.otto.services.mac.notify(title, message)
        except Exception:
            pass

    # -- run ---------------------------------------------------------------

    def run(self) -> None:
        app = self.build()
        self.otto.services.speak(self.otto.greeting())
        app.run()


def run_menubar(otto: "Otto") -> None:
    MenuBarApp(otto).run()
