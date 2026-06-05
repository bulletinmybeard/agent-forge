#!/bin/bash

# Teardown the AgentForge stack on the remote — data-preserving.
#
# Removes the containers without touching persistent data: NEVER passes
# `--volumes` and never deletes ${REMOTE_DIR}/{data,secrets}. The Qdrant
# vectors (agentforge-qdrant-data), Redis append-only data, all SQLite DBs,
# and secrets survive. A later deploy-remote.sh brings the stack back.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Deployment settings live in deploy.env (gitignored).
if [ -f "${PROJECT_ROOT}/deploy.env" ]; then
    set -a; . "${PROJECT_ROOT}/deploy.env"; set +a
else
    echo "deploy.env not found — copy deploy.example.env to deploy.env and fill it in." >&2
    exit 1
fi
. "${SCRIPT_DIR}/lib/resolve-deploy.sh"   # for ALL_COMPOSE_PROFILES

SSH_HOST="${REMOTE_SSH_HOST:?set REMOTE_SSH_HOST in deploy.env}"
REMOTE_DIR="${REMOTE_DIR:-/opt/agentforge}"
SSH_OPTS="-o ControlMaster=auto -o ControlPath=/tmp/agentforge-teardown-%C -o ControlPersist=120"
SSH_CMD="ssh ${SSH_OPTS}"

REMOVE_IMAGES=false
ASSUME_YES=false
KEEP_WORKER=false
for arg in "$@"; do
    case "$arg" in
        --rmi)         REMOVE_IMAGES=true ;;
        -y|--yes)      ASSUME_YES=true ;;
        --keep-worker) KEEP_WORKER=true ;;
        -h|--help)
            cat <<'HLP'
Usage: teardown-remote.sh [--rmi] [-y] [--keep-worker]

Stops and removes the AgentForge containers on the remote. Persistent data
(Qdrant volume, Redis data, SQLite DBs, secrets) is always preserved.

  --rmi          Also remove the built agentforge-{api,web,sidecar}:latest images
  -y, --yes      Skip the confirmation prompt
  --keep-worker  Don't stop the native local worker
HLP
            exit 0 ;;
    esac
done

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

echo -e "${BLUE}AgentForge teardown — target: ${SSH_HOST}:${REMOTE_DIR}${NC}"
echo -e "${GREEN}Preserved:${NC} agentforge-qdrant-data + agentforge-redis-data volumes, ${REMOTE_DIR}/{data,secrets}"

if [ "${ASSUME_YES}" != true ]; then
    printf "Continue? [y/N] "
    read -r reply
    case "$reply" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# The overlay interpolates ${PUBLIC_DOMAIN}/${PROXY_NETWORK}, so pass them
# through even for `down` (compose parses both files). COMPOSE_PROFILES enables
# every profile so `down` removes profile-gated services, not just the core.
COMPOSE_VARS="COMPOSE_PROFILES='${ALL_COMPOSE_PROFILES}' PUBLIC_DOMAIN='${PUBLIC_DOMAIN}' QDRANT_DOMAIN='${QDRANT_DOMAIN}' SAQ_DOMAIN='${SAQ_DOMAIN}' PROXY_NETWORK='${PROXY_NETWORK}' REMOTE_REDIS_URL='${REMOTE_REDIS_URL}'"

echo -e "\n${GREEN}Stopping the stack...${NC}"
# --remove-orphans clears anything since deleted from the compose file too.
${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_VARS} docker compose -f docker-compose.yml -f docker-compose.remote.yml down --remove-orphans"

if [ "${REMOVE_IMAGES}" = true ]; then
    echo -e "\n${GREEN}Removing built images...${NC}"
    ${SSH_CMD} "${SSH_HOST}" "docker image rm agentforge-api:latest agentforge-web:latest agentforge-sidecar:latest 2>/dev/null || true"
fi

# Stop the native local worker so it stops retrying the now-down remote Redis.
if [ "${KEEP_WORKER}" != true ] && command -v launchctl >/dev/null 2>&1; then
    echo -e "\n${GREEN}Stopping the native local worker...${NC}"
    "${SCRIPT_DIR}/setup-local-worker.sh" uninstall || true
fi

echo -e "\n${GREEN}Verifying preserved data...${NC}"
${SSH_CMD} "${SSH_HOST}" "
  echo -n '  qdrant volume: '; docker volume ls --format '{{.Name}}' | grep -q agentforge-qdrant-data && echo present || echo 'absent'
  echo -n '  data dir:      '; du -sh ${REMOTE_DIR}/data 2>/dev/null || echo 'absent'
  echo -n '  containers:    '; cd ${REMOTE_DIR} && ${COMPOSE_VARS} docker compose -f docker-compose.yml -f docker-compose.remote.yml ps -q | wc -l | tr -d ' '; echo ' running'
"
echo -e "\n${GREEN}Done.${NC} Bring it back with: ${YELLOW}scripts/deploy-remote.sh${NC}"
