#!/usr/bin/env bash
# Make the robot start Talk with Reachy by itself: on its first wake-up after it is switched on, and whenever it
# is awake with no app running and someone touches an antenna. This is the robot's own "startup app" setting;
# it survives reboots and app updates. Run it on a Mac on the same network as the robot:
#
#   bash deploy/set_startup_app.sh          set Talk with Reachy as the startup app
#   bash deploy/set_startup_app.sh clear    turn the startup app off
#
# The robot is reachy-mini.local, or the address that the Reachy Mini Control app used last;
# set ROBOT=<name or IP> to choose another one. No password is needed.

APP="talk_with_reachy"
ACTION="${1:-set}"
CONTROL_LOG="$HOME/Library/Logs/com.pollen-robotics.reachy-mini/Reachy Mini Control.log"

ROBOT="${ROBOT:-reachy-mini.local}"
if ! curl -sS -m 5 "http://$ROBOT:8000/api/daemon/status" >/dev/null 2>&1 && [ -f "$CONTROL_LOG" ]; then
  LAST_IP="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:8000' "$CONTROL_LOG" | grep -v '^127\.' | tail -1 | sed 's/:8000//')"
  [ -n "$LAST_IP" ] && ROBOT="$LAST_IP"
fi
API="http://$ROBOT:8000/api/apps/startup-app"

echo "Robot: $ROBOT"
if ! BEFORE="$(curl -sS -m 10 "$API" 2>&1)"; then
  echo "The robot did not answer ($BEFORE). Switch it on, wait until Reachy Mini Control shows it as connected, and try again."
  exit 1
fi
echo "Startup app before: $BEFORE"

case "$ACTION" in
  set) BODY="{\"startup_app\": \"$APP\"}" ;;
  clear) BODY='{"startup_app": null}' ;;
  *) echo "Unknown action '$ACTION'; use 'set' or 'clear'."; exit 1 ;;
esac

RESPONSE="$(curl -sS -m 300 -X PUT -H "Content-Type: application/json" -d "$BODY" -w ' (HTTP %{http_code})' "$API" 2>&1)"
echo "Robot's answer: $RESPONSE"
case "$RESPONSE" in
  *"(HTTP 200)") ;;
  *) echo "The setting was not changed. If the answer says the app is not installed, install Talk with Reachy first."; exit 1 ;;
esac

echo "Startup app now: $(curl -sS -m 10 "$API" 2>&1)"
if [ "$ACTION" = "set" ]; then
  echo "Done. The robot starts Talk with Reachy on its next wake-up, or when someone touches an antenna while no app runs."
else
  echo "Done. The robot no longer starts an app by itself."
fi
