#!/usr/bin/env bash
# Publish Talk with Reachy as a public Hugging Face Space (a community app in the Reachy Mini Control store).
#
#   bash ~/ReachyMini/talk_with_reachy/deploy/publish_to_hf.sh
#
# Installs the Hugging Face tools into deploy/.deploy-venv (nothing system-wide),
# signs you in to Hugging Face through your browser if needed, creates the
# Space <your-account>/talk_with_reachy, uploads the app, and makes the Space public
# (run with SPACE_PRIVATE=1 to keep it private).
# Safe to run again: it updates the same Space.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/deploy/.deploy-venv"
SPACE_NAME="${SPACE_NAME:-talk_with_reachy}"
HUB_REQ='huggingface_hub>=1.17,<2'

if ! grep -Eq '^CLIENT_ID = ".+\.apps\.googleusercontent\.com"' "$REPO_DIR/src/talk_with_reachy/google_drive_upload.py"; then
  echo "Note: CLIENT_ID in src/talk_with_reachy/google_drive_upload.py is empty, so this version"
  echo "keeps study files on the robot only (no Google Drive upload). Publishing anyway."
elif [ ! -s "$REPO_DIR/deploy/google_client_secret.txt" ]; then
  echo "Note: deploy/google_client_secret.txt is missing, so robots that install this version"
  echo "cannot sign in to Google Drive. Publishing anyway."
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
