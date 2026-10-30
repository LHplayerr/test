#!/usr/bin/env bash
# deploy.sh — safe update runner for the CIP Simulation Platform.
#
# Run this on the server as root (or with sudo). Never overwrites
# /etc/cip-sim/env or the SQLite database.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cip-sim}"
CONFIG_DIR="${CONFIG_DIR:-/etc/cip-sim}"
SERVICE_USER="${SERVICE_USER:-cip}"

banner() { printf '\n\033[1;36m========== %s ==========\033[0m\n' "$*"; }
step()   { printf '\033[1;34m--> %s\033[0m\n' "$*"; }
ok()     { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
fail()   { printf '    \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

trap 'fail "deploy aborted at line $LINENO"' ERR

[[ $EUID -eq 0 ]] || fail "run this script as root (sudo $0)"
[[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git checkout"

banner "deploy: $(date -u +%FT%TZ)"

step "fetching latest code"
sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch --quiet --prune
BRANCH=$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)
BEFORE=$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-parse HEAD)
sudo -u "$SERVICE_USER" git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH"
AFTER=$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-parse HEAD)
if [[ "$BEFORE" == "$AFTER" ]]; then
    ok "already up to date ($AFTER)"
else
    ok "updated $BEFORE -> $AFTER"
fi

step "pip install -r requirements.txt"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --quiet \
    -r "$APP_DIR/requirements.txt"
ok "dependencies in sync"

# Helper: run a Django command with the server env, without touching the env file.
run_django() {
    sudo -u "$SERVICE_USER" env $(grep -v '^#' "$CONFIG_DIR/env" | xargs) \
        "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" "$@" \
        --settings=control_plane.settings.prod
}

step "python manage.py migrate"
run_django migrate --noinput
ok "migrations applied"

step "python manage.py sync_catalog"
run_django sync_catalog
ok "catalog synced"

step "restarting services"
for unit in cip-control-plane cip-event-consumer cip-worker-pool cip-gateway; do
    systemctl restart "$unit"
    ok "$unit restarted"
done

step "health check"
sleep 2
for unit in cip-control-plane cip-worker-pool cip-event-consumer cip-gateway; do
    if ! systemctl is-active --quiet "$unit"; then
        fail "$unit failed to start; check 'journalctl -u $unit -n 50'"
    fi
done
ok "all services active"

banner "deploy complete"