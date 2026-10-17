#!/bin/bash
set -euo pipefail

LABEL="com.doublex.telegram-digest"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
PYTHON="$PROJECT_DIR/.venv/bin/python"
TG_DIGEST="$PROJECT_DIR/.venv/bin/tg-digest"

require_file() {
    if [[ ! -f "$1" ]]; then
        printf 'Missing required file: %s\n' "$1" >&2
        exit 1
    fi
}

require_file "$PROJECT_DIR/.env"
require_file "$TEMPLATE"
require_file "$PYTHON"
require_file "$TG_DIGEST"

CONFIG_PATH="$(cd "$PROJECT_DIR" && "$PYTHON" - <<'PY'
from tg_digest.settings import EnvironmentSettings
print(EnvironmentSettings().tg_digest_config)
PY
)"
DATA_DIR="$(cd "$PROJECT_DIR" && "$PYTHON" - <<'PY'
from tg_digest.settings import EnvironmentSettings
print(EnvironmentSettings().tg_digest_data_dir)
PY
)"
BASE_URL="$(cd "$PROJECT_DIR" && "$PYTHON" - <<'PY'
from tg_digest.settings import EnvironmentSettings
print(EnvironmentSettings().anthropic_base_url or "")
PY
)"

[[ "$CONFIG_PATH" = /* ]] || CONFIG_PATH="$PROJECT_DIR/$CONFIG_PATH"
[[ "$DATA_DIR" = /* ]] || DATA_DIR="$PROJECT_DIR/$DATA_DIR"
require_file "$CONFIG_PATH"
require_file "$DATA_DIR/user.session"

if [[ -n "$BASE_URL" ]] && ! curl --silent --show-error --max-time 5 --output /dev/null \
    "$BASE_URL"; then
    printf 'The configured model gateway is not reachable: %s\n' "$BASE_URL" >&2
    printf '%s\n' 'Start or repair it before installing Telegram Digest.' >&2
    exit 1
fi

mkdir -p "$PLIST_DIR" "$DATA_DIR"

"$PYTHON" - "$TEMPLATE" "$PLIST_PATH" "$PROJECT_DIR" <<'PY'
from pathlib import Path
from xml.sax.saxutils import escape
import sys

template = Path(sys.argv[1])
destination = Path(sys.argv[2])
project_dir = escape(sys.argv[3])
rendered = template.read_text(encoding="utf-8").replace("__PROJECT_DIR__", project_dir)
destination.write_text(rendered, encoding="utf-8")
PY

plutil -lint "$PLIST_PATH"
chmod 600 "$PLIST_PATH"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST_PATH"
launchctl kickstart -k "gui/$UID/$LABEL"

printf 'Installed and started %s\n' "$LABEL"
printf 'Status: %s/deploy/status-launchd.sh\n' "$PROJECT_DIR"
