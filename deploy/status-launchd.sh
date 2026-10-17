#!/bin/bash
set -u

LABEL="com.doublex.telegram-digest"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_PATH="$(
    cd "$PROJECT_DIR" 2>/dev/null \
        && .venv/bin/python -c 'from tg_digest.settings import load_settings; print(load_settings().log_path)' \
            2>/dev/null
)" || LOG_PATH="$PROJECT_DIR/data/tg-digest.log"

printf 'LaunchAgent: %s\n' "$PLIST_PATH"
if [[ -f "$PLIST_PATH" ]]; then
    plutil -lint "$PLIST_PATH"
else
    printf 'Not installed. Run: %s/deploy/install-launchd.sh\n' "$PROJECT_DIR"
fi

printf '\nlaunchctl status:\n'
launchctl print "gui/$UID/$LABEL" 2>&1 || true

printf '\nConfigured schedules:\n'
(
    cd "$PROJECT_DIR" || exit 1
    .venv/bin/python - <<'PY'
from tg_digest.settings import load_settings

settings = load_settings()
summary = settings.config.summary
maintenance = settings.config.maintenance
print(f"Digest:      {summary.cron} ({settings.config.timezone})")
print(
    "Maintenance: "
    f"{maintenance.cron} "
    f"(enabled={maintenance.enabled}, retention_days={maintenance.retention_days})"
)
print(f"Application log: {settings.log_path}")
PY
) 2>&1 || true

printf '\nRecent application log:\n'
tail -n 40 "$LOG_PATH" 2>/dev/null || printf 'No application log yet.\n'

printf '\nRecent launchd errors:\n'
tail -n 20 "$PROJECT_DIR/data/launchd.stderr.log" 2>/dev/null || printf 'No launchd error log yet.\n'
