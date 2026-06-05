#!/bin/bash

# Bring up an AgentForge stack locally with Docker Compose.
#
# By default ("full" preset) every service runs in a container: Qdrant, Redis,
# the API + web apps, the scraper sidecar, the two SAQ workers, and the SAQ
# dashboard. The "light" preset runs only the web app + one SAQ worker (+ Redis,
# + Qdrant by default), with in_process dispatch — handy on a MacBook. The preset
# and the host-vs-container dependency toggles come from deploy.env (optional) or
# the --preset / --profiles flags. Persistent data lives in the
# agentforge-qdrant-data / agentforge-redis-data volumes and ./data.
#
# Usage: deploy-local.sh [--preset light|full] [--profiles a,b,c]
#                        [--no-build] [--no-cache] [--foreground]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# Local config lives in deploy.local.env (optional), NOT deploy.env — that one is
# remote-tuned (host Redis, split dispatch, REMOTE_HOST) and would leak the wrong
# settings into a single-host local run. Absent is fine — defaults to the full
# stack with container deps. A local box is always single-host, so dispatch is
# pinned to in_process (no native tools worker involved).
if [ -f "${PROJECT_ROOT}/deploy.local.env" ]; then
    set -a; . "${PROJECT_ROOT}/deploy.local.env"; set +a
fi
. "${SCRIPT_DIR}/lib/resolve-deploy.sh"
DEFAULT_DISPATCH="in_process"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

DETACH="-d"
BUILD="--build"
NO_CACHE_FLAG=""
PRESET_OVERRIDE=""
PROFILES_OVERRIDE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-build)   BUILD="" ;;
        --no-cache)   NO_CACHE_FLAG="--no-cache" ;;
        --foreground) DETACH="" ;;
        --preset)     PRESET_OVERRIDE="$2"; shift ;;
        --preset=*)   PRESET_OVERRIDE="${1#*=}" ;;
        --profiles)   PROFILES_OVERRIDE="$2"; shift ;;
        --profiles=*) PROFILES_OVERRIDE="${1#*=}" ;;
        -h|--help)
            cat <<'HLP'
Usage: deploy-local.sh [FLAGS]

Brings up an AgentForge stack locally via Docker Compose.

  --preset light|full   light = web + one worker (+ redis/qdrant), in_process
                        dispatch; full = the whole stack (default)
  --profiles "a,b,c"    raw COMPOSE_PROFILES override (power users); known:
                        redis qdrant api sidecar split dashboard full
  --no-build            Recreate containers without rebuilding images
  --no-cache            Force a clean image build (no Docker layer cache)
  --foreground          Run attached (stream logs) instead of detached

Knobs also read from deploy.local.env (optional, local-only): AGENTFORGE_PRESET,
AGENTFORGE_PROFILES, AGENTFORGE_REDIS/QDRANT (container|host), HOST_REDIS_URL,
HOST_QDRANT_HOST/PORT. Dispatch is always in_process locally (single host).

Tear down with: scripts/teardown-local.sh  (data preserved by default)
HLP
            exit 0 ;;
        *) echo -e "${RED}Unknown flag: $1${NC} (try --help)"; exit 2 ;;
    esac
    shift
done

# The apps read config.yaml + framework-config.yaml (both gitignored). Seed them
# from the committed examples on first run — the local Ollama path needs no
# credentials; edit the files to add cloud backends.
for f in config framework-config; do
    if [ ! -f "${PROJECT_ROOT}/${f}.yaml" ]; then
        cp "${PROJECT_ROOT}/${f}.example.yaml" "${PROJECT_ROOT}/${f}.yaml"
        echo -e "${YELLOW}[init] created ${f}.yaml from ${f}.example.yaml — edit it for cloud backends${NC}"
    fi
done

# user_context.md is bind-mounted :ro into the app containers. Seed it from the
# example so the mount is a real file, not a Docker-created directory.
if [ ! -f "${PROJECT_ROOT}/user_context.md" ]; then
    cp "${PROJECT_ROOT}/user_context.example.md" "${PROJECT_ROOT}/user_context.md"
    echo -e "${YELLOW}[init] created user_context.md from user_context.example.md — edit it to describe yourself${NC}"
fi

# Resolve the preset/profiles into COMPOSE_PROFILES + dispatch + host-dep env.
resolve_deploy
export COMPOSE_PROFILES="${RESOLVED_COMPOSE_PROFILES}"
export AGENTFORGE_DISPATCH_MODE="${RESOLVED_DISPATCH_MODE}"
[ -n "${RESOLVED_REDIS_URL}" ]   && export REDIS_URL="${RESOLVED_REDIS_URL}"
[ -n "${RESOLVED_QDRANT_HOST}" ] && export QDRANT_HOST="${RESOLVED_QDRANT_HOST}"
[ -n "${RESOLVED_QDRANT_PORT}" ] && export QDRANT_PORT="${RESOLVED_QDRANT_PORT}"

echo -e "${BLUE}=== AgentForge — local stack ===${NC}"
echo -e "  preset   : ${YELLOW}${PRESET_OVERRIDE:-${AGENTFORGE_PRESET:-full}}${NC}"
echo -e "  profiles : ${YELLOW}${COMPOSE_PROFILES:-<none — light core only>}${NC}"
echo -e "  dispatch : ${YELLOW}${AGENTFORGE_DISPATCH_MODE}${NC}"
echo ""

if [ -n "${NO_CACHE_FLAG}" ]; then
    docker compose build --no-cache
    BUILD=""
fi
docker compose up ${DETACH} ${BUILD}

# Detached run: wait, then health-check. A foreground run already streamed logs.
if [ -n "${DETACH}" ]; then
    echo -e "\n${GREEN}Waiting for containers...${NC}"
    sleep 6
    docker compose ps
    echo ""
    if deploy_has_service agentforge-api; then
        curl -sf http://localhost:8100/health >/dev/null 2>&1 \
            && echo -e "  ${GREEN}[OK]${NC}   api  http://localhost:8100" \
            || echo -e "  ${YELLOW}[WAIT]${NC} api still starting..."
    fi
    curl -sf http://localhost:8200/ >/dev/null 2>&1 \
        && echo -e "  ${GREEN}[OK]${NC}   web  http://localhost:8200" \
        || echo -e "  ${YELLOW}[WAIT]${NC} web still starting..."
    echo ""
    echo -e "Web/agent : ${YELLOW}http://localhost:8200${NC}"
    deploy_has_service agentforge-api     && echo -e "API       : ${YELLOW}http://localhost:8100${NC}"
    deploy_has_service agentforge-saq-web && echo -e "SAQ board : ${YELLOW}http://localhost:8086${NC}"
    deploy_has_service qdrant             && echo -e "Qdrant    : ${YELLOW}http://localhost:6333/dashboard${NC}"
    echo -e "Logs      : ${YELLOW}docker compose logs -f${NC}"
    echo -e "Down      : ${YELLOW}scripts/teardown-local.sh${NC}"
fi
