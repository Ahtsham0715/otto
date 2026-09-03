"""The real ASR and capture code paths, against stub wheels.

`FasterWhisperTranscriber.transcribe` and `SoundDeviceCapture` are the two places
where Otto talks to a wheel that is not installed in this environment. Without
these tests their Python is never executed at all, so a typo would surface as a
traceback the first time the user pressed the hotkey.

What is proven here: the argument shapes Otto passes, the audio conversion, the
segment joining, the lazy-load and unload behaviour, and the error handling.
What is *not* proven: that faster-whisper transcribes correctly or that PortAudio
opens a device — see STATUS.md §3.
"""

from __future__ import annotations

import sys
import types

import pytest

from otto.voice.asr import (
    FasterWhisperTranscriber,
    TranscriptionError,
    _as_float32_mono,
    build_transcriber,
)
from otto.voice.capture import CaptureError, SoundDeviceCapture, build_capture

numpy = pytest.importorskip("numpy", reason="numpy ships with faster-whisper")


# -- audio conversion (real numpy, no stub) ---------------------------------


def test_float32_audio_passes_through_unchanged():
    audio = numpy.array([0.1, -0.2, 0.3], dtype=numpy.float32)
    out = _as_float32_mono(audio)
    assert out.dtype == numpy.float32
    assert numpy.allclose(out, audio)


def test_stereo_is_mixed_down_to_mono():
    stereo = numpy.array([[1.0, 0.0], [0.0, 1.0]], dtype=numpy.float32)
    out = _as_float32_mono(stereo)
    assert out.shape == (2,)
    assert numpy.allclose(out, [0.5, 0.5])


def test_int16_is_scaled_into_the_float_range():
    audio = numpy.array([32767, -32768, 0], dtype=numpy.int16)
    out = _as_float32_mono(audio)
    assert out.dtype == numpy.float32
    assert -1.01 <= float(out.min()) and float(out.max()) <= 1.01
    assert abs(float(out[0]) - 1.0) < 0.001


def test_a_plain_list_is_accepted():
    out = _as_float32_mono([0.1, 0.2])
    assert len(out) == 2


# -- the transcriber against a stub faster_whisper --------------------------


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeInfo:
    duration = 5.0


@pytest.fixture
def whisper(monkeypatch):
    """Install a stub `faster_whisper` and record how Otto drives it."""
    calls: dict = {"constructed": [], "transcribed": []}

    class FakeWhisperModel:
        def __init__(self, size, **kw):
            calls["constructed"].append((size, kw))

        def transcribe(self, audio, **kw):
            calls["transcribed"].append((audio, kw))
            return iter([FakeSegment(" hello "), FakeSegment(" world ")]), FakeInfo()

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return calls


def test_transcription_joins_and_trims_the_segments(whisper):
    transcriber = FasterWhisperTranscriber(model_size="base", idle_unload_seconds=0)
    result = transcriber.transcribe(numpy.zeros(16000, dtype=numpy.float32), 16000)
    assert result.text == "hello world"
    assert result.model == "faster-whisper:base:int8"
    assert result.seconds == 5.0
    assert result.latency >= 0


def test_the_model_is_built_for_a_cpu_with_no_gpu(whisper):
    transcriber = FasterWhisperTranscriber(model_size="tiny", idle_unload_seconds=0)
    transcriber.transcribe(numpy.zeros(1600, dtype=numpy.float32), 16000)
    size, kw = whisper["constructed"][0]
    assert size == "tiny"
    assert kw["device"] == "cpu", "there is no usable GPU on the target machine"
    assert kw["compute_type"] == "int8"
    assert kw["cpu_threads"] == 4, "saturating every core is what spins the fans"


def test_transcription_options_are_the_cheap_ones(whisper):
    transcriber = FasterWhisperTranscriber(idle_unload_seconds=0)
    transcriber.transcribe(numpy.zeros(1600, dtype=numpy.float32), 16000)
    _, kw = whisper["transcribed"][0]
    assert kw["beam_size"] == 1, "greedy decoding — measurably cheaper on CPU"
    assert kw["vad_filter"] is True
    assert kw["condition_on_previous_text"] is False
    assert kw["language"] == "en"


def test_the_model_is_built_once_and_reused(whisper):
    transcriber = FasterWhisperTranscriber(idle_unload_seconds=0)
    for _ in range(3):
        transcriber.transcribe(numpy.zeros(1600, dtype=numpy.float32), 16000)
    assert len(whisper["constructed"]) == 1


def test_an_unload_forces_a_reload_on_the_next_use(whisper):
    transcriber = FasterWhisperTranscriber(idle_unload_seconds=0)
    transcriber.transcribe(numpy.zeros(1600, dtype=numpy.float32), 16000)
    transcriber.unload()
    assert not transcriber.loaded
    transcriber.transcribe(numpy.zeros(1600, dtype=numpy.float32), 16000)
    assert len(whisper["constructed"]) == 2


def test_transcription_schedules_an_unload(whisper):
    transcriber = FasterWhisperTranscriber(idle_unload_seconds=60)
    transcriber.transcribe(numpy.zeros(1600, dtype=numpy.float32), 16000)
    assert transcriber._timer is not None
    transcriber.unload()


def test_a_failing_model_becomes_a_TranscriptionError(monkeypatch):
    class Exploding:
        def __init__(self, *a, **kw):
            pass

        def transcribe(self, *a, **kw):
            raise RuntimeError("ggml assertion failed")

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = Exploding
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    transcriber = FasterWhisperTranscriber(idle_unload_seconds=0)
    with pytest.raises(TranscriptionError, match="ggml"):
        transcriber.transcribe(numpy.zeros(1600, dtype=numpy.float32), 16000)


def test_a_missing_wheel_says_how_to_fix_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    transcriber = FasterWhisperTranscriber()
    with pytest.raises(TranscriptionError, match="setup.sh"):
        transcriber.transcribe(numpy.zeros(16, dtype=numpy.float32), 16000)


def test_preload_swallows_a_missing_wheel(monkeypatch):
    """Preload runs on a background thread during recording; it must not raise."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    FasterWhisperTranscriber().preload()  # must not raise


def test_build_transcriber_honours_the_config(config):
    config.asr_model = "tiny"
    config.asr_compute_type = "int8"
    transcriber = build_transcriber(config)
    assert isinstance(transcriber, FasterWhisperTranscriber)
    assert transcriber.model_size == "tiny"

    config.asr_model = "none"
    assert not isinstance(build_transcriber(config), FasterWhisperTranscriber)


# -- capture against a stub sounddevice -------------------------------------


@pytest.fixture
def sd(monkeypatch):
    state: dict = {"streams": []}

    class FakeStream:
        def __init__(self, **kw):
            self.kw = kw
            self.started = False
            self.stopped = False
            self.closed = False
            state["streams"].append(self)

        def start(self):
            self.started = True
            # PortAudio delivers callbacks from its own thread once the stream is
            # running, so a synchronous callback here would not be realistic — and
            # would deadlock any implementation that starts the stream while
            # holding the buffer lock. Blocks are delivered by feed() instead.

        def feed(self, blocks=2, level=0.25):
            block = numpy.full((1024, 1), level, dtype=numpy.float32)
            for _ in range(blocks):
                self.kw["callback"](block, 1024, None, None)

        def stop(self):
            self.stopped = True

        def close(self):
            self.closed = True

    module = types.ModuleType("sounddevice")
    module.InputStream = FakeStream
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return state


def test_capture_opens_a_mono_16k_float32_stream(sd):
    capture = SoundDeviceCapture()
    capture.start(16000)
    assert capture.recording
    stream = sd["streams"][0]
    assert stream.kw["samplerate"] == 16000
    assert stream.kw["channels"] == 1
    assert stream.kw["dtype"] == "float32"
    capture.stop()


def test_capture_returns_the_concatenated_samples(sd):
    capture = SoundDeviceCapture()
    capture.start(16000)
    sd["streams"][0].feed()
    samples = capture.stop()
    assert len(samples) == 2048
    assert samples.ndim == 1
    assert not capture.recording


def test_stopping_closes_the_stream(sd):
    capture = SoundDeviceCapture()
    capture.start(16000)
    capture.stop()
    stream = sd["streams"][0]
    assert stream.stopped and stream.closed


def test_starting_twice_does_not_open_two_streams(sd):
    capture = SoundDeviceCapture()
    capture.start(16000)
    capture.start(16000)
    assert len(sd["streams"]) == 1
    capture.stop()


def test_stopping_without_starting_returns_nothing(sd):
    assert SoundDeviceCapture().stop() == []


def test_a_device_that_will_not_open_names_the_permission(monkeypatch):
    class Refusing:
        def __init__(self, **kw):
            raise OSError("Device unavailable")

    module = types.ModuleType("sounddevice")
    module.InputStream = Refusing
    monkeypatch.setitem(sys.modules, "sounddevice", module)

    capture = SoundDeviceCapture()
    with pytest.raises(CaptureError, match="Microphone"):
        capture.start(16000)
    assert not capture.recording


def test_a_missing_wheel_points_at_setup(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    with pytest.raises(CaptureError, match="setup.sh"):
        SoundDeviceCapture().start(16000)


def test_build_capture_uses_the_configured_limit(config):
    config.max_recording_seconds = 12.5
    assert build_capture(config).max_seconds == 12.5


# -- the two together, through the pipeline ---------------------------------


def test_the_pipeline_drives_the_real_classes_end_to_end(otto, sd, whisper):
    """Real SoundDeviceCapture + real FasterWhisperTranscriber, stub wheels."""
    from otto.voice.pipeline import VoicePipeline

    pipeline = VoicePipeline(
        otto,
        SoundDeviceCapture(),
        FasterWhisperTranscriber(model_size="tiny", idle_unload_seconds=0),
    )
    pipeline.start()
    assert pipeline.recording
    sd["streams"][0].feed(blocks=8)
    task = pipeline.stop_and_run()

    # The stub transcriber says "hello world", which is not a fast-path command,
    # and with no model configured that is a REQUIRES_HUMAN, not a crash.
    assert task is not None
    assert pipeline.last_transcript.text == "hello world"
    assert task.request == "hello world"


def test_the_audio_callback_does_not_deadlock_against_start(sd):
    """The audio thread can fire the moment the stream opens.

    An implementation that starts the stream while holding the buffer lock stalls
    that callback — and with a non-reentrant lock, hangs outright. This is that
    regression, caught with a stream that calls back the instant it starts.
    """
    import threading

    class SynchronousStream:
        def __init__(self, **kw):
            self.kw = kw
            sd["streams"].append(self)

        def start(self):
            block = numpy.full((1024, 1), 0.3, dtype=numpy.float32)
            self.kw["callback"](block, 1024, None, None)

        def stop(self):
            pass

        def close(self):
            pass

    sys.modules["sounddevice"].InputStream = SynchronousStream

    capture = SoundDeviceCapture()
    done = threading.Event()

    def run():
        capture.start(16000)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(timeout=5), "start() deadlocked against its own audio callback"
    assert len(capture.stop()) == 1024


# -- a broken native library must not escape as a traceback -----------------


def test_a_missing_portaudio_becomes_a_capture_error(monkeypatch):
    """sounddevice raises OSError, not ImportError, when PortAudio is missing.

    Observed for real in this sandbox: catching only ImportError let it escape
    out of the hotkey handler as a traceback.
    """

    class Exploding(types.ModuleType):
        def __getattr__(self, name):
            raise OSError("PortAudio library not found")

    def fake_import(name, *a, **kw):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *a, **kw)

    import builtins

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(CaptureError, match="audio system is unavailable"):
        SoundDeviceCapture().start(16000)


def test_a_broken_ctranslate2_becomes_a_transcription_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "faster_whisper":
            raise OSError("libstdc++.so.6: version GLIBCXX_3.4.29 not found")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(TranscriptionError, match="speech engine could not load"):
        FasterWhisperTranscriber().transcribe([0.0] * 16, 16000)


def test_the_pipeline_reports_a_broken_audio_stack_instead_of_crashing(otto, monkeypatch):
    import builtins

    from otto.voice.pipeline import VoicePipeline

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    pipeline = VoicePipeline(otto, SoundDeviceCapture(), None)
    assert pipeline.start() is False  # not a traceback
    assert "audio system" in otto.last_error
