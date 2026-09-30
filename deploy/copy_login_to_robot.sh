#!/usr/bin/env bash
# Copy this Mac's OneDrive sign-in to the robot, so the robot needs no sign-in of its own.
#
#   bash ~/ReachyMini/talk_with_reachy/deploy/copy_login_to_robot.sh [robot-address]
#
# robot-address defaults to reachy-mini.local; use the robot's IP if that name does not
# resolve. Run deploy/onedrive_dry_run.sh first. SSH asks once for the robot's password.
set -euo pipefail

ROBOT="${1:-reachy-mini.local}"
CACHE="$HOME/.config/talk_with_reachy/onedrive_token_cache.json"

if [ ! -f "$CACHE" ]; then
  echo "No OneDrive sign-in found on this Mac. Run deploy/onedrive_dry_run.sh first."
  exit 1
fi

ssh "pollen@$ROBOT" \
  'umask 077 && mkdir -p ~/.config/talk_with_reachy && cat > ~/.config/talk_with_reachy/onedrive_token_cache.json' \
  < "$CACHE"

echo "Copied the sign-in to $ROBOT."
echo "A running Talk with Reachy picks it up on its next upload pass (within 5 minutes)."
