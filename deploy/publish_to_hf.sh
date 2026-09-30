#!/usr/bin/env bash
# Publish Talk with Reachy as a private Hugging Face Space.
#
#   bash ~/ReachyMini/talk_with_reachy/deploy/publish_to_hf.sh
#
# Installs the Hugging Face tools into deploy/.deploy-venv (nothing system-wide),
# signs you in to Hugging Face through your browser if needed, creates the
# private Space <your-account>/talk_with_reachy, and uploads the app.
# Safe to run again: it updates the same Space.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/deploy/.deploy-venv"
SPACE_NAME="${SPACE_NAME:-talk_with_reachy}"
HUB_REQ='huggingface_hub>=1.17,<2'

if ! grep -Eq '^CLIENT_ID = "[0-9a-fA-F-]{36}"' "$REPO_DIR/src/talk_with_reachy/onedrive_upload.py"; then
  echo "CLIENT_ID in src/talk_with_reachy/onedrive_upload.py is not filled in yet."
  echo "Finish the Microsoft app registration first."
  exit 1
fi

if [ ! -x "$VENV/bin/hf" ]; then
  echo "Setting up Hugging Face tools (one time) ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv -q --python 3.12 "$VENV"
    uv pip install -q --python "$VENV/bin/python" "$HUB_REQ"
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
      echo "Then run this script again."
      exit 1
    fi
    "$PY" -m venv "$VENV"
    "$VENV/bin/pip" install -q "$HUB_REQ"
  fi
fi

if ! "$VENV/bin/hf" auth whoami >/dev/null 2>&1; then
  echo "Sign in to Hugging Face (a browser window will open) ..."
  "$VENV/bin/hf" auth login
fi

"$VENV/bin/python" "$REPO_DIR/deploy/publish_space.py" "$REPO_DIR" "$SPACE_NAME"
