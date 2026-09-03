"""The voice pipeline.

Press → record → transcribe → **`Otto.handle_utterance`** → speak. That middle arrow
is the whole point: voice does not get its own understanding, its own planner or its
own tools. It produces a string and hands it to exactly the method the text box calls
(DECISIONS D-24).

Both halves are injected, so this entire file is exercised on Linux with a fake
microphone and a fake transcriber.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..core.state import Task
from .asr import Transcriber, TranscriptionError, Transcript
from .capture import AudioCapture, CaptureError, looks_silent

if TYPE_CHECKING:
    from ..app import Otto


class VoicePipeline:
    """Push-to-talk. `toggle` mode: press to start, press again to send."""

    def __init__(
        self,
        otto: "Otto",
        capture: AudioCapture | None = None,
        transcriber: Transcriber | None = None,
        *,
        on_transcript: Callable[[str], None] | None = None,
    ):
        self.otto = otto
        self.config = otto.services.config
        self._capture = capture
        self._transcriber = transcriber
        self.on_transcript = on_transcript
        self.last_transcript: Transcript | None = None
        self._lock = threading.RLock()
        self._timeout: threading.Timer | None = None

    # -- lazily built, so nothing is imported until the first press ---------

    @property
    def capture(self) -> AudioCapture:
        if self._capture is None:
            from .capture import build_capture

            self._capture = build_capture(self.config)
        return self._capture

    @property
    def transcriber(self) -> Transcriber:
        if self._transcriber is None:
            from .asr import build_transcriber

            self._transcriber = build_transcriber(self.config)
        return self._transcriber

    @property
    def recording(self) -> bool:
        return self._capture is not None and self._capture.recording

    # -- push to talk ------------------------------------------------------

    def toggle(self) -> None:
        """What the hotkey calls in `toggle` mode."""
        if self.recording:
            self.stop_and_run()
        else:
            self.start()

    def start(self) -> bool:
        from ..app import ERROR, LISTENING

        with self._lock:
            if self.recording:
                return False
            try:
                self.capture.start(self.config.sample_rate)
            except CaptureError as exc:
                self.otto.last_error = str(exc)
                self.otto.set_state(ERROR)
                self.otto.services.speak(str(exc))
                return False
            self.otto.set_state(LISTENING)
            # Load the model while the user is still speaking, so the cold load
            # does not land inside the measured press-to-transcript latency.
            threading.Thread(
                target=self.transcriber.preload, name="otto-asr-preload", daemon=True
            ).start()
            self._arm_timeout()
            return True

    def _arm_timeout(self) -> None:
        limit = float(self.config.max_recording_seconds or 0)
        if limit <= 0:
            return
        if self._timeout is not None:
            self._timeout.cancel()
        self._timeout = threading.Timer(limit, self._timeout_fired)
        self._timeout.daemon = True
        self._timeout.start()

    def _timeout_fired(self) -> None:
        if self.recording:
            self.stop_and_run()

    def cancel(self) -> None:
        """Throw the recording away without running anything."""
        from ..app import IDLE

        with self._lock:
            if self._timeout is not None:
                self._timeout.cancel()
                self._timeout = None
            if self.recording:
                self.capture.stop()
            self.otto.set_state(IDLE)

    def stop_and_run(self) -> Task | None:
        """Stop recording, transcribe, and run it through the shared entry point."""
        from ..app import ERROR, IDLE, THINKING

        with self._lock:
            if self._timeout is not None:
                self._timeout.cancel()
                self._timeout = None
            if not self.recording:
                return None
            samples = self.capture.stop()

        self.otto.set_state(THINKING)

        if looks_silent(samples):
            message = (
                "I didn't hear anything. If macOS hasn't asked for microphone "
                "access yet, grant it in System Settings → Privacy & Security → "
                "Microphone."
            )
            self.otto.last_error = message
            self.otto.set_state(ERROR)
            self.otto.services.speak(message)
            return None

        try:
            transcript = self.transcriber.transcribe(samples, self.config.sample_rate)
        except TranscriptionError as exc:
            self.otto.last_error = str(exc)
            self.otto.set_state(ERROR)
            self.otto.services.speak(str(exc))
            return None

        self.last_transcript = transcript
        text = (transcript.text or "").strip()
        if self.on_transcript is not None:
            try:
                self.on_transcript(text)
            except Exception:
                pass

        if not text:
            self.otto.set_state(IDLE)
            self.otto.services.speak("I didn't catch that.")
            return None

        # The one and only entry point — identical to what the text box calls.
        return self.otto.handle_utterance(text, source="voice")

    def shutdown(self) -> None:
        if self._timeout is not None:
            self._timeout.cancel()
        if self._transcriber is not None:
            self._transcriber.unload()
