#!/usr/bin/env bash
# Deploy vest-mini-app to the production VPS.
#
# Usage: ./deploy.sh
#
# Syncs the app to the server, (re)creates the venv, installs deps,
# and restarts the systemd service. Run scripts/remote_setup.sh once
# beforehand (or let this script bootstrap it) to install system
# packages, nginx and the systemd unit.

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-root@191.44.109.10}"
REMOTE_DIR="${REMOTE_DIR:-/opt/vest-mini-app}"
SERVICE_NAME="vest-mini-app"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Syncing code to ${REMOTE_HOST}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.env' \
  --exclude '.git' \
  --exclude '.DS_Store' \
  "${SCRIPT_DIR}/" "${REMOTE_HOST}:${REMOTE_DIR}/"

echo "==> Installing dependencies and restarting service"
ssh "${REMOTE_HOST}" bash -s <<EOF
set -euo pipefail
cd "${REMOTE_DIR}"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  echo "!! No .env found on server at ${REMOTE_DIR}/.env — create one from .env.example before starting the service."
fi

systemctl restart ${SERVICE_NAME}
systemctl --no-pager --lines=5 status ${SERVICE_NAME}
EOF

echo "==> Done. Health check:"
ssh "${REMOTE_HOST}" "curl -fsS http://127.0.0.1:8080/healthz || true"
echo
