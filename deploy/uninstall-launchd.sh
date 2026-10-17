#!/bin/bash
set -euo pipefail

LABEL="com.doublex.telegram-digest"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST_PATH"

printf 'Uninstalled %s\n' "$LABEL"
printf '%s\n' 'Project code, .env, Telegram sessions, logs, and SQLite data were not deleted.'
