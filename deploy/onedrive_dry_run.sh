#!/usr/bin/env bash
# Check the OneDrive upload end to end on this Mac, without a robot.
#
#   bash ~/ReachyMini/talk_with_reachy/deploy/onedrive_dry_run.sh
#
# Opens a browser for your UC sign-in, then logs and uploads a two-line test
# conversation with the app's own code. Nothing is installed system-wide.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/deploy/.onedrive-venv"
REQS=(msal "httpx>=0.28")

if [ ! -x "$VENV/bin/python" ]; then
  echo "Setting up sign-in tools (one time) ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv -q --python 3.12 "$VENV"
    uv pip install -q --python "$VENV/bin/python" "${REQS[@]}"
  else
    PY=""
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        PY="$candidate"
        break
      fi
    done
    if [ -z "$PY" ]; then
      echo "This needs Python 3.10 or newer, or uv. Install uv with:  brew install uv"
      exit 1
    fi
    "$PY" -m venv "$VENV"
    "$VENV/bin/pip" install -q "${REQS[@]}"
  fi
fi

"$VENV/bin/python" "$REPO_DIR/deploy/onedrive_dry_run.py"
