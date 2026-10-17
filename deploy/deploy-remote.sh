#!/bin/bash
# Sync code to the Linux host and restart the systemd service.
# Usage: deploy/deploy-remote.sh [--force] [ssh-host] [remote-dir]
#   --force  deploy even with uncommitted changes or commits not pushed to the upstream branch;
#            the recorded revision is then marked "-dirty".
set -euo pipefail

FORCE=false
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=true ;;
        -h | --help)
            sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done

HOST="${POSITIONAL[0]:-dmit-us-la}"
REMOTE_DIR="${POSITIONAL[1]:-/root/Telegram_summary}"
SERVICE="tg-digest.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

fail_or_force() {
    if [[ "$FORCE" == true ]]; then
        printf 'Warning: %s (continuing because of --force)\n' "$1" >&2
        DIRTY=true
    else
        printf 'Refusing to deploy: %s\n' "$1" >&2
        printf 'Commit and push first, or rerun with --force for a temporary deployment.\n' >&2
        exit 1
    fi
}

cd "$PROJECT_DIR"
DIRTY=false

if [[ -n "$(git status --porcelain)" ]]; then
    git status --short >&2
    fail_or_force "the working tree has uncommitted changes"
fi

if ! git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
    fail_or_force "branch $(git rev-parse --abbrev-ref HEAD) has no upstream branch"
else
    git fetch --quiet
    UNPUSHED="$(git rev-list --count '@{upstream}..HEAD')"
    if [[ "$UNPUSHED" != 0 ]]; then
        fail_or_force "$UNPUSHED commit(s) have not been pushed to $(git rev-parse --abbrev-ref '@{upstream}')"
    fi
fi

REVISION="$(git rev-parse --short=12 HEAD)"
[[ "$DIRTY" == true ]] && REVISION="$REVISION-dirty"
printf 'Deploying revision %s to %s:%s\n' "$REVISION" "$HOST" "$REMOTE_DIR"

.venv/bin/ruff check .
.venv/bin/pytest -q

# Secrets, real group config, sessions, SQLite, logs, the remote venv and the deployed revision
# stay on the host.
rsync -az --delete --no-owner --no-group \
    --exclude '.git/' \
    --exclude '.env' \
    --exclude 'config/groups.yaml*' \
    --exclude 'data/' \
    --exclude '.venv/' \
    --exclude 'REVISION' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '*.egg-info/' \
    --exclude '.DS_Store' \
    "$PROJECT_DIR/" "$HOST:$REMOTE_DIR/"

ssh "$HOST" bash -s -- "$REMOTE_DIR" "$SERVICE" "$REVISION" <<'REMOTE'
set -euo pipefail
cd "$1"
.venv/bin/pip install --quiet -e .
printf '%s\n' "$3" > REVISION
install -m 644 deploy/systemd/tg-digest.service "/etc/systemd/system/$2"
systemctl daemon-reload
systemctl restart "$2"
sleep 5
systemctl --no-pager --lines=0 status "$2"
printf 'Deployed revision: %s\n' "$(cat REVISION)"
tail -n 20 data/tg-digest.log
REMOTE
