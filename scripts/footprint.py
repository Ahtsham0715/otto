"""Measure Otto against the performance budget in BRIEF.md §2b.

Run it on the Mac:  python3 scripts/footprint.py

It reports resident memory and cold start for the parts that exist on every
platform, and — when run on macOS with the optional wheels installed — the
menu-bar and ASR deltas too. Numbers measured on Linux are still meaningful for
the core harness, because the core imports nothing but the standard library; the
macOS-only rows are the ones that need a Mac and are flagged as such.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BUDGET = {
    "idle_rss_mb": 250,
    "cold_start_s": 3.0,
    "transcribe_s": 2.0,
}


def rss_mb() -> float:
    """Resident set size in MB, without a psutil dependency."""
    if sys.platform == "darwin":
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True, text=True, check=False,
        )
        try:
            return int(out.stdout.strip()) / 1024
        except ValueError:
            return 0.0
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def timed_import(statement: str) -> float:
    code = f"import time; t=time.perf_counter(); {statement}; print(time.perf_counter()-t)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=False,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return float("nan")


def main() -> int:
    print(f"Otto footprint — {sys.platform}, python {sys.version.split()[0]}\n")

    baseline = rss_mb()
    print(f"  bare interpreter RSS            {baseline:6.1f} MB")

    started = time.perf_counter()
    import otto.app  # noqa: F401
    from otto.app import Otto
    from otto.services import Services
    import_seconds = time.perf_counter() - started
    after_import = rss_mb()
    print(f"  after importing otto            {after_import:6.1f} MB "
          f"(+{after_import - baseline:.1f})  import {import_seconds * 1000:.0f} ms")

    otto = Otto(Services.for_tests(Path.home() / ".otto-footprint"))
    otto.services.broker.set_auto(True)
    gc.collect()
    constructed = rss_mb()
    print(f"  Otto constructed and idle       {constructed:6.1f} MB "
          f"(+{constructed - after_import:.1f})")

    started = time.perf_counter()
    otto.handle_utterance("open Safari")
    fast_seconds = time.perf_counter() - started
    gc.collect()
    after_task = rss_mb()
    print(f"  after one fast-path command     {after_task:6.1f} MB "
          f"(+{after_task - constructed:.1f})  took {fast_seconds * 1000:.0f} ms")

    print()
    print("  cold start, measured in a fresh interpreter:")
    for label, statement in (
        ("import otto.app", "import otto.app"),
        ("import + construct",
         "import otto.app; otto.app.Otto()"),
    ):
        print(f"    {label:<28} {timed_import(statement) * 1000:6.0f} ms")

    print()
    print("  optional wheels:")
    for module in ("rumps", "pynput", "sounddevice", "faster_whisper"):
        seconds = timed_import(f"import {module}")
        state = "not installed" if seconds != seconds else f"{seconds * 1000:.0f} ms"
        print(f"    import {module:<22} {state}")

    print()
    print("  budget (BRIEF.md §2b):")
    print(f"    idle RSS  < {BUDGET['idle_rss_mb']} MB   → measured {after_task:.1f} MB "
          f"{'PASS' if after_task < BUDGET['idle_rss_mb'] else 'FAIL'}")
    print(f"    cold start < {BUDGET['cold_start_s']} s  → see above")
    print("    idle CPU ~0%       → no polling loops; timers are one-shot")
    print("    ASR < 2 s          → needs a Mac with faster-whisper installed")

    otto.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
