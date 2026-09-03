"""Voice pipeline, the shared entry point, the app object and the console.

Includes the budget tests: nothing heavy may be imported at start-up, and the ASR
model must be lazy and must unload when idle.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from otto.app import ERROR, IDLE, LISTENING, Otto
from otto.core.state import Status
from otto.voice.asr import FakeTranscriber, FasterWhisperTranscriber, TranscriptionError
from otto.voice.capture import FakeAudioCapture, looks_silent
from otto.voice.pipeline import VoicePipeline


@pytest.fixture
def voice(otto: Otto):
    capture = FakeAudioCapture([0.4, -0.4] * 8000)
    transcriber = FakeTranscriber(["open Safari"])
    return VoicePipeline(otto, capture, transcriber), capture, transcriber


# -- the shared pipeline ----------------------------------------------------


def test_voice_reaches_the_same_entry_point_as_text(voice, otto):
    pipeline, _, _ = voice
    seen: list = []
    original = otto.handle_utterance

    def spy(text, *, source="text"):
        seen.append((text, source))
        return original(text, source=source)

    otto.handle_utterance = spy  # type: ignore[method-assign]

    pipeline.start()
    task = pipeline.stop_and_run()

    assert seen == [("open Safari", "voice")]
    assert task is not None and task.status is Status.COMPLETED
    assert otto.services.mac.frontmost_app() == "Safari"


def test_text_and_voice_produce_identical_task_shapes(otto):
    typed = otto.handle_utterance("open Safari", source="text")
    otto.services.mac.frontmost = "Finder"
    pipeline = VoicePipeline(
        otto, FakeAudioCapture([0.4, -0.4] * 8000), FakeTranscriber(["open Safari"])
    )
    pipeline.start()
    spoken = pipeline.stop_and_run()

    assert typed.status is spoken.status
    assert [s.description for s in typed.subtasks] == [
        s.description for s in spoken.subtasks
    ]
    assert typed.source == "text" and spoken.source == "voice"


def test_toggle_starts_then_sends(voice):
    pipeline, capture, _ = voice
    pipeline.toggle()
    assert pipeline.recording and capture.starts == 1
    pipeline.toggle()
    assert not pipeline.recording and capture.stops == 1


def test_cancelling_a_recording_runs_nothing(voice, otto):
    pipeline, capture, _ = voice
    pipeline.start()
    pipeline.cancel()
    assert not pipeline.recording
    assert otto.state == IDLE
    assert otto.tasks == []


def test_silence_names_the_microphone_permission(otto):
    pipeline = VoicePipeline(
        otto, FakeAudioCapture([0.0] * 16000), FakeTranscriber(["should not be used"])
    )
    pipeline.start()
    assert pipeline.stop_and_run() is None
    assert otto.state == ERROR
    assert "Microphone" in otto.last_error
    assert otto.tasks == []


def test_an_empty_transcript_does_not_run_a_task(otto):
    pipeline = VoicePipeline(
        otto, FakeAudioCapture([0.4, -0.4] * 8000), FakeTranscriber([""])
    )
    pipeline.start()
    assert pipeline.stop_and_run() is None
    assert otto.tasks == []
    assert "didn't catch that" in " ".join(otto.services.mac.spoken)


def test_a_transcription_failure_is_reported_not_swallowed(otto):
    class Broken(FakeTranscriber):
        def transcribe(self, samples, sample_rate):
            raise TranscriptionError("faster-whisper is not installed")

    pipeline = VoicePipeline(otto, FakeAudioCapture([0.4] * 16000), Broken())
    pipeline.start()
    assert pipeline.stop_and_run() is None
    assert otto.state == ERROR
    assert "faster-whisper" in otto.last_error


def test_a_microphone_failure_is_reported(otto):
    from otto.voice.capture import CaptureError

    capture = FakeAudioCapture(fail=CaptureError("could not open the microphone"))
    pipeline = VoicePipeline(otto, capture, FakeTranscriber(["x"]))
    assert pipeline.start() is False
    assert otto.state == ERROR


def test_stop_without_start_is_a_no_op(voice):
    pipeline, _, _ = voice
    assert pipeline.stop_and_run() is None


def test_the_recording_state_is_visible_to_the_ui(voice, otto):
    pipeline, _, _ = voice
    pipeline.start()
    assert otto.state == LISTENING


def test_the_recording_times_out_by_itself(otto):
    otto.services.config.max_recording_seconds = 0.1
    pipeline = VoicePipeline(
        otto, FakeAudioCapture([0.4, -0.4] * 8000), FakeTranscriber(["open Safari"])
    )
    pipeline.start()
    for _ in range(50):
        if not pipeline.recording:
            break
        time.sleep(0.02)
    assert not pipeline.recording, "the recording never stopped on its own"


def test_looks_silent():
    assert looks_silent([])
    assert looks_silent([0.0] * 100)
    assert not looks_silent([0.5, -0.5] * 100)


# -- the ASR budget ---------------------------------------------------------


def test_the_model_is_not_loaded_until_it_is_used():
    transcriber = FasterWhisperTranscriber(model_size="tiny")
    assert transcriber.loaded is False
    assert transcriber._model is None


def test_the_model_is_preloaded_while_the_user_is_still_speaking(voice):
    pipeline, _, transcriber = voice
    pipeline.start()
    for _ in range(50):
        if transcriber.loaded:
            break
        time.sleep(0.01)
    assert transcriber.loaded, "the model was not preloaded during recording"


def test_the_model_unloads_on_an_idle_timer():
    transcriber = FasterWhisperTranscriber(model_size="tiny", idle_unload_seconds=0.05)
    transcriber._model = object()  # stand in for a loaded model
    transcriber._schedule_unload()
    for _ in range(60):
        if not transcriber.loaded:
            break
        time.sleep(0.02)
    assert not transcriber.loaded, "the ASR model stayed resident while idle"


def test_unloading_cancels_the_timer_rather_than_polling():
    transcriber = FasterWhisperTranscriber(idle_unload_seconds=60)
    transcriber._model = object()
    transcriber._schedule_unload()
    assert transcriber._timer is not None
    transcriber.unload()
    assert transcriber._timer is None
    assert not transcriber.loaded


def test_shutdown_releases_the_model(voice):
    pipeline, _, transcriber = voice
    pipeline.start()
    pipeline.stop_and_run()
    pipeline.shutdown()
    assert transcriber.loaded is False


# -- the cold-start budget --------------------------------------------------

HEAVY = ("faster_whisper", "torch", "transformers", "numpy", "sounddevice", "rumps",
         "pynput", "ctranslate2")


def test_importing_otto_does_not_pull_in_anything_heavy():
    """Cold start must stay under 3 s, which means importing nothing expensive.

    Run in a subprocess so an earlier test's imports cannot mask a regression.
    """
    code = (
        "import sys, otto.app, otto.services, otto.agentloop.supervisor;"
        f"heavy=[m for m in {HEAVY!r} if m in sys.modules];"
        "print(','.join(heavy))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"heavy imports at start-up: {result.stdout}"


def test_the_core_imports_quickly():
    code = "import time; t=time.time(); import otto.app; print(time.time()-t)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout) < 1.0, "importing otto.app is already too slow"


# -- the app object ---------------------------------------------------------


def test_greeting_is_honest_about_having_no_model(otto):
    greeting = otto.greeting()
    assert "No language model is configured" in greeting
    assert "open Safari" in greeting


def test_greeting_names_cloud_use(otto):
    from otto.config import ProviderConfig

    otto.services.config.providers["strong"] = ProviderConfig(
        kind="groq", model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
    )
    assert "cloud" in otto.greeting()
    assert otto.model_status()["cloud_tiers"] == ["strong"]


def test_a_local_model_is_not_reported_as_cloud(otto):
    from otto.config import ProviderConfig

    otto.services.config.providers["fast"] = ProviderConfig(
        kind="ollama", model="qwen2.5:3b", base_url="http://127.0.0.1:11434"
    )
    assert otto.model_status()["cloud_tiers"] == []
    assert "on this Mac" in otto.greeting()


def test_empty_input_is_handled(otto):
    task = otto.handle_utterance("   ")
    assert task.status is Status.FAILED
    assert "didn't catch" in task.summary


def test_state_hooks_fire(otto):
    seen: list[str] = []
    otto.on_state_change(lambda state, app: seen.append(state))
    otto.handle_utterance("open Safari")
    assert "thinking" in seen and "idle" in seen


def test_a_broken_state_hook_cannot_break_a_run(otto):
    otto.on_state_change(lambda state, app: 1 / 0)
    task = otto.handle_utterance("open Safari")
    assert task.status is Status.COMPLETED


def test_submit_runs_off_the_calling_thread(otto):
    done = threading.Event()
    thread = otto.submit("open Safari", done=lambda t: done.set())
    assert done.wait(timeout=10)
    thread.join(timeout=5)
    assert otto.services.mac.frontmost_app() == "Safari"


def test_cancel_returns_false_when_nothing_is_running(otto):
    assert otto.cancel() is False


def test_history_is_capped(otto):
    for i in range(60):
        otto.handle_utterance("open Safari")
    assert len(otto.tasks) <= 50


def test_snapshot_shape(otto):
    otto.handle_utterance("open Safari")
    snapshot = otto.snapshot()
    assert snapshot["state"] in (IDLE, ERROR)
    assert snapshot["current"]["request"] == "open Safari"
    assert any(a["id"] == "research" and a["ceiling"] == "SAFE"
               for a in snapshot["agents"])
    assert "open_app" in snapshot["tools"]
    assert snapshot["mac_bridge"] == "FakeMac"
    assert snapshot["models"]["any_configured"] is False
    json.dumps(snapshot, default=str)  # must be serialisable for the console


def test_an_exception_in_the_supervisor_does_not_escape(otto, monkeypatch):
    def boom(task):
        raise RuntimeError("supervisor exploded")

    monkeypatch.setattr(otto.supervisor, "run", boom)
    task = otto.handle_utterance("open Safari")
    assert task.status is Status.FAILED
    assert "supervisor exploded" in task.error
    assert otto.state == ERROR


# -- the developer console --------------------------------------------------


@pytest.fixture
def console(otto):
    from otto.ui.console import DevConsole

    console = DevConsole(otto, port=0)
    console.start()
    console.port = console._server.server_address[1]
    yield console
    console.stop()


def _get(console, path, *, raise_for_status=False):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{console.port}{path}", timeout=5
        ) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        if raise_for_status:
            raise
        return exc.code, exc.read().decode()


def _post(console, path, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{console.port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_the_console_binds_only_to_loopback(console):
    assert console._server.server_address[0] == "127.0.0.1"


def test_the_page_loads(console):
    status, body = _get(console, "/")
    assert status == 200
    assert "developer console" in body
    assert console.token in body


def test_the_snapshot_needs_the_token(console):
    status, body = _get(console, "/api/snapshot?token=wrong")
    assert status == 403
    assert "bad token" in body
    status, body = _get(console, f"/api/snapshot?token={console.token}")
    assert status == 200
    assert "agents" in json.loads(body)


def test_memory_is_editable_and_deletable_from_the_console(console, otto):
    status, body = _post(console, "/api/memory/set",
                         {"token": console.token, "key": "projects",
                          "value": "my projects live in ~/Projects"})
    assert status == 200 and body["saved"]["key"] == "projects"

    memory_id = body["saved"]["id"]
    status, body = _post(console, "/api/memory/delete",
                         {"token": console.token, "id": memory_id})
    assert body["deleted"] is True
    assert otto.services.memory.get("projects") is None


def test_the_console_refuses_to_store_a_secret(console):
    status, body = _post(
        console, "/api/memory/set",
        {"token": console.token, "key": "k",
         "value": "sk-" + "abcdefghijklmnopqrstuvwxyz0123"},
    )
    assert status == 400
    assert "credential" in body["error"]


def test_writes_need_the_token(console, otto):
    otto.services.memory.remember("a", "one")
    status, body = _post(console, "/api/memory/delete", {"token": "guess", "id": 1})
    assert status == 403
    assert otto.services.memory.get("a") is not None


def test_there_is_no_route_that_executes_anything(console):
    for path in ("/api/run", "/api/command", "/api/exec", "/api/shell", "/api/tool"):
        status, body = _post(console, path, {"token": console.token, "argv": ["id"]})
        assert status == 404


def test_unknown_get_routes_404(console):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(console, "/nonsense", raise_for_status=True)
    assert exc.value.code == 404
