#!/bin/bash
# Sync code to the Linux host and restart the systemd service.
# Usage: deploy/deploy-remote.sh [ssh-host] [remote-dir]
set -euo pipefail

HOST="${1:-dmit-us-la}"
REMOTE_DIR="${2:-/root/Telegram_summary}"
SERVICE="tg-digest.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

(
    cd "$PROJECT_DIR"
    .venv/bin/ruff check .
    .venv/bin/pytest -q
)

# Secrets, real group config, sessions, SQLite, logs and the remote venv stay on the host.
rsync -az --delete --no-owner --no-group \
    --exclude '.env' \
    --exclude 'config/groups.yaml*' \
    --exclude 'data/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '*.egg-info/' \
    --exclude '.DS_Store' \
    "$PROJECT_DIR/" "$HOST:$REMOTE_DIR/"

ssh "$HOST" bash -s -- "$REMOTE_DIR" "$SERVICE" <<'REMOTE'
set -euo pipefail
cd "$1"
.venv/bin/pip install --quiet -e .
install -m 644 deploy/systemd/tg-digest.service "/etc/systemd/system/$2"
systemctl daemon-reload
systemctl restart "$2"
sleep 5
systemctl --no-pager --lines=0 status "$2"
tail -n 20 data/tg-digest.log
REMOTE
