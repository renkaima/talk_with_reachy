#!/usr/bin/env bash
# Manage the voices Talk with Reachy recognizes, from a Mac.
#
#   bash ~/ReachyMini/talk_with_reachy/deploy/voices.sh list
#   bash ~/ReachyMini/talk_with_reachy/deploy/voices.sh enroll P01 --name Mary
#   bash ~/ReachyMini/talk_with_reachy/deploy/voices.sh link V007 P02 --name Sam
#   bash ~/ReachyMini/talk_with_reachy/deploy/voices.sh delete P01
#
# By default the command runs on the robot over SSH (reachy-mini.local; set
# ROBOT=<address> to use another name or an IP). SSH asks for the robot's password.
# With ROBOT=local it runs on this Mac instead, for the Reachy Mini Control simulation.
set -euo pipefail

ROBOT="${ROBOT:-reachy-mini.local}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$#" -eq 0 ]; then
  sed -n '2,11p' "$0"
  exit 1
fi

if [ "$ROBOT" = "local" ]; then
  PYTHONPATH="$HERE/src" exec python3 -m talk_with_reachy.voices_cli "$@"
fi

quoted=$(printf '%q ' "$@")
exec ssh -t "pollen@$ROBOT" "/venvs/apps_venv/bin/talk-with-reachy-voices $quoted"
