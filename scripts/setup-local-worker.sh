#!/bin/bash

# Set up the native LOCAL SAQ tools-worker on macOS (launchd).
#
# This worker runs role "local" on your own machine and executes host-access
# tools (shell, SSH keys, gh, Docker, filesystem) that the deployed remote stack
# dispatches to you. It talks to the remote's Redis/Qdrant/Ollama/web (REMOTE_HOST
# in deploy.env), which must be reachable from here.
#
#   scripts/setup-local-worker.sh [install|uninstall|status]   (default: install)
#
# Linux: launchd isn't available — run the same command under systemd or directly:
#   AGENTFORGE_WORKER_ROLE=local REDIS_URL=redis://<remote>:6379 \
#   QDRANT_HOST=<remote> OLLAMA_HOST=http://<remote>:11434 \
#   AGENTFORGE_WEB_URL=http://<remote>:8200 \
#   .venv/bin/saq -v web.server.queue.settings_tools.settings

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LABEL="com.agentforge.worker-local-tools"
TEMPLATE="${PROJECT_ROOT}/worker/${LABEL}.plist.template"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOGDIR="${HOME}/Library/Logs/${LABEL}"
VENV="${AGENTFORGE_VENV:-${PROJECT_ROOT}/.venv}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ACTION="${1:-install}"

if ! command -v launchctl >/dev/null 2>&1; then
    echo "launchctl not found — this script is macOS-only (see the Linux note in the header)." >&2
    exit 1
fi

uninstall() {
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    rm -f "${DEST}"
    echo -e "${GREEN}Removed${NC} ${LABEL}"
}

status() {
    if launchctl list 2>/dev/null | awk '{print $3}' | grep -Fxq "${LABEL}"; then
        echo -e "${GREEN}loaded${NC}: ${LABEL}"
        echo "log: ${LOGDIR}/worker.log"
        tail -n 15 "${LOGDIR}/worker.log" 2>/dev/null || true
    else
        echo "not loaded: ${LABEL}"
    fi
}

case "${ACTION}" in
    uninstall) uninstall; exit 0 ;;
    status)    status;    exit 0 ;;
    install)   ;;
    *) echo "Usage: setup-local-worker.sh [install|uninstall|status]" >&2; exit 2 ;;
esac

# deploy.env gives us REMOTE_HOST (the deployed stack's reachable address).
if [ -f "${PROJECT_ROOT}/deploy.env" ]; then
    set -a; . "${PROJECT_ROOT}/deploy.env"; set +a
fi
REMOTE_HOST="${REMOTE_HOST:?set REMOTE_HOST in deploy.env to the deployed-stack IP or hostname}"

# 1) venv with the service deps (saq + web/server modules).
if [ ! -x "${VENV}/bin/saq" ]; then
    echo -e "${GREEN}Creating venv + installing production deps (this takes a minute)...${NC}"
    [ -d "${VENV}" ] || python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install -q -e "${PROJECT_ROOT}"
fi

# 2) Generate the plist from the template.
mkdir -p "${HOME}/Library/LaunchAgents" "${LOGDIR}"
sed -e "s|__VENV__|${VENV}|g" \
    -e "s|__REPO__|${PROJECT_ROOT}|g" \
    -e "s|__REMOTE_HOST__|${REMOTE_HOST}|g" \
    -e "s|__HOME__|${HOME}|g" \
    -e "s|__TOOL_PLUGINS__|${AGENTFORGE_TOOL_PLUGINS:-}|g" \
    "${TEMPLATE}" > "${DEST}"

# 3) (Re)load it. bootout is async — bootstrapping immediately after races it and
# fails with "5: Input/output error". Settle, clear any disabled flag, then retry.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
sleep 1
launchctl enable "gui/$(id -u)/${LABEL}" 2>/dev/null || true
for _try in 1 2 3; do
    launchctl bootstrap "gui/$(id -u)" "${DEST}" && break
    echo "  bootstrap attempt ${_try} failed (launchd still settling) — retrying..."
    sleep 2
done

echo -e "\n${GREEN}Installed + loaded${NC} ${LABEL}"
echo -e "  role:   local   ->  agentforge:tools:local"
echo -e "  remote: ${REMOTE_HOST} (Redis 6379, Qdrant 6333, Ollama 11434, web 8200)"
echo -e "  log:    ${YELLOW}${LOGDIR}/worker.log${NC}"
echo -e "  status: ${YELLOW}scripts/setup-local-worker.sh status${NC}   stop: ${YELLOW}... uninstall${NC}"
