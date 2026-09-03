"""Entry point.

    python3 -m otto              → menu-bar app (macOS)
    python3 -m otto --text "…"   → run one command and print the result
    python3 -m otto --repl       → a terminal REPL; works everywhere, including Linux
    python3 -m otto --check      → environment and permission report, no side effects

The REPL exists so the whole system can be driven without a Mac, and so the user has
a fallback if a macOS permission is missing.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="otto", description="Otto, a local assistant")
    parser.add_argument("--text", help="run one command and exit")
    parser.add_argument("--repl", action="store_true", help="terminal REPL")
    parser.add_argument("--check", action="store_true", help="environment report")
    parser.add_argument("--console", action="store_true", help="open the dev console")
    parser.add_argument("--yes", action="store_true",
                        help="auto-approve confirmations (scripted runs only)")
    args = parser.parse_args(argv)

    from .app import Otto

    otto = Otto()

    if args.yes:
        otto.services.broker.set_auto(True)

    if args.check:
        return _check(otto)

    if args.text:
        task = otto.handle_utterance(args.text, source="text")
        print(task.summary or task.error or "(no result)")
        for subtask in task.subtasks:
            print(f"  [{subtask.status.value}] {subtask.description}"
                  f" — {subtask.result or subtask.error or ''}")
        return 0 if task.status.value == "COMPLETED" else 1

    if args.repl or sys.platform != "darwin":
        return _repl(otto, open_console=args.console)

    from .ui.menubar import run_menubar

    run_menubar(otto)
    return 0


def _check(otto) -> int:
    from .ui.hotkey import accessibility_trusted

    config = otto.services.config
    print("Otto environment check")
    print(f"  platform            {sys.platform}")
    print(f"  python              {sys.version.split()[0]}")
    print(f"  macOS bridge        {type(otto.services.mac).__name__}")
    print(f"  sandbox roots       {otto.services.sandbox.describe()}")
    print(f"  memory database     {config.db_path}")
    print(f"  audit log           {config.audit_file}")
    print(f"  hotkey              {config.hotkey} ({config.push_to_talk})")
    print(f"  ASR                 {config.asr_model} / {config.asr_compute_type} "
          f"(unloads after {config.asr_idle_unload_seconds:.0f}s idle)")
    print(f"  TTS                 {config.tts}")
    status = otto.model_status()
    for tier, info in status["tiers"].items():
        label = f"model[{tier}]"
        print(f"  {label:<19} {info['kind']} {info['model']}".rstrip())
    if not status["any_configured"]:
        print("  → no model configured; the fast path still handles simple commands")
    if status["cloud_tiers"]:
        print(f"  → CLOUD in use for: {', '.join(status['cloud_tiers'])}")
    print(f"  accessibility       {'trusted' if accessibility_trusted() else 'NOT trusted'}")
    for name, module in (("rumps", "rumps"), ("pynput", "pynput"),
                         ("sounddevice", "sounddevice"),
                         ("faster-whisper", "faster_whisper")):
        try:
            __import__(module)
            print(f"  {name:<19} installed")
        except ImportError:
            print(f"  {name:<19} NOT installed")
    return 0


def _repl(otto, *, open_console: bool = False) -> int:
    print(otto.greeting())
    print("Type a command, or /quit, /cancel, /console, /memory, /check.")
    if open_console:
        _open_console(otto)
    # In a terminal the human is right here, so approvals are a y/n prompt.
    otto.set_approval_hook(lambda a: a.decide(_ask_terminal(a)))
    while True:
        try:
            line = input("otto> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/cancel":
            print("cancelled" if otto.cancel() else "nothing running")
            continue
        if line == "/console":
            _open_console(otto)
            continue
        if line == "/check":
            _check(otto)
            continue
        if line == "/memory":
            for memory in otto.services.memory.all():
                print(f"  [{memory.id}] {memory.scope}: {memory.as_line()}")
            continue
        task = otto.handle_utterance(line, source="text")
        print(f"  {task.status.value}: {task.summary or task.error}")
    otto.close()
    return 0


def _open_console(otto) -> None:
    from .ui.console import DevConsole

    console = DevConsole(otto)
    print("  console:", console.start())


def _ask_terminal(approval) -> bool:
    print(f"\n  ⚠️  {approval.reason}")
    print(f"     agent={approval.agent_id} level={approval.level} tool={approval.tool}")
    try:
        return input("     allow? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
