#!/bin/bash

# Build and deploy AgentForge on the remote — building ON the remote itself.
#
# Rsyncs the repo to the remote and runs `docker compose up -d --build` there
# (native x86_64, no cross-compilation). Persistent data (the qdrant volume +
# ./data + ./secrets) is preserved across deploys. Which services run is decided
# by COMPOSE_PROFILES, derived from AGENTFORGE_PRESET + the AGENTFORGE_REDIS/QDRANT
# toggles in deploy.env (set AGENTFORGE_REDIS=host to reuse the host Redis instead
# of the bundled container). After the stack is up, the native LOCAL worker on
# this machine is installed/refreshed.
#
# Flow:
#   1. Rsync repo to the remote (configs go along; no SPA build, no yq merge)
#   2. docker compose up -d --build (overlay: Traefik + host Redis)
#   3. Install / refresh the native local worker (macOS launchd)
#
# Settings (SSH host, dir, domains, proxy network, host Redis) come from
# deploy.env — copy deploy.example.env -> deploy.env and fill it in.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${PROJECT_ROOT}/deploy.env" ]; then
    set -a; . "${PROJECT_ROOT}/deploy.env"; set +a
else
    echo "deploy.env not found — copy deploy.example.env to deploy.env and fill it in." >&2
    exit 1
fi
. "${SCRIPT_DIR}/lib/resolve-deploy.sh"

SSH_HOST="${REMOTE_SSH_HOST:?set REMOTE_SSH_HOST in deploy.env}"
REMOTE_DIR="${REMOTE_DIR:-/opt/agentforge}"
SSH_OPTS="-o ControlMaster=auto -o ControlPath=/tmp/agentforge-deploy-%C -o ControlPersist=120"
SSH_CMD="ssh ${SSH_OPTS}"
SCP_CMD="scp ${SSH_OPTS}"
RSYNC_RSH="ssh ${SSH_OPTS}"

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.remote.yml"

# Selective build flags (default: build all via `up --build`)
NO_BUILD=false
NO_CACHE_FLAG=""
CONFIG_ONLY=false
NO_LOCAL_WORKER=false
WITH_CATALOG=false
BUILD_SERVICES=""     # empty = build all
RESTART_SERVICES=""   # restart-only fast path
PRESET_OVERRIDE=""
PROFILES_OVERRIDE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --no-build)         NO_BUILD=true ;;
        --no-cache)         NO_CACHE_FLAG="--no-cache" ;;
        --config-only)      CONFIG_ONLY=true ;;
        --no-local-worker)  NO_LOCAL_WORKER=true ;;
        --with-catalog)     WITH_CATALOG=true ;;
        --api-only)         BUILD_SERVICES="agentforge-api" ;;
        --web-only)         BUILD_SERVICES="agentforge-web" ;;
        --sidecar-only)     BUILD_SERVICES="agentforge-sidecar" ;;
        --saq-only)         RESTART_SERVICES="agentforge-worker-saq" ;;
        --saq-dash-only)    RESTART_SERVICES="agentforge-saq-web" ;;
        --saq-tools-only)   RESTART_SERVICES="agentforge-worker-saq-tools" ;;
        --workers-only)     RESTART_SERVICES="agentforge-worker-saq agentforge-worker-saq-tools agentforge-saq-web" ;;
        --preset)           PRESET_OVERRIDE="$2"; shift ;;
        --preset=*)         PRESET_OVERRIDE="${1#*=}" ;;
        --profiles)         PROFILES_OVERRIDE="$2"; shift ;;
        --profiles=*)       PROFILES_OVERRIDE="${1#*=}" ;;
        -h|--help)
            cat <<'HLP'
Usage: deploy-remote.sh [FLAGS]

Preset / profile selection (also settable in deploy.env):
  --preset light|full   light = web + one worker (+ redis/qdrant), in_process
                        dispatch; full = the whole stack (default)
  --profiles "a,b,c"    raw COMPOSE_PROFILES override; known:
                        redis qdrant api sidecar split dashboard full

Build / deploy flags:
  --api-only          Only rebuild + recreate the API service
  --web-only          Only rebuild + recreate the web service (workers share its image)
  --sidecar-only      Only rebuild + recreate the sidecar
  --no-build          Recreate containers without rebuilding images
  --no-cache          Force a clean build (no Docker layer cache)
  --config-only       Sync config + recreate the app services (no rebuild)
  --no-local-worker   Skip installing/refreshing the native local worker (macOS)
  --with-catalog      Also sync data/catalogs/*.json and bust the catalog:* Redis cache

Restart-only flags (no build; recreate the chosen services):
  --saq-only          Restart the agent-job worker
  --saq-dash-only     Restart the SAQ dashboard
  --saq-tools-only    Restart the tools worker
  --workers-only      Restart all SAQ workers + dashboard

Settings come from deploy.env (see deploy.example.env).
HLP
            exit 0 ;;
    esac
    shift
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

# Resolve the preset/profiles into the compose env + active-service list.
# Remote is multi-host by default (a native local worker on another machine),
# so split dispatch unless deploy.env / --preset says otherwise.
DEFAULT_DISPATCH="split"
resolve_deploy
# Vars the compose overlay interpolates — passed through to the remote compose.
# COMPOSE_PROFILES picks which services run; REMOTE_REDIS_URL stays the authoritative
# host-Redis knob on the remote (overlay reads it). Host Qdrant only when requested.
COMPOSE_ENV="COMPOSE_PROFILES='${RESOLVED_COMPOSE_PROFILES}' PUBLIC_DOMAIN='${PUBLIC_DOMAIN}' API_DOMAIN='${API_DOMAIN}' QDRANT_DOMAIN='${QDRANT_DOMAIN}' SAQ_DOMAIN='${SAQ_DOMAIN}' PROXY_NETWORK='${PROXY_NETWORK}' REMOTE_REDIS_URL='${REMOTE_REDIS_URL}' AGENTFORGE_DISPATCH_MODE='${RESOLVED_DISPATCH_MODE}' AGENTFORGE_TOOL_PLUGINS='${AGENTFORGE_TOOL_PLUGINS:-}' AGENTFORGE_ALLOW_INSECURE='${AGENTFORGE_ALLOW_INSECURE:-}' AGENTFORGE_API_KEYS='${AGENTFORGE_API_KEYS:-}' AGENTFORGE_REQUIRE_AUTH='${AGENTFORGE_REQUIRE_AUTH:-}' SIDECAR_AUTH_TOKEN='${SIDECAR_AUTH_TOKEN:-}' AGENTFORGE_INTERNAL_TOKEN='${AGENTFORGE_INTERNAL_TOKEN:-}'"
if [ -n "${RESOLVED_QDRANT_HOST}" ]; then
    COMPOSE_ENV="${COMPOSE_ENV} QDRANT_HOST='${RESOLVED_QDRANT_HOST}' QDRANT_PORT='${RESOLVED_QDRANT_PORT}'"
fi
# The Python app services to recreate on config changes, filtered to the active
# profile set (qdrant/redis are infra, left alone).
APP_SERVICES=""
for s in agentforge-api agentforge-web agentforge-worker-saq agentforge-worker-saq-tools agentforge-saq-web; do
    deploy_has_service "$s" && APP_SERVICES="${APP_SERVICES:+${APP_SERVICES} }$s"
done
# A targeted --*-only flag must name a service that the active preset includes.
for s in ${RESTART_SERVICES} ${BUILD_SERVICES}; do
    deploy_has_service "$s" || { echo -e "${RED}[FAIL] '${s}' is not in the active profile set (${RESOLVED_COMPOSE_PROFILES:-light core}). Adjust --preset/--profiles or AGENTFORGE_* in deploy.env.${NC}"; exit 2; }
done

# ── Helpers ───────────────────────────────────────────────────────────

check_ssh() {
    if ! ${SSH_CMD} -o ConnectTimeout=5 "${SSH_HOST}" "echo ok" > /dev/null 2>&1; then
        echo -e "${RED}[FAIL] Cannot reach ${SSH_HOST} via SSH${NC}"
        echo -e "${YELLOW}  Check that ${SSH_HOST} is in ~/.ssh/config and the host is reachable${NC}"
        exit 1
    fi
    echo -e "${GREEN}[OK] SSH connection to ${SSH_HOST}${NC}"
}

close_ssh() { ssh -O exit -o ControlPath=/tmp/agentforge-deploy-%C "${SSH_HOST}" 2>/dev/null || true; }

# Local secrets (connector OAuth tokens) -> remote ./secrets. Skips if absent.
sync_secrets() {
    local local_dir="${HOME}/.agentforge"
    [ -d "${local_dir}" ] || return 0
    echo -e "\n${GREEN}Syncing secrets (${local_dir} -> remote)...${NC}"
    ${SSH_CMD} "${SSH_HOST}" "mkdir -p ${REMOTE_DIR}/secrets && chmod 700 ${REMOTE_DIR}/secrets"
    rsync -az --chmod=F600 -e "${RSYNC_RSH}" "${local_dir}/" "${SSH_HOST}:${REMOTE_DIR}/secrets/"
    echo -e "${GREEN}[OK] secrets synced${NC}"
}

# Model-catalog JSONs + Redis cache bust. Gated behind --with-catalog.
sync_catalogs() {
    [ "${WITH_CATALOG}" = true ] || return 0
    local local_dir="${PROJECT_ROOT}/data/catalogs"
    if [ ! -d "${local_dir}" ]; then
        echo -e "${YELLOW}[WARN] --with-catalog set but ${local_dir} doesn't exist; skipping${NC}"
        return 0
    fi
    echo -e "\n${GREEN}Syncing model catalogs...${NC}"
    ${SSH_CMD} "${SSH_HOST}" "mkdir -p ${REMOTE_DIR}/data/catalogs"
    rsync -az -e "${RSYNC_RSH}" "${local_dir}/" "${SSH_HOST}:${REMOTE_DIR}/data/catalogs/"
    echo -e "${GREEN}Busting catalog:* Redis keys...${NC}"
    local keys="catalog:deepinfra catalog:openrouter catalog:ollama"
    if ${SSH_CMD} "${SSH_HOST}" "command -v redis-cli >/dev/null && redis-cli DEL ${keys}" >/dev/null 2>&1; then
        echo -e "${GREEN}[OK] Cache busted (host redis-cli)${NC}"
    else
        echo -e "${YELLOW}[WARN] Cache bust skipped (no host redis-cli) — wait for TTL.${NC}"
    fi
}

# Ensure a local user_context.md exists before sync. The :ro bind-mount needs a
# real file on the host — a missing path makes Docker create a directory at the
# mount point, which then trips the read_text() warning in _load_user_context.
# The live file is gitignored + private; fall back to the committed example.
ensure_user_context() {
    [ -f "${PROJECT_ROOT}/user_context.md" ] && return 0
    if [ -f "${PROJECT_ROOT}/user_context.example.md" ]; then
        cp "${PROJECT_ROOT}/user_context.example.md" "${PROJECT_ROOT}/user_context.md"
        echo -e "${YELLOW}[i] user_context.md missing — seeded from user_context.example.md${NC}"
    else
        : > "${PROJECT_ROOT}/user_context.md"
        echo -e "${YELLOW}[i] user_context.md missing — created empty placeholder${NC}"
    fi
}

# Install / refresh the native LOCAL tools-worker on this machine.
setup_local_worker() {
    if [ "${NO_LOCAL_WORKER}" = true ]; then
        echo -e "\n${YELLOW}[skip] --no-local-worker — native local worker not touched.${NC}"
    elif command -v launchctl >/dev/null 2>&1; then
        echo -e "\n${GREEN}Setting up the native local worker (role=local)...${NC}"
        "${SCRIPT_DIR}/setup-local-worker.sh" install \
            || echo -e "${YELLOW}[warn] local worker setup failed — run scripts/setup-local-worker.sh manually.${NC}"
    else
        echo -e "\n${YELLOW}[skip] no launchctl (not macOS). Run a 'local' worker yourself — see scripts/setup-local-worker.sh.${NC}"
    fi
}

# ── Restart-only fast path ────────────────────────────────────────────
# Sync the compose files + recreate just the requested services. No build,
# no source rsync, no local-worker reload (these never touch source/config).
if [ -n "${RESTART_SERVICES}" ]; then
    echo -e "${BLUE}=== AgentForge — restart-only: ${RESTART_SERVICES} ===${NC}"
    check_ssh
    echo -e "\n${GREEN}Syncing compose files...${NC}"
    ${SCP_CMD} "${PROJECT_ROOT}/docker-compose.yml" "${PROJECT_ROOT}/docker-compose.remote.yml" "${SSH_HOST}:${REMOTE_DIR}/"
    echo -e "\n${GREEN}Recreating: ${RESTART_SERVICES}${NC}"
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} up -d --force-recreate --no-deps ${RESTART_SERVICES}"
    ${SSH_CMD} "${SSH_HOST}" "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'agentforge|NAMES' || true"
    close_ssh
    echo -e "\n${GREEN}Restart-only done! $(date '+%Y-%m-%d %H:%M:%S')${NC}"
    exit 0
fi

# ── Config-only fast path ─────────────────────────────────────────────
# Sync the config/compose files + recreate the app services. No build.
if [ "${CONFIG_ONLY}" = true ]; then
    echo -e "${BLUE}=== AgentForge — config sync ===${NC}"
    check_ssh
    for f in framework-config.yaml config.yaml custom_agents.yaml; do
        [ -f "${PROJECT_ROOT}/${f}" ] || { echo -e "${RED}[FAIL] ${f} missing (copy from ${f%.yaml}.example.yaml).${NC}"; exit 1; }
    done
    ensure_user_context
    echo -e "\n${GREEN}Syncing config + compose...${NC}"
    ${SSH_CMD} "${SSH_HOST}" "mkdir -p ${REMOTE_DIR}/data"
    ${SCP_CMD} \
        "${PROJECT_ROOT}/docker-compose.yml" "${PROJECT_ROOT}/docker-compose.remote.yml" \
        "${PROJECT_ROOT}/framework-config.yaml" "${PROJECT_ROOT}/config.yaml" \
        "${PROJECT_ROOT}/user_context.md" \
        "${PROJECT_ROOT}/tool_routing.yaml" "${PROJECT_ROOT}/custom_agents.yaml" "${PROJECT_ROOT}/skills.yaml" \
        "${SSH_HOST}:${REMOTE_DIR}/"
    ${SSH_CMD} "${SSH_HOST}" "rm -rf ${REMOTE_DIR}/profiles && mkdir -p ${REMOTE_DIR}/profiles"
    ${SCP_CMD} -r "${PROJECT_ROOT}/profiles/." "${SSH_HOST}:${REMOTE_DIR}/profiles/"
    echo -e "${GREEN}[OK] config synced${NC}"
    sync_secrets
    sync_catalogs
    echo -e "\n${GREEN}Recreating app services to pick up config...${NC}"
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} up -d --force-recreate --no-deps ${APP_SERVICES}"
    setup_local_worker
    close_ssh
    echo -e "\n${GREEN}Config deployed! $(date '+%Y-%m-%d %H:%M:%S')${NC}"
    exit 0
fi

# ── Full build + deploy ───────────────────────────────────────────────
echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  AgentForge — build ON remote (${SSH_HOST})${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

check_ssh
for f in framework-config.yaml config.yaml; do
    if [ ! -f "${PROJECT_ROOT}/${f}" ]; then
        echo -e "${RED}[FAIL] ${f} not found.${NC} Copy it from ${f%.yaml}.example.yaml and add credentials." >&2
        exit 1
    fi
done
ensure_user_context

echo -e "\n${GREEN}Syncing source to ${SSH_HOST}:${REMOTE_DIR}...${NC}"
${SSH_CMD} "${SSH_HOST}" "mkdir -p ${REMOTE_DIR}/data ${REMOTE_DIR}/secrets"
rsync -az --delete -e "${RSYNC_RSH}" \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.egg-info' --exclude='node_modules' --exclude='dist' \
    --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='.pytest_cache' \
    --exclude='data' --exclude='secrets' \
    "${PROJECT_ROOT}/" "${SSH_HOST}:${REMOTE_DIR}/"
echo -e "${GREEN}[OK] source synced${NC}"

sync_secrets
sync_catalogs

# Build (selective if --api-only/--web-only/--sidecar-only, else all) then bring up.
# COMPOSE_PROFILES (in COMPOSE_ENV) decides which services start — including
# whether the bundled Redis/Qdrant run or a host service is reused.
echo -e "\n${GREEN}Building + starting on ${SSH_HOST} — preset='${PRESET_OVERRIDE:-${AGENTFORGE_PRESET:-full}}' profiles='${RESOLVED_COMPOSE_PROFILES:-light core}' dispatch='${RESOLVED_DISPATCH_MODE}'${NC}"
if [ -n "${BUILD_SERVICES}" ]; then
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} build ${NO_CACHE_FLAG} ${BUILD_SERVICES}"
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} up -d --no-deps ${BUILD_SERVICES}"
elif [ "${NO_BUILD}" = true ]; then
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} up -d"
elif [ -n "${NO_CACHE_FLAG}" ]; then
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} build --no-cache"
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} up -d"
else
    ${SSH_CMD} "${SSH_HOST}" "cd ${REMOTE_DIR} && ${COMPOSE_ENV} docker compose ${COMPOSE_FILES} up -d --build"
fi

# ── Health checks + prune ─────────────────────────────────────────────
# Only probe services the active preset actually starts.
api_active=false; deploy_has_service agentforge-api && api_active=true
WORKER_CHECK=""
for s in agentforge-worker-saq agentforge-worker-saq-tools agentforge-saq-web; do
    deploy_has_service "$s" && WORKER_CHECK="${WORKER_CHECK:+${WORKER_CHECK} }${s}-1"
done
echo -e "\n${GREEN}Waiting for containers...${NC}"
${SSH_CMD} "${SSH_HOST}" "
  sleep 6
  docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'agentforge|qdrant|NAMES' || true
  echo ''
  echo 'Health:'
  if ${api_active}; then curl -sf http://localhost:8100/health >/dev/null 2>&1 && echo '  [OK] api (8100)' || echo '  [WAIT] api still starting...'; fi
  curl -sf http://localhost:8200/ >/dev/null 2>&1 && echo '  [OK] web (8200)' || echo '  [WAIT] web still starting...'
  for s in ${WORKER_CHECK}; do
    docker ps --format '{{.Names}}' | grep -q \"\$s\" && echo \"  [OK] \$s\" || echo \"  [WAIT] \$s\"
  done
  echo ''
  echo 'Pruning dangling images...'
  docker image prune -f >/dev/null 2>&1 || true
"

setup_local_worker
close_ssh

echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}  AgentForge built and deployed on ${SSH_HOST}!${NC}"
echo -e "${GREEN}  $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo -e "Web:    ${YELLOW}https://${PUBLIC_DOMAIN}${NC}   (agent WS + REST)"
deploy_has_service agentforge-api     && echo -e "API:    ${YELLOW}https://${API_DOMAIN}${NC}   (RAG index/search)"
deploy_has_service qdrant             && echo -e "Qdrant: ${YELLOW}https://${QDRANT_DOMAIN}/dashboard${NC}"
deploy_has_service agentforge-saq-web && echo -e "SAQ:    ${YELLOW}https://${SAQ_DOMAIN}/${NC}"
echo -e "Direct: ${YELLOW}http://${REMOTE_HOST}:8200${NC} (web/agent)"
echo -e "Logs:   ${YELLOW}ssh ${SSH_HOST} 'cd ${REMOTE_DIR} && docker compose logs -f'${NC}"
echo -e "Down:   ${YELLOW}scripts/teardown-remote.sh${NC} (preserves data)"
echo ""
echo -e "${GREEN}Done!${NC}"
