#!/bin/zsh

# Local 5-minute scheduler for the deployed Vercel endpoint.
# Before Telegram is initialized, it calls ?confirm=1 so the first successful
# chat detection automatically sends a confirmation message once.

set -eu

STATUS_URL="https://wgai-monitor.vercel.app/api/check"
NOTIFY_URL="https://wgai-monitor.vercel.app/api/notify"
STATE_FILE="$HOME/.wgai-monitor-confirmed"
LAST_TEXT_FILE="$HOME/.wgai-monitor-last-text"
LOG_FILE="/tmp/wgai-monitor-cron.log"

STATUS_RESPONSE="$(/usr/bin/curl -fsS "$STATUS_URL")"
printf '%s %s\n' "$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')" "$STATUS_RESPONSE" >> "$LOG_FILE"

CURRENT_TEXT="$(
  printf '%s' "$STATUS_RESPONSE" | /usr/bin/python3 -c '
import json, sys
payload = json.load(sys.stdin)
print(payload.get("current_text", ""))
')"

PREVIOUS_TEXT=""
if [[ -f "$LAST_TEXT_FILE" ]]; then
  PREVIOUS_TEXT="$(/bin/cat "$LAST_TEXT_FILE")"
fi

if [[ ! -f "$LAST_TEXT_FILE" ]]; then
  printf '%s' "$CURRENT_TEXT" > "$LAST_TEXT_FILE"
fi

if [[ ! -f "$STATE_FILE" ]]; then
  NOTIFY_RESPONSE="$(/usr/bin/curl -fsS "$NOTIFY_URL?confirm=1")"
  printf '%s %s\n' "$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')" "$NOTIFY_RESPONSE" >> "$LOG_FILE"
  /usr/bin/touch "$STATE_FILE"
  printf '%s' "$CURRENT_TEXT" > "$LAST_TEXT_FILE"
elif [[ "$CURRENT_TEXT" != "$PREVIOUS_TEXT" ]]; then
  NOTIFY_RESPONSE="$(/usr/bin/curl -fsS "$NOTIFY_URL")"
  printf '%s %s\n' "$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')" "$NOTIFY_RESPONSE" >> "$LOG_FILE"
  printf '%s' "$CURRENT_TEXT" > "$LAST_TEXT_FILE"
fi
