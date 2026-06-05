#!/bin/bash

# Tear down the local AgentForge stack. Data-preserving by default: the Qdrant
# and Redis volumes plus ./data survive, so a later deploy-local.sh resumes where
# you left off. Pass --volumes to wipe the vector index + Redis data too.
#
# Usage: teardown-local.sh [--volumes] [--rmi] [-y]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

. "${SCRIPT_DIR}/lib/resolve-deploy.sh"   # for ALL_COMPOSE_PROFILES

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

REMOVE_VOLUMES=false
REMOVE_IMAGES=false
ASSUME_YES=false
for arg in "$@"; do
    case "$arg" in
        --volumes|-v) REMOVE_VOLUMES=true ;;
        --rmi)        REMOVE_IMAGES=true ;;
        -y|--yes)     ASSUME_YES=true ;;
        -h|--help)
            cat <<'HLP'
Usage: teardown-local.sh [--volumes] [--rmi] [-y]

Stops and removes the local AgentForge containers.

  --volumes, -v  ALSO delete the Qdrant + Redis volumes (DESTROYS indexed data)
  --rmi          Also remove the built agentforge-{api,web,sidecar}:latest images
  -y, --yes      Skip the confirmation prompt

Without --volumes the vector index, Redis data, and ./data are preserved.
HLP
            exit 0 ;;
        *) echo -e "${RED}Unknown flag: ${arg}${NC} (try --help)"; exit 2 ;;
    esac
done

if [ "${REMOVE_VOLUMES}" = true ]; then
    echo -e "${YELLOW}WARNING: --volumes will DELETE the Qdrant vector index + Redis data.${NC}"
fi
if [ "${ASSUME_YES}" != true ]; then
    printf "Tear down the local stack? [y/N] "
    read -r reply
    case "$reply" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

DOWN_ARGS=""
[ "${REMOVE_VOLUMES}" = true ] && DOWN_ARGS="--volumes"
[ "${REMOVE_IMAGES}" = true ] && DOWN_ARGS="${DOWN_ARGS} --rmi local"

echo -e "\n${GREEN}Stopping the stack...${NC}"
# Enable every profile so `down` removes profile-gated services (qdrant/redis/...),
# not just the always-on core. --remove-orphans clears anything since deleted.
COMPOSE_PROFILES="${ALL_COMPOSE_PROFILES}" docker compose down ${DOWN_ARGS} --remove-orphans

echo -e "\n${GREEN}Done.${NC} Bring it back with: ${YELLOW}scripts/deploy-local.sh${NC}"
