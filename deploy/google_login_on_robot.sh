#!/usr/bin/env bash
# Sign the robot in to Google Drive, from this Mac.
#
#   bash ~/ReachyMini/talk_with_reachy/deploy/google_login_on_robot.sh [robot-address]
#
# robot-address defaults to reachy-mini.local; use the robot's IP if that name does
# not resolve. SSH asks for the robot's password. The robot then shows a code: open
# google.com/device on any phone or computer, enter it, and sign in. Install Talk with
# Reachy on the robot first. A running app picks up the sign-in on its next upload pass.
set -euo pipefail

ROBOT="${1:-reachy-mini.local}"
exec ssh -t "pollen@$ROBOT" /venvs/apps_venv/bin/talk-with-reachy-google-login
