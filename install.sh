#!/usr/bin/env bash
# install.sh — provision a fresh Ubuntu 22.04 LTS server for the CIP
# Simulation Platform.
#
# Run this ON THE SERVER as root (or via sudo). Idempotent: safe to
# run again after a partial install.
#
# What it does:
#   1. Installs apt packages: Python 3.11, venv, Redis, git, build-essential
#   2. Creates the dedicated system user 'cip' and its runtime dirs
#   3. Clones/updates the repo into /opt/cip-sim (or copies the current tree)
#   4. Builds /opt/cip-sim/.venv and pip-installs requirements.txt
#   5. Seeds /etc/cip-sim/env from .env.production.example on first install
#   6. Migrates the DB and syncs the simulation catalog
#   7. Installs and enables the systemd units
#   8. Runs the firewall script if INTRANET_CIDR is set
set -euo pipefail

# ---------- config ----------
REPO_URL="${REPO_URL:-}"                    # optional; empty = use current tree
APP_DIR="${APP_DIR:-/opt/cip-sim}"
CONFIG_DIR="${CONFIG_DIR:-/etc/cip-sim}"
STATE_DIR="${STATE_DIR:-/var/lib/cip-sim}"
LOG_DIR="${LOG_DIR:-/var/log/cip-sim}"
SERVICE_USER="${SERVICE_USER:-cip}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
INTRANET_CIDR="${INTRANET_CIDR:-}"

# ---------- presentation ----------
banner()  { printf '\n\033[1;36m========== %s ==========\033[0m\n' "$*"; }
step()    { printf '\033[1;34m--> %s\033[0m\n' "$*"; }
ok()      { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
warn()    { printf '    \033[1;33mwarn\033[0m %s\n' "$*"; }
fail()    { printf '    \033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

trap 'fail "install aborted at line $LINENO"' ERR

# ---------- preflight ----------
banner "preflight"

[[ $EUID -eq 0 ]] || fail "run this script as root (sudo $0)"
command -v apt-get >/dev/null || fail "this installer targets Ubuntu 22.04 (apt-get not found)"

if ! grep -qi 'ubuntu 22.04' /etc/os-release; then
    warn "not on Ubuntu 22.04 — continuing anyway (best effort)"
fi
ok "on $(. /etc/os-release && echo "$PRETTY_NAME")"

# ---------- apt packages ----------
banner "apt packages"

step "updating package index"
apt-get update -qq
ok "apt update"

step "installing base packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    software-properties-common ca-certificates curl git \
    build-essential pkg-config
ok "base packages"

step "adding deadsnakes PPA for Python 3.11"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
fi
ok "$PYTHON_BIN available"

step "installing Python 3.11 + venv + Redis + UFW"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    "${PYTHON_BIN}" "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev" \
    redis-server ufw
ok "python/redis/ufw installed"

step "enabling redis-server"
systemctl enable --now redis-server
ok "redis active"

# ---------- service user ----------
banner "service user + directories"

if id "$SERVICE_USER" &>/dev/null; then
    ok "user '$SERVICE_USER' already exists"
else
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "created system user '$SERVICE_USER'"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$CONFIG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$LOG_DIR"
ok "directories: $APP_DIR $CONFIG_DIR $STATE_DIR $LOG_DIR"

# ---------- source tree ----------
banner "source tree"

if [[ -n "$REPO_URL" ]]; then
    if [[ -d "$APP_DIR/.git" ]]; then
        step "updating existing checkout"
        git -C "$APP_DIR" fetch --quiet
        git -C "$APP_DIR" reset --hard --quiet "origin/$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)"
    else
        step "cloning $REPO_URL"
        git clone --quiet "$REPO_URL" "$APP_DIR"
    fi
    chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
    ok "source updated from git"
else
    # Running from a local tree: rsync everything except .git and .venv.
    if [[ -d "$(dirname "$0")/.git" ]] && [[ "$(cd "$(dirname "$0")" && pwd)" != "$(cd "$APP_DIR" && pwd)" ]]; then
        step "rsyncing local tree into $APP_DIR"
        command -v rsync >/dev/null || apt-get install -y -qq rsync
        rsync -a --delete --exclude .git --exclude .venv --exclude db.sqlite3 \
              --exclude .run --exclude __pycache__ \
              "$(cd "$(dirname "$0")" && pwd)/" "$APP_DIR/"
        chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
        ok "source synced"
    else
        ok "running inside $APP_DIR; no copy needed"
        chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
    fi
fi

# ---------- venv ----------
banner "virtualenv"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    step "creating .venv with $PYTHON_BIN"
    sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
    ok ".venv created"
else
    ok ".venv already present"
fi

step "pip install -r requirements.txt"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "dependencies installed"

# ---------- env file ----------
banner "environment file"

if [[ ! -f "$CONFIG_DIR/env" ]]; then
    step "seeding $CONFIG_DIR/env from .env.production.example"
    install -m 0640 -o root -g "$SERVICE_USER" "$APP_DIR/.env.production.example" "$CONFIG_DIR/env"

    # Generate a real SECRET_KEY in place.
    SECRET=$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(64))')
    sed -i "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=${SECRET}|" "$CONFIG_DIR/env"

    # Best-effort: put the server's primary IP into ALLOWED_HOSTS and GATEWAY_PUBLIC_HOST.
    SERVER_IP=$(hostname -I | awk '{print $1}')
    sed -i "s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=${SERVER_IP},localhost,127.0.0.1|" "$CONFIG_DIR/env"
    sed -i "s|^GATEWAY_PUBLIC_HOST=.*|GATEWAY_PUBLIC_HOST=${SERVER_IP}:8080|" "$CONFIG_DIR/env"

    ok "env file written (SECRET_KEY generated, hosts set to $SERVER_IP)"
    warn "review $CONFIG_DIR/env before going live"
else
    ok "$CONFIG_DIR/env already exists — left untouched"
fi

# ---------- database ----------
banner "database"

step "running migrations"
sudo -u "$SERVICE_USER" env $(grep -v '^#' "$CONFIG_DIR/env" | xargs) \
    "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" migrate --noinput \
    --settings=control_plane.settings.prod
ok "migrations applied"

step "syncing simulation catalog"
sudo -u "$SERVICE_USER" env $(grep -v '^#' "$CONFIG_DIR/env" | xargs) \
    "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" sync_catalog \
    --settings=control_plane.settings.prod
ok "catalog synced"

# ---------- systemd ----------
banner "systemd units"

# The ADVERTISE_HOST env var is read by the worker-pool and gateway units.
SERVER_IP=$(hostname -I | awk '{print $1}')
mkdir -p /etc/systemd/system/cip-worker-pool.service.d
cat > /etc/systemd/system/cip-worker-pool.service.d/advertise.conf <<EOF
[Service]
Environment=ADVERTISE_HOST=${SERVER_IP}
EOF
mkdir -p /etc/systemd/system/cip-gateway.service.d
cat > /etc/systemd/system/cip-gateway.service.d/advertise.conf <<EOF
[Service]
Environment=ADVERTISE_HOST=${SERVER_IP}
EOF
ok "advertise host: $SERVER_IP"

install -m 0644 "$APP_DIR"/deploy/systemd/cip-*.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/deploy/systemd/cip-purge-events.timer /etc/systemd/system/

systemctl daemon-reload
ok "daemon reloaded"

for unit in cip-control-plane cip-worker-pool cip-event-consumer cip-gateway; do
    systemctl enable --now "$unit"
    ok "$unit enabled and started"
done
systemctl enable --now cip-purge-events.timer
ok "cip-purge-events.timer enabled"

# ---------- firewall (optional) ----------
if [[ -n "$INTRANET_CIDR" ]]; then
    banner "firewall"
    INTRANET_CIDR="$INTRANET_CIDR" "$APP_DIR/deploy/firewall/setup-ufw.sh"
else
    warn "INTRANET_CIDR not set; skipping firewall configuration"
fi

# ---------- summary ----------
banner "install complete"
cat <<EOF

  App directory:   $APP_DIR
  Environment:     $CONFIG_DIR/env
  State / DB:      $STATE_DIR
  Logs:            $LOG_DIR (also: journalctl -u cip-*)

  Services (systemctl status cip-*):
    cip-control-plane.service   - Django HTTP/WebSocket on :8000
    cip-worker-pool.service     - worker pool daemon
    cip-event-consumer.service  - Redis -> DB event consumer
    cip-gateway.service         - OPC UA aggregation gateway on :8080
    cip-purge-events.timer      - nightly event-log purge

  Browser UI:      http://<this-server>:8000/
  OPC UA endpoint: opc.tcp://<this-server>:8080/gateway/

EOF