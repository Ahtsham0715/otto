#!/bin/bash
# Otto — one-command setup. Idempotent: safe to run again at any time.
#
# Designed for an Intel (x86_64) Mac with no Rust, no guaranteed Xcode and no LLM.
# Nothing here compiles anything: every dependency has a pre-built wheel.

set -u

OTTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$OTTO_DIR/.venv"
PY="${PYTHON:-python3}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; }

bold "Otto setup"
echo

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
bold "1. Python"
if ! command -v "$PY" >/dev/null 2>&1; then
  fail "python3 not found. Install Python 3.11+ (brew install python@3.11) and re-run."
  exit 1
fi
PY_VERSION="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$("$PY" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
if [ "$PY_OK" != "1" ]; then
  fail "Python $PY_VERSION found, but Otto needs 3.11 or newer."
  exit 1
fi
ok "python $PY_VERSION at $(command -v "$PY")"

ARCH="$(uname -m 2>/dev/null || echo unknown)"
ok "architecture $ARCH"
if [ "$ARCH" = "x86_64" ]; then
  ok "Intel Mac detected — Otto is configured for CPU-only, no-GPU operation"
fi

# ---------------------------------------------------------------------------
# 2. Virtual environment
# ---------------------------------------------------------------------------
echo
bold "2. Environment"
if [ ! -d "$VENV" ]; then
  "$PY" -m venv "$VENV" || { fail "could not create $VENV"; exit 1; }
  ok "created .venv"
else
  ok ".venv already exists"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip >/dev/null 2>&1 && ok "pip up to date"

# ---------------------------------------------------------------------------
# 3. Dependencies
# ---------------------------------------------------------------------------
echo
bold "3. Dependencies"
echo "   Otto's core needs nothing but the standard library. These wheels are for"
echo "   the menu bar, the hotkey and speech-to-text."
echo

install_group() {
  local label="$1"; shift
  if python -m pip install --quiet "$@" >/tmp/otto-pip.log 2>&1; then
    ok "$label"
  else
    warn "$label could not be installed — Otto will run without it"
    warn "  (see /tmp/otto-pip.log; the terminal REPL and text box still work)"
  fi
}

if [ "$(uname -s)" = "Darwin" ]; then
  install_group "menu bar (rumps, pyobjc-framework-Cocoa)" rumps "pyobjc-framework-Cocoa>=9"
  install_group "global hotkey (pynput)" pynput
else
  warn "not macOS — skipping the menu-bar and hotkey wheels"
fi
install_group "microphone (sounddevice)" sounddevice
install_group "speech to text (faster-whisper, CTranslate2 int8 — no torch)" faster-whisper
install_group "tests (pytest)" pytest

echo
if python -c "import torch" 2>/dev/null; then
  warn "torch is installed in this environment. Otto never imports it; it is not needed."
else
  ok "no torch — as intended (PyPI has no modern torch wheel for macOS x86_64)"
fi

# ---------------------------------------------------------------------------
# 4. Speech model
# ---------------------------------------------------------------------------
echo
bold "4. Speech-to-text model"
echo "   Otto uses faster-whisper 'base' at int8, loaded only when you talk to it"
echo "   and unloaded again after five minutes idle."
if python -c "import faster_whisper" 2>/dev/null; then
  read -r -p "   Download the model now (~75 MB, one-off)? [Y/n] " reply
  case "${reply:-Y}" in
    [Nn]*) warn "skipped — it will download on your first voice command" ;;
    *)
      # stderr is suppressed: a failed download is expected when offline, and a
      # raw traceback would look far more alarming than the situation warrants.
      if python - >/tmp/otto-model.log 2>&1 <<'MODEL'
from faster_whisper import WhisperModel
WhisperModel("base", device="cpu", compute_type="int8")
MODEL
      then ok "model ready"
      else warn "download failed (see /tmp/otto-model.log) — it retries on first use"
      fi
      ;;
  esac
else
  warn "faster-whisper is not installed — voice is off, the text box still works"
fi

# ---------------------------------------------------------------------------
# 5. A language model (optional)
# ---------------------------------------------------------------------------
echo
bold "5. Language model (optional)"
echo "   Otto runs simple commands — open an app, create a folder, remember a"
echo "   preference — with no model at all. A model is only needed for requests"
echo "   that have to be planned."
echo
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama is installed"
  if ollama list 2>/dev/null | grep -qE 'qwen2.5:3b|llama3.2:3b'; then
    ok "a small model is already pulled"
  else
    read -r -p "   Pull qwen2.5:3b (~2 GB)? On a 2019 i9 expect roughly 5–15 tokens/sec. [y/N] " reply
    case "${reply:-N}" in
      [Yy]*) ollama pull qwen2.5:3b && ok "qwen2.5:3b pulled" ;;
      *) warn "skipped" ;;
    esac
  fi
  python - <<'PY'
import json, pathlib, os
path = pathlib.Path(os.environ.get("OTTO_HOME", pathlib.Path.home() / ".otto")) / "config.json"
path.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(path.read_text()) if path.exists() else {}
providers = data.setdefault("providers", {})
for tier in ("fast", "strong"):
    if not providers.get(tier, {}).get("kind") or providers[tier]["kind"] == "none":
        providers[tier] = {"kind": "ollama", "model": "qwen2.5:3b",
                           "base_url": "http://127.0.0.1:11434"}
path.write_text(json.dumps(data, indent=2) + "\n")
print(f"  ✓ pointed Otto at Ollama (edit {path} to change)")
PY
else
  warn "Ollama is not installed"
  echo "     • Local, private, free:  brew install ollama && ollama pull qwen2.5:3b"
  echo "       Honest expectation on a 2019 i9: ~5–15 tokens/sec, so a multi-step"
  echo "       request takes tens of seconds and the fans will spin up."
  echo "     • Fast and free-tier:    a Groq or Cerebras API key feels dramatically"
  echo "       better on this hardware and costs the Mac nothing. See SETUP.md §5."
  echo "     • Either way, Otto works today for simple commands with no model."
fi

# ---------------------------------------------------------------------------
# 6. macOS permissions
# ---------------------------------------------------------------------------
echo
bold "6. macOS permissions you must grant"
if [ "$(uname -s)" = "Darwin" ]; then
  echo "   Grant these to the app that launches Otto — your terminal (Terminal.app"
  echo "   or iTerm), NOT to a file called otto. macOS reads them at process start,"
  echo "   so quit and relaunch Otto after granting each one."
  echo
  echo "   System Settings → Privacy & Security →"
  echo "     • Accessibility     — drive other apps' menus and buttons"
  echo "     • Input Monitoring  — the push-to-talk hotkey (without it, pynput"
  echo "                           fails SILENTLY: the hotkey simply never fires)"
  echo "     • Microphone        — prompted the first time you record"
  echo
  echo "   Run ./run.sh --check at any time to see which are granted."
else
  warn "not macOS — permissions do not apply here"
fi

# ---------------------------------------------------------------------------
# 7. Verify
# ---------------------------------------------------------------------------
echo
bold "7. Verifying the install"
if python -m pytest "$OTTO_DIR/tests" -q >/tmp/otto-tests.log 2>&1; then
  ok "$(tail -1 /tmp/otto-tests.log)"
else
  warn "some tests failed — see /tmp/otto-tests.log"
fi
python -m otto --check 2>/dev/null | sed 's/^/  /'

echo
bold "Done."
echo "  Start Otto:      ./run.sh"
echo "  Try it by text:  ./run.sh --text 'create a folder called Test on my Desktop'"
echo "  Read next:       SETUP.md (5 minutes), then STATUS.md for what is verified."
