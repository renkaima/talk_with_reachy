#!/usr/bin/env bash
# Check the Google Drive upload end to end on this Mac, without a robot.
#
#   bash ~/ReachyMini/talk_with_reachy/deploy/google_dry_run.sh
#
# Shows a code to enter at google.com/device, then logs and uploads a two-line
# test conversation with the app's own code. Nothing is installed system-wide.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/deploy/.google-venv"
REQS=("httpx>=0.28" numpy)

if [ ! -x "$VENV/bin/python" ]; then
  echo "Setting up sign-in tools (one time) ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv -q --python 3.12 "$VENV"
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
  fi
fi
# Every run, so that a venv made by an older version of this script gets new requirements.
if command -v uv >/dev/null 2>&1; then
  uv pip install -q --python "$VENV/bin/python" "${REQS[@]}"
else
  "$VENV/bin/pip" install -q "${REQS[@]}"
fi

"$VENV/bin/python" "$REPO_DIR/deploy/google_dry_run.py"
