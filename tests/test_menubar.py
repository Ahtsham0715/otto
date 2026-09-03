"""The menu bar and the hotkey, against stub `rumps` and `pynput` modules.

This cannot prove anything about how macOS behaves. What it *can* prove is that
`MenuBarApp` and `HotkeyManager` are correct Python: that every menu callback
exists and runs, that the seven UI states map to titles, that the approval modal
decides the approval, and that quitting releases the hotkey, the pipeline and the
console. Those are the failures that would otherwise show up as a traceback the
first time the user runs `./run.sh` — and they are exactly the failures I could
not otherwise catch without a Mac.

Everything about rumps' *actual* behaviour remains unverified; see STATUS.md §3.
"""

from __future__ import annotations

import sys
import types

import pytest

from otto.app import (
    ERROR,
    EXECUTING,
    IDLE,
    LISTENING,
    SPEAKING,
    THINKING,
    WAITING,
    Otto,
)
from otto.core.state import Approval


# -- the stubs --------------------------------------------------------------


class FakeMenuItem:
    def __init__(self, title="", callback=None):
        self.title = title
        self.callback = callback

    def set_callback(self, callback):
        self.callback = callback


class FakeApp:
    def __init__(self, name, title="", quit_button=None):
        self.name = name
        self.title = title
        self.quit_button = quit_button
        self.menu: list = []
        self.ran = False

    def run(self):
        self.ran = True


class FakeWindowResponse:
    def __init__(self, clicked, text):
        self.clicked = clicked
        self.text = text


class FakeWindow:
    #: What the next Window().run() returns; set by a test.
    next_response = FakeWindowResponse(0, "")

    def __init__(self, **kw):
        self.kw = kw

    def run(self):
        return FakeWindow.next_response


@pytest.fixture
def rumps(monkeypatch):
    """Install a stub `rumps` module for the duration of one test."""
    module = types.ModuleType("rumps")
    module.App = FakeApp
    module.MenuItem = FakeMenuItem
    module.Window = FakeWindow
    module.alerts = []
    module.quit_calls = []

    def alert(title="", message="", ok=None, cancel=None):
        module.alerts.append((title, message, ok, cancel))
        return module.alert_returns

    module.alert = alert
    module.alert_returns = 1
    module.quit_application = lambda: module.quit_calls.append(True)
    module.notification = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "rumps", module)
    FakeWindow.next_response = FakeWindowResponse(0, "")
    return module


@pytest.fixture
def bar(otto: Otto, rumps):
    from otto.ui.menubar import MenuBarApp

    app = MenuBarApp(otto)
    app.build()
    return app


# -- construction -----------------------------------------------------------


def test_the_menu_bar_builds(bar, rumps):
    assert isinstance(bar.app, FakeApp)
    assert bar.app.title == "🎙"
    assert bar.app.quit_button is None, "Otto supplies its own Quit so it can clean up"
    assert bar.voice is not None
    assert bar.hotkey is not None


def test_every_menu_entry_has_a_title_and_a_callback(bar):
    items = [i for i in bar.app.menu if i is not None]
    assert len(items) >= 9
    for item in items:
        assert item.title
    # The status line is the only deliberately inert entry.
    inert = [i for i in items if i.callback is None]
    assert [i.title for i in inert] == ["Ready"]


def test_the_hotkey_is_named_in_the_menu(bar, otto):
    titles = [i.title for i in bar.app.menu if i is not None]
    assert any(otto.services.config.hotkey in t for t in titles)


def test_the_menu_says_when_no_model_is_configured(bar):
    titles = [i.title for i in bar.app.menu if i is not None]
    assert any("Model: none" in t for t in titles)


def test_the_menu_flags_a_cloud_model(otto, rumps):
    from otto.config import ProviderConfig
    from otto.ui.menubar import MenuBarApp

    otto.services.config.providers["strong"] = ProviderConfig(
        kind="groq", model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
    )
    app = MenuBarApp(otto)
    app.build()
    titles = [i.title for i in app.app.menu if i is not None]
    assert any("☁️" in t for t in titles)


# -- the seven states -------------------------------------------------------


@pytest.mark.parametrize(
    "state,icon",
    [
        (IDLE, "🎙"),
        (LISTENING, "🔴"),
        (THINKING, "💭"),
        (EXECUTING, "⚙️"),
        (WAITING, "❓"),
        (SPEAKING, "🔊"),
        (ERROR, "⚠️"),
    ],
)
def test_each_ui_state_has_its_own_icon(bar, otto, state, icon):
    otto.set_state(state)
    assert bar.app.title == icon
    assert bar.status_item.title


def test_the_state_line_names_the_active_agents(bar, otto):
    from otto.core.state import Status, Subtask, Task

    task = Task(request="open Safari and Notes")
    running = Subtask(description="open", agent_id="mac")
    running.set_status(Status.RUNNING)
    task.subtasks.append(running)
    otto.current = task
    otto.set_state(EXECUTING)
    assert "mac" in bar.status_item.title


def test_the_error_state_shows_the_error(bar, otto):
    otto.last_error = "Accessibility permission has not been granted"
    otto.set_state(ERROR)
    assert "Accessibility" in bar.status_item.title


# -- callbacks --------------------------------------------------------------


def test_typing_a_command_runs_it(bar, otto, rumps):
    FakeWindow.next_response = FakeWindowResponse(1, "open Safari")
    bar.on_type()
    for _ in range(200):
        if otto.services.mac.frontmost_app() == "Safari":
            break
        import time

        time.sleep(0.01)
    assert otto.services.mac.frontmost_app() == "Safari"


def test_cancelling_the_type_window_runs_nothing(bar, otto):
    FakeWindow.next_response = FakeWindowResponse(0, "open Safari")
    bar.on_type()
    assert otto.tasks == []


def test_talk_toggles_the_recording(bar, otto):
    from otto.voice.asr import FakeTranscriber
    from otto.voice.capture import FakeAudioCapture
    from otto.voice.pipeline import VoicePipeline

    bar.voice = VoicePipeline(
        otto, FakeAudioCapture([0.4, -0.4] * 8000), FakeTranscriber(["open Safari"])
    )
    bar.on_talk()
    assert bar.voice.recording
    bar.on_talk()
    assert not bar.voice.recording
    assert otto.services.mac.frontmost_app() == "Safari"


def test_cancel_stops_a_recording_before_a_task(bar, otto):
    from otto.voice.asr import FakeTranscriber
    from otto.voice.capture import FakeAudioCapture
    from otto.voice.pipeline import VoicePipeline

    bar.voice = VoicePipeline(otto, FakeAudioCapture(), FakeTranscriber(["x"]))
    bar.voice.start()
    bar.on_cancel()
    assert not bar.voice.recording
    assert otto.tasks == []


def test_the_informational_menu_items_all_run(bar, otto, rumps):
    otto.handle_utterance("open Safari")
    for callback in (bar.on_last_result, bar.on_recent, bar.on_models,
                     bar.on_permissions):
        callback()
    assert len(rumps.alerts) == 4
    joined = " ".join(a[1] for a in rumps.alerts)
    assert "open Safari" in joined
    assert "No model is configured" in joined


def test_last_result_before_anything_has_run(bar, rumps):
    bar.on_last_result()
    assert "Nothing has run yet." in rumps.alerts[-1][1]


def test_the_permissions_item_reports_the_hotkey_state(bar, rumps):
    bar.on_permissions()
    body = rumps.alerts[-1][1]
    assert "Hotkey" in body
    assert "Accessibility" in body
    assert "Microphone" in body


def test_opening_the_console_opens_a_loopback_url(bar, otto, rumps):
    bar.on_console()
    try:
        assert bar.console is not None and bar.console.running
        opened = otto.services.mac.windows.get(otto.services.mac.frontmost_app()) or []
        assert any("127.0.0.1" in str(w) for w in opened)
    finally:
        bar.console.stop()


def test_memory_opens_the_console_where_rows_are_visible(bar, otto):
    bar.on_memory()
    try:
        assert bar.console is not None and bar.console.running
    finally:
        bar.console.stop()


# -- approvals --------------------------------------------------------------


def test_the_approval_modal_grants(bar, rumps):
    rumps.alert_returns = 1
    approval = Approval(tool="make_folder", args={"path": "/x"}, agent_id="files",
                        level="CONFIRM", reason="Create the folder /x?")
    bar._ask_approval(approval)
    assert approval.granted is True
    assert "Create the folder /x?" in rumps.alerts[-1][1]
    assert "files" in rumps.alerts[-1][1]


def test_the_approval_modal_denies(bar, rumps):
    rumps.alert_returns = 0
    approval = Approval(tool="move_to_trash", args={}, agent_id="files",
                        level="ALWAYS_CONFIRM", reason="Trash it?")
    bar._ask_approval(approval)
    assert approval.granted is False


def test_the_approval_hook_is_wired_to_the_broker(bar, otto, rumps, home):
    """End to end: a CONFIRM tool raises the modal and the modal's answer lands."""
    rumps.alert_returns = 1
    otto.services.broker.set_auto(None)  # use the real hook, not the test shortcut
    task = otto.handle_utterance("create a folder called FromMenu on my Desktop")
    assert task.status.value == "COMPLETED"
    assert (home / "Desktop" / "FromMenu").is_dir()
    assert any("FromMenu" in a[1] for a in rumps.alerts)


# -- shutdown ---------------------------------------------------------------


def test_quitting_releases_everything(bar, otto, rumps):
    bar.on_console()
    console = bar.console
    bar.on_quit()
    assert rumps.quit_calls == [True]
    assert not console.running
    assert bar.hotkey._listener is None


def test_run_starts_the_app_and_greets(otto, rumps):
    from otto.ui.menubar import MenuBarApp

    app = MenuBarApp(otto)
    app.run()
    assert app.app.ran
    assert otto.services.mac.spoken, "Otto said nothing on launch"


# -- the hotkey -------------------------------------------------------------


def test_the_hotkey_reports_a_missing_pynput(monkeypatch, otto):
    from otto.ui.hotkey import HotkeyManager

    monkeypatch.setitem(sys.modules, "pynput", None)
    manager = HotkeyManager("<ctrl>+<alt>+space", lambda: None)
    assert manager.start() is False
    assert "pynput" in manager.error
    ok, message = manager.health()
    assert ok is False and message


def test_the_hotkey_registers_and_fires(monkeypatch, otto):
    from otto.ui.hotkey import HotkeyManager

    fired: list = []
    registered: dict = {}

    class FakeListener:
        def __init__(self, mapping):
            registered.update(mapping)
            self.daemon = False
            self.stopped = False

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    keyboard = types.SimpleNamespace(GlobalHotKeys=FakeListener)
    monkeypatch.setitem(
        sys.modules, "pynput", types.SimpleNamespace(keyboard=keyboard)
    )

    manager = HotkeyManager("<ctrl>+<alt>+space", lambda: fired.append(1))
    assert manager.start() is True
    assert manager.active
    assert "<ctrl>+<alt>+space" in registered

    registered["<ctrl>+<alt>+space"]()
    assert fired == [1] and manager.triggered == 1

    manager.stop()
    assert manager._listener is None


def test_a_failing_hotkey_handler_does_not_kill_the_listener(monkeypatch):
    from otto.ui.hotkey import HotkeyManager

    registered: dict = {}

    class FakeListener:
        def __init__(self, mapping):
            registered.update(mapping)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "pynput",
        types.SimpleNamespace(keyboard=types.SimpleNamespace(GlobalHotKeys=FakeListener)),
    )

    def explode():
        raise RuntimeError("boom")

    manager = HotkeyManager("<ctrl>+<alt>+space", explode)
    manager.start()
    registered["<ctrl>+<alt>+space"]()  # must not propagate
    assert "boom" in manager.error


def test_registration_failure_explains_the_permissions(monkeypatch):
    from otto.ui.hotkey import PERMISSION_HELP, HotkeyManager

    class Exploding:
        def __init__(self, mapping):
            raise RuntimeError("cannot tap the event stream")

    monkeypatch.setitem(
        sys.modules,
        "pynput",
        types.SimpleNamespace(keyboard=types.SimpleNamespace(GlobalHotKeys=Exploding)),
    )
    manager = HotkeyManager("<ctrl>+<alt>+space", lambda: None)
    assert manager.start() is False
    assert "Input Monitoring" in manager.error
    assert PERMISSION_HELP in manager.error
