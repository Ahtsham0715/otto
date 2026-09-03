"""Optional Piper TTS.

Off by default and it should usually stay off (docs/RESEARCH.md §3): Piper's medium
voices have been reported at >2 GB RAM and slower-than-realtime synthesis on modest
hardware, which is exactly the profile the budget exists to prevent. macOS `say` is
free, instant and always there.

This exists so the user who wants a nicer voice can have one deliberately, with the
cost written down.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any


class PiperUnavailable(RuntimeError):
    """Piper is not installed or has no voice model configured."""


def piper_available() -> bool:
    return shutil.which("piper") is not None


def speak_with_piper(text: str, config: Any) -> None:
    """Synthesise with Piper and play through `afplay`.

    Both are argv lists; no shell, and the text goes in on stdin so it is never
    part of a command line.
    """
    binary = shutil.which("piper")
    if binary is None:
        raise PiperUnavailable("piper is not installed")
    voice = getattr(config, "tts_voice", "") or ""
    if not voice:
        raise PiperUnavailable("no Piper voice model configured (config.tts_voice)")

    argv = [binary, "--model", voice, "--output_file", "-"]
    try:
        synth = subprocess.run(  # noqa: S603 - argv list, shell=False
            argv,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PiperUnavailable(f"piper failed: {exc}") from exc
    if synth.returncode != 0 or not synth.stdout:
        raise PiperUnavailable(
            f"piper produced no audio: {synth.stderr[:200].decode('utf-8', 'replace')}"
        )

    player = shutil.which("afplay") or shutil.which("aplay")
    if player is None:
        raise PiperUnavailable("no audio player found")
    subprocess.run(  # noqa: S603 - argv list, shell=False
        [player, "-"], input=synth.stdout, capture_output=True, timeout=120,
        shell=False, check=False,
    )
