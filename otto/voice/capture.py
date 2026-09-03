"""Microphone capture, behind an interface.

Push-to-talk only (DECISIONS D-05). The stream is opened when the user presses the
hotkey and closed when they release or press again — there is no always-on audio and
no wake-word tick, so idle CPU is genuinely zero and the microphone indicator is only
lit while Otto is actually listening.

`sounddevice` is imported inside `start()`, never at module level.
"""

from __future__ import annotations

import abc
import threading
import time
from typing import Any


class CaptureError(Exception):
    """The microphone could not be opened or read."""


class AudioCapture(abc.ABC):
    """Start/stop recording. `stop` returns the samples."""

    @abc.abstractmethod
    def start(self, sample_rate: int) -> None: ...

    @abc.abstractmethod
    def stop(self) -> Any:
        """Return captured samples (float32 mono, or a list in the fake)."""

    @property
    @abc.abstractmethod
    def recording(self) -> bool: ...

    @property
    def seconds(self) -> float:
        return 0.0


class FakeAudioCapture(AudioCapture):
    """Deterministic capture for tests: hands back whatever was queued."""

    def __init__(self, samples: Any = None, *, fail: Exception | None = None):
        self.samples = samples if samples is not None else [0.0] * 16000
        self.fail = fail
        self._recording = False
        self.started_at = 0.0
        self.starts = 0
        self.stops = 0
        self.sample_rate = 16000

    def start(self, sample_rate: int) -> None:
        if self.fail is not None:
            raise self.fail
        self.sample_rate = sample_rate
        self._recording = True
        self.started_at = time.time()
        self.starts += 1

    def stop(self) -> Any:
        self._recording = False
        self.stops += 1
        return self.samples

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def seconds(self) -> float:
        return len(self.samples) / max(self.sample_rate, 1)


class SoundDeviceCapture(AudioCapture):
    """Real microphone via `sounddevice` (PortAudio).

    macOS will prompt for Microphone permission the first time this runs. If the
    user declines, PortAudio returns silence rather than an error, so
    `looks_silent` lets the pipeline say something useful instead of transcribing
    nothing and blaming the model.
    """

    def __init__(self, max_seconds: float = 20.0):
        self.max_seconds = max_seconds
        self._stream: Any = None
        self._chunks: list[Any] = []
        self._lock = threading.Lock()
        self._recording = False
        self._started = 0.0
        self.sample_rate = 16000

    def start(self, sample_rate: int) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - needs the wheel
            raise CaptureError(
                "sounddevice is not installed — run ./setup.sh, or use the text box."
            ) from exc

        with self._lock:
            if self._recording:
                return
            self._chunks = []
            self.sample_rate = sample_rate
            self._np = np
            # Mark recording *before* the stream starts: the callback runs on
            # PortAudio's thread and may fire the instant the stream opens.
            self._recording = True
            self._started = time.time()

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            # Audio callbacks must not block. This takes the lock only to append
            # an already-copied block, and never calls anything that could wait.
            with self._lock:
                if self._recording:
                    self._chunks.append(indata.copy())

        # Opening and starting the stream happen OUTSIDE the lock. Holding it here
        # would stall the audio thread on its very first callback — and with a
        # non-reentrant lock it deadlocks outright if the callback is synchronous.
        try:
            stream = sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=1024,
                callback=callback,
            )
            stream.start()
        except Exception as exc:
            with self._lock:
                self._recording = False
            raise CaptureError(
                f"could not open the microphone: {exc}. Grant Microphone access "
                "in System Settings → Privacy & Security → Microphone."
            ) from exc

        with self._lock:
            self._stream = stream

    def stop(self) -> Any:
        with self._lock:
            self._recording = False
            stream, self._stream = self._stream, None
            chunks, self._chunks = self._chunks, []
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # pragma: no cover
                pass
        if not chunks:
            return []
        return self._np.concatenate(chunks, axis=0).reshape(-1)

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def seconds(self) -> float:
        return (time.time() - self._started) if self._recording else 0.0


def looks_silent(samples: Any, threshold: float = 0.002) -> bool:
    """True when the capture is effectively silence — usually a denied Microphone
    permission, occasionally a muted input. Worth saying out loud either way."""
    try:
        import numpy as np

        array = np.asarray(samples, dtype="float32")
        if array.size == 0:
            return True
        return bool(float(np.abs(array).mean()) < threshold)
    except ImportError:
        values = list(samples or [])
        if not values:
            return True
        return sum(abs(float(v)) for v in values) / len(values) < threshold


def build_capture(config: Any) -> AudioCapture:
    return SoundDeviceCapture(max_seconds=getattr(config, "max_recording_seconds", 20.0))
