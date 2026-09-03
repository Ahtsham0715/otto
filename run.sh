#!/bin/bash
# Otto — start the assistant.
#
#   ./run.sh                       menu-bar app (macOS)
#   ./run.sh --repl                terminal REPL (works anywhere)
#   ./run.sh --text "open Safari"  run one command and exit
#   ./run.sh --check               environment and permission report

set -u

OTTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$OTTO_DIR/.venv"

if [ -d "$VENV" ]; then
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
else
  echo "No .venv found — run ./setup.sh first." >&2
  echo "Trying the system python anyway." >&2
fi

cd "$OTTO_DIR" || exit 1
exec python -m otto "$@"
