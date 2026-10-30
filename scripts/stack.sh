#!/usr/bin/env bash
# scripts/stack.sh — manage the full CIP simulation stack.
#
# Starts worker pool, Django, event consumer, and gateway as detached
# background processes. Waits for each to become ready, creates a
# session via the REST API, and prints the CODESYS connection info.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$PROJECT_ROOT/.run"
LOG_DIR="$RUN_DIR/logs"
PID_DIR="$RUN_DIR/pids"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
# Reads $PROJECT_ROOT/.env if it exists. Shell-set variables take
# precedence, so "SERVER_IP=1.2.3.4 ./scripts/stack.sh up" overrides the
# file for one run without editing it.
#
# Supported syntax: KEY=value, optional single or double quotes, blank
# lines, and # comments. No export prefix, no line continuation.
if [ -f "$PROJECT_ROOT/.env" ]; then
    while IFS='=' read -r _key _value || [ -n "$_key" ]; do
        _key="${_key#"${_key%%[![:space:]]*}"}"
        _key="${_key%"${_key##*[![:space:]]}"}"
        [ -z "$_key" ] && continue
        [ "${_key:0:1}" = "#" ] && continue
        _value="${_value#"${_value%%[![:space:]]*}"}"
        _value="${_value%"${_value##*[![:space:]]}"}"
        case "$_value" in
            \"*\") _value="${_value#\"}"; _value="${_value%\"}" ;;
            \'*\') _value="${_value#\'}"; _value="${_value%\'}" ;;
        esac
        # Shell env wins over .env.
        if [ -z "${!_key+x}" ]; then
            export "$_key=$_value"
        fi
    done < "$PROJECT_ROOT/.env"
    unset _key _value
fi

if [ -z "${SERVER_IP:-}" ]; then
    SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
SERVER_IP="${SERVER_IP:-127.0.0.1}"

GATEWAY_PORT="${GATEWAY_PORT:-8080}"
DJANGO_PORT="${DJANGO_PORT:-8000}"
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
OPCUA_PORT_START="${OPCUA_PORT_START:-5000}"
OPCUA_PORT_END="${OPCUA_PORT_END:-5100}"
READY_TIMEOUT="${READY_TIMEOUT:-15}"
GATEWAY_PUBLIC_HOST="${GATEWAY_PUBLIC_HOST:-$SERVER_IP:$GATEWAY_PORT}"

# ---------- helpers ----------

log()  { printf '\033[1;36m[stack]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[stack]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[stack]\033[0m %s\n' "$*" >&2; }

ensure_dirs() { mkdir -p "$LOG_DIR" "$PID_DIR"; }
pidfile()     { echo "$PID_DIR/$1.pid"; }
logfile()     { echo "$LOG_DIR/$1.log"; }

is_running() {
    local f; f="$(pidfile "$1")"
    [ -f "$f" ] || return 1
    kill -0 "$(cat "$f")" 2>/dev/null
}

start_service() {
    local name="$1"; shift
    if is_running "$name"; then
        warn "$name already running (pid $(cat "$(pidfile "$name")"))"
        return 0
    fi
    log "starting $name..."
    # setsid detaches into a new session so Ctrl-C on this script
    # won't kill the service.
    setsid "$@" > "$(logfile "$name")" 2>&1 < /dev/null &
    echo $! > "$(pidfile "$name")"
}

stop_service() {
    local name="$1" f pid
    f="$(pidfile "$name")"
    [ -f "$f" ] || return 0
    pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
        log "stopping $name (pid $pid)"
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 25); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "$name ignored SIGTERM, sending SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    rm -f "$f"
}

wait_for_log() {
    local name="$1" pattern="$2" timeout="${3:-$READY_TIMEOUT}"
    local f deadline now
    f="$(logfile "$name")"
    deadline=$(( $(date +%s) + timeout ))
    while :; do
        now=$(date +%s)
        [ "$now" -ge "$deadline" ] && return 1
        [ -f "$f" ] && grep -q "$pattern" "$f" && return 0
        sleep 0.3
    done
}

activate_venv() {
    cd "$PROJECT_ROOT"
    if [ ! -f .venv/bin/activate ]; then
        err "no .venv — run: python3 -m venv .venv && pip install -r requirements.txt"
        exit 1
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate
}

# ---------- commands ----------

cmd_up() {
    ensure_dirs
    activate_venv

    start_service worker-pool \
        env REDIS_URL="$REDIS_URL" \
            OPCUA_PORT_START="$OPCUA_PORT_START" \
            OPCUA_PORT_END="$OPCUA_PORT_END" \
            python -m worker_pool.daemon --advertise-host 127.0.0.1

    start_service django \
        env REDIS_URL="$REDIS_URL" \
            GATEWAY_PUBLIC_HOST="$GATEWAY_PUBLIC_HOST" \
            PYTHONUNBUFFERED=1 \
            uvicorn control_plane.asgi:application \
                --host 0.0.0.0 --port "$DJANGO_PORT"

    start_service event-consumer \
        env REDIS_URL="$REDIS_URL" \
            python manage.py run_event_consumer

    start_service gateway \
        env REDIS_URL="$REDIS_URL" \
            python -m gateway.main --advertise-host "$SERVER_IP" --port "$GATEWAY_PORT"

    log "waiting for services to become ready..."

    wait_for_log worker-pool       '"msg":"daemon ready"'                || { err "worker-pool not ready";    cmd_status; return 1; }
    wait_for_log django            'Application startup complete'         || { err "django not ready";         cmd_status; return 1; }
    wait_for_log event-consumer    'event consumer started'              || { err "event-consumer not ready"; cmd_status; return 1; }
    wait_for_log gateway           '"msg":"gateway listening"'           || { err "gateway not ready";        cmd_status; return 1; }

    log "all services ready"
    echo
    cmd_create "$@"
}

cmd_down() {
    ensure_dirs
    log "stopping all services..."
    stop_service gateway
    stop_service event-consumer
    stop_service django
    stop_service worker-pool
    pkill -f 'worker_pool.worker.main' 2>/dev/null || true
    sleep 1
    log "all stopped"
}

cmd_reset() {
    cmd_down
    log "wiping state..."
    redis-cli --scan --pattern 'cip:*' 2>/dev/null | xargs -r redis-cli DEL >/dev/null || true
    redis-cli DEL cip:cmd cip:evt >/dev/null 2>&1 || true
    rm -f "$LOG_DIR"/*.log
    cmd_up "$@"
}

cmd_status() {
    ensure_dirs
    printf '%-18s %-10s %s\n' "SERVICE" "STATUS" "DETAILS"
    printf '%-18s %-10s %s\n' "-------" "------" "-------"
    for name in worker-pool django event-consumer gateway; do
        if is_running "$name"; then
            printf '%-18s \033[1;32m%-10s\033[0m pid %s\n' "$name" "running" "$(cat "$(pidfile "$name")")"
        else
            printf '%-18s \033[1;31m%-10s\033[0m\n' "$name" "stopped"
        fi
    done
    echo
    echo "Listening ports:"

    local ports
    ports=$(ss -tlnp 2>/dev/null | awk -v gw="$GATEWAY_PORT" \
                                      -v dj="$DJANGO_PORT" \
                                      -v ps="$OPCUA_PORT_START" \
                                      -v pe="$OPCUA_PORT_END" '
        /LISTEN/ {
            n = split($4, addr, ":")
            port = addr[n] + 0
            if (port == gw || port == dj || (port >= ps && port <= pe)) {
                print "  " $4
            }
        }')

    if [ -z "$ports" ]; then
        echo "  (none)"
    else
        echo "$ports"
    fi
}

cmd_logs() {
    ensure_dirs
    if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--follow" ]; then
        log "tailing all logs — Ctrl-C to stop"
        tail -F "$LOG_DIR"/*.log
    else
        for name in worker-pool django event-consumer gateway; do
            echo
            printf '\033[1;36m===== %s =====\033[0m\n' "$name"
            tail -25 "$(logfile "$name")" 2>/dev/null || echo "(no log yet)"
        done
    fi
}

cmd_create() {
    activate_venv

    local slug=""
    local label="codesys test"
    while [ $# -gt 0 ]; do
        case "$1" in
            --slug)
                [ -n "${2:-}" ] || { err "--slug requires a value"; return 1; }
                slug="$2"; shift 2 ;;
            --slug=*)
                slug="${1#--slug=}"; shift ;;
            *)
                label="$1"; shift ;;
        esac
    done

    if [ -n "$slug" ]; then
        log "creating session (slug: $slug, label: $label)..."
    else
        log "creating session (label: $label)..."
    fi

    # Build the JSON body in python so label content can't break the
    # request when it contains quotes or backslashes.
    local body
    body=$(python - "$slug" "$label" <<'PY'
import json, sys
slug, label = sys.argv[1], sys.argv[2]
payload = {"simulation_id": "cip", "label": label}
if slug:
    payload["slug"] = slug
print(json.dumps(payload))
PY
)

    local response
    response=$(curl -sS -X POST "http://127.0.0.1:$DJANGO_PORT/api/sessions/" \
        -H 'Content-Type: application/json' \
        -d "$body") || {
        err "failed to POST to Django at 127.0.0.1:$DJANGO_PORT"
        return 1
    }

    local parsed session_id external_id slug_value token
    parsed=$(echo "$response" | python -c "
import sys, json
d = json.load(sys.stdin)
print('|'.join([
    d.get('id', ''),
    d.get('external_id', ''),
    d.get('slug') or '',
    d.get('token', ''),
]))
" 2>/dev/null || true)

    if [ -z "$parsed" ]; then
        err "session create returned a non-JSON response"
        # Django renders its DEBUG error page as HTML. Dumping the
        # whole thing to the terminal is useless; show a short head
        # and point at the log for the full traceback.
        case "$response" in
            '<'*)
                warn "response looks like Django's debug error page."
                warn "Check the traceback:"
                warn "    tail -60 .run/logs/django.log"
                ;;
            *)
                echo "$response"
                ;;
        esac
        return 1
    fi

    IFS='|' read -r session_id external_id slug_value token <<< "$parsed"

    if [ -z "$session_id" ]; then
        err "session create returned no id"
        echo "$response"
        return 1
    fi

    log "session id: $session_id"
    log "waiting for it to become 'running'..."

    local status="" deadline=$(( $(date +%s) + 15 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        status=$(curl -sS "http://127.0.0.1:$DJANGO_PORT/api/sessions/$token/" \
            | python -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
        [ "$status" = "running" ] && break
        sleep 0.5
    done

    if [ "$status" != "running" ]; then
        err "session never reached 'running' (last: $status)"
        return 1
    fi

    echo
    echo "================================================================="
    echo "  Session created"
    echo "================================================================="
    echo "  Endpoint URL:   opc.tcp://$SERVER_IP:$GATEWAY_PORT/gateway/"
    echo "  Session UUID:   $session_id"
    if [ -n "$slug_value" ]; then
        echo "  Slug:           $slug_value"
        echo "  OPC UA path:    Gateway.Sessions.$slug_value"
    fi
    echo "  Token:          $token"
    echo "================================================================="
    echo
    if [ -n "$slug_value" ]; then
        echo "  CODESYS Data Sources path:"
        echo "    Datasource.Gateway.Sessions.$slug_value.*"
        echo
        echo "  Recreate with the same slug to keep this path valid:"
        echo "    ./scripts/stack.sh create --slug $slug_value \"next run\""
    else
        echo "  Tip: pass --slug NAME to get a stable OPC UA path that"
        echo "       survives session re-creation:"
        echo "    ./scripts/stack.sh create --slug cip_live \"next run\""
    fi
    echo
}

cmd_test() {
    activate_venv
    local session_id="${1:-}"
    if [ -z "$session_id" ]; then
        session_id=$(python tools/opcua_cli.py \
            --endpoint "opc.tcp://$SERVER_IP:$GATEWAY_PORT/gateway/" \
            --list-sessions 2>/dev/null | head -1)
    fi
    if [ -z "$session_id" ]; then
        err "no sessions on gateway — run './stack.sh up' or './stack.sh create'"
        return 1
    fi

    local ep="opc.tcp://$SERVER_IP:$GATEWAY_PORT/gateway/"
    log "testing session $session_id"
    echo

    log "1. initial state"
    python tools/opcua_cli.py --endpoint "$ep" --session "$session_id" \
        --read LevelPercent FlowLpm WaterValveCmd 2>&1 | grep -v 'session timeout'
    echo

    log "2. opening water valve for 3 seconds..."
    python tools/opcua_cli.py --endpoint "$ep" --session "$session_id" \
        WaterValveCmd=1 2>&1 | grep -v 'session timeout'
    sleep 3

    log "3. after 3 seconds"
    python tools/opcua_cli.py --endpoint "$ep" --session "$session_id" \
        --read LevelPercent FlowLpm WaterValveCmd 2>&1 | grep -v 'session timeout'
    echo

    log "4. closing water valve"
    python tools/opcua_cli.py --endpoint "$ep" --session "$session_id" \
        WaterValveCmd=0 2>&1 | grep -v 'session timeout'
    echo

    log "done — if LevelPercent climbed in step 3, the pipeline works"
}

cmd_shell() {
    activate_venv
    python "$@"
}

cmd_cli() {
    activate_venv
    python tools/opcua_cli.py \
        --endpoint "opc.tcp://$SERVER_IP:$GATEWAY_PORT/gateway/" "$@"
}

usage() {
    cat <<EOF
Usage: scripts/stack.sh <command> [args]

Commands:
  up [--slug NAME] [label]
                   Start all services, wait, and create a session.
                   --slug NAME gives the session a stable OPC UA
                   identifier so the CODESYS Data Sources tree does
                   not need updating between runs.
                   Default label: "codesys test".
  down             Stop all services and stray workers.
  reset [--slug NAME] [label]
                   Down + wipe Redis + up. Fresh state.
  status           Show service states and listening ports.
  logs [-f]        Show last lines of each service log, or tail -f.
  create [--slug NAME] [label]
                   Create another session without touching services.
  test [session]   Run CLI round-trip test on a session (default: first).
  shell [args]     Open a Python shell inside the venv.
  cli [args]       Run tools/opcua_cli.py against the gateway.

Environment overrides:
  SERVER_IP        default: first non-loopback IP (hostname -I)
  GATEWAY_PORT     default: 8080
  DJANGO_PORT      default: 8000
  OPCUA_PORT_START default: 5000
  OPCUA_PORT_END   default: 5100

Examples:
  ./scripts/stack.sh up --slug cip_live
  ./scripts/stack.sh test
  ./scripts/stack.sh create --slug alice_cip "alice tank"
  ./scripts/stack.sh cli --list-sessions
  ./scripts/stack.sh logs -f
  ./scripts/stack.sh down
EOF
}

case "${1:-}" in
    up)     shift; cmd_up     "$@" ;;
    down)   cmd_down ;;
    reset)  shift; cmd_reset  "$@" ;;
    status) cmd_status ;;
    logs)   shift; cmd_logs   "$@" ;;
    create) shift; cmd_create "$@" ;;
    test)   shift; cmd_test   "$@" ;;
    shell)  shift; cmd_shell  "$@" ;;
    cli)    shift; cmd_cli    "$@" ;;
    ""|help|-h|--help) usage ;;
    *) err "unknown command: $1"; echo; usage; exit 1 ;;
esac