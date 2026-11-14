#!/usr/bin/env bash
# Lock down the CIP Simulation Platform host with UFW.
# Intended for a fresh Ubuntu 22.04 install with no existing UFW rules.
#
# Usage:
#   sudo INTRANET_CIDR=10.0.0.0/24 ./deploy/firewall/setup-ufw.sh
#
# Only these ports are opened, and only to the intranet:
#   22    SSH
#   8000  Django UI + REST API
#   8080  OPC UA gateway (CODESYS / SCADA clients)
# Worker OPC UA ports (5000-5100) and HTTP ports are loopback-only
# and must not be opened.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run this script as root (sudo)" >&2
    exit 1
fi

INTRANET_CIDR="${INTRANET_CIDR:-10.0.0.0/24}"

banner() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()     { printf '   \033[1;32mok\033[0m  %s\n' "$*"; }

banner "configuring UFW (intranet: ${INTRANET_CIDR})"

if ! command -v ufw >/dev/null 2>&1; then
    echo "Installing ufw..."
    apt-get update -qq
    apt-get install -y -qq ufw
fi

ufw --force default allow incoming
ufw --force default allow outgoing
ok "default policy: allow incoming, allow outgoing"

ufw allow from "$INTRANET_CIDR" to any port 22 proto tcp comment 'SSH (intranet)'
ufw allow from "$INTRANET_CIDR" to any port 8000 proto tcp comment 'Django UI (intranet)'
ufw allow from "$INTRANET_CIDR" to any port 8080 proto tcp comment 'OPC UA gateway (intranet)'
ok "intranet rules added for 22/tcp, 8000/tcp, 8080/tcp"

ufw --force enable
ufw reload >/dev/null
ok "ufw enabled"

echo
ufw status verbose
echo
echo "Firewall is active. To add another intranet range:"
echo "sudo ufw allow from 10.0.1.0/24 to any port 8000 proto tcp"