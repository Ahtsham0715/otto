"""Speech to text.

The budget drives every choice here (docs/RESEARCH.md §2, DECISIONS D-03/D-04):

* `faster-whisper` (CTranslate2, int8) — **no torch**, which has no macOS x86_64
  wheel anyway.
* `base` by default, `tiny` for the truly impatient. Never `small`, never `large`.
* The model is **loaded on first use and unloaded after 5 minutes idle**, via a
  one-shot timer rather than a polling thread. A resident ASR model would eat most
  of the 250 MB idle budget on its own.
* Nothing in this module is imported at start-up; `faster_whisper` is imported
  inside `_ensure_model`.
"""

from __future__ import annotations

import abc
import threading
import time
from dataclasses import dataclass
from typing import Any


class TranscriptionError(Exception):
    """Recording could not be turned into text."""


@dataclass
class Transcript:
    text: str
    seconds: float = 0.0
    latency: float = 0.0
    model: str = ""

    @property
    def real_time_factor(self) -> float:
        return (self.seconds / self.latency) if self.latency else 0.0


class Transcriber(abc.ABC):
    """The ASR boundary. Two implementations: faster-whisper, and a fake."""

    @abc.abstractmethod
    def transcribe(self, samples: Any, sample_rate: int) -> Transcript: ...

    def preload(self) -> None:
        """Optional: start loading before the user stops speaking."""

    def unload(self) -> None:
        """Release the model. Must be safe to call at any time."""

    @property
    def loaded(self) -> bool:
        return False


class FakeTranscriber(Transcriber):
    """Returns queued text. The whole voice pipeline is tested through this."""

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.calls: list[tuple[int, int]] = []
        self._loaded = False
        self.load_count = 0
        self.unload_count = 0

    def queue(self, text: str) -> None:
        self.replies.append(text)

    def transcribe(self, samples: Any, sample_rate: int) -> Transcript:
        self.preload()
        length = len(samples) if hasattr(samples, "__len__") else 0
        self.calls.append((length, sample_rate))
        if not self.replies:
            return Transcript(text="", seconds=length / max(sample_rate, 1))
        return Transcript(
            text=self.replies.pop(0),
            seconds=length / max(sample_rate, 1),
            latency=0.01,
            model="fake",
        )

    def preload(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.load_count += 1

    def unload(self) -> None:
        if self._loaded:
            self._loaded = False
            self.unload_count += 1

    @property
    def loaded(self) -> bool:
        return self._loaded


class FasterWhisperTranscriber(Transcriber):
    """CTranslate2 int8 Whisper, loaded lazily and dropped when idle."""

    def __init__(
        self,
        model_size: str = "base",
        *,
        compute_type: str = "int8",
        language: str = "en",
        idle_unload_seconds: float = 300.0,
        cpu_threads: int = 4,
    ):
        self.model_size = model_size
        self.compute_type = compute_type
        self.language = language
        self.idle_unload_seconds = idle_unload_seconds
        # Leave headroom: saturating every core on a 2019 i9 is what makes the fans
        # audible, and the model is fast enough at 4 threads.
        self.cpu_threads = cpu_threads
        self._model: Any = None
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self.last_used: float = 0.0

    # -- model lifecycle ---------------------------------------------------

    def _ensure_model(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel  # imported here on purpose
            except ImportError as exc:
                raise TranscriptionError(
                    "faster-whisper is not installed. Run ./setup.sh, or set "
                    "asr_model to 'none' in ~/.otto/config.json to use Otto by "
                    "text only."
                ) from exc
            except OSError as exc:
                # CTranslate2 is a compiled extension; a missing or mismatched
                # system library surfaces as OSError, which must not escape as a
                # traceback out of the hotkey handler.
                raise TranscriptionError(
                    f"the speech engine could not load ({exc}). Try ./setup.sh "
                    "again; Otto still works from the text box."
                ) from exc
            started = time.time()
            self._model = WhisperModel(
                self.model_size,
                device="cpu",  # there is no usable GPU on this machine
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
            )
            self.load_seconds = time.time() - started
            return self._model

    def preload(self) -> None:
        """Called when recording *starts*, so the load overlaps with the user
        speaking instead of adding to the measured transcribe latency."""
        try:
            self._ensure_model()
        except TranscriptionError:
            pass

    def unload(self) -> None:
        with self._lock:
            self._model = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule_unload(self) -> None:
        """One-shot timer, not a polling loop — the idle-CPU budget is 0%."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            if self.idle_unload_seconds <= 0:
                return
            self._timer = threading.Timer(self.idle_unload_seconds, self.unload)
            self._timer.daemon = True
            self._timer.start()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # -- transcription -----------------------------------------------------

    def transcribe(self, samples: Any, sample_rate: int) -> Transcript:
        model = self._ensure_model()
        audio = _as_float32_mono(samples)
        started = time.time()
        try:
            segments, info = model.transcribe(
                audio,
                language=self.language or None,
                beam_size=1,  # greedy: measurably cheaper, fine for short commands
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:  # pragma: no cover - needs the wheel
            raise TranscriptionError(f"transcription failed: {exc}") from exc
        finally:
            self.last_used = time.time()
            self._schedule_unload()
        return Transcript(
            text=text,
            seconds=getattr(info, "duration", len(audio) / max(sample_rate, 1)),
            latency=time.time() - started,
            model=f"faster-whisper:{self.model_size}:{self.compute_type}",
        )


def _as_float32_mono(samples: Any) -> Any:
    """Accept whatever the capture layer produced and hand Whisper float32 mono."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy ships with faster-whisper
        return samples
    array = np.asarray(samples)
    if array.ndim > 1:
        array = array.mean(axis=1)
    if array.dtype != np.float32:
        if array.dtype.kind == "i":
            array = array.astype(np.float32) / float(1 << (8 * array.dtype.itemsize - 1))
        else:
            array = array.astype(np.float32)
    return array


def build_transcriber(config: Any) -> Transcriber:
    """Pick an ASR backend from config. `none` disables voice entirely."""
    model = getattr(config, "asr_model", "base")
    if model in ("none", ""):
        return FakeTranscriber([])
    return FasterWhisperTranscriber(
        model_size=model,
        compute_type=getattr(config, "asr_compute_type", "int8"),
        language=getattr(config, "asr_language", "en"),
        idle_unload_seconds=getattr(config, "asr_idle_unload_seconds", 300.0),
    )
