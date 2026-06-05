#!/bin/bash
# Shared deploy resolver — sourced by deploy-local.sh + deploy-remote.sh.
#
# Turns the high-level knobs (AGENTFORGE_PRESET + the AGENTFORGE_REDIS/QDRANT
# dependency-reuse toggles, or a raw AGENTFORGE_PROFILES override) into the
# concrete values the compose CLI needs:
#
#   RESOLVED_COMPOSE_PROFILES   value for COMPOSE_PROFILES
#   RESOLVED_DISPATCH_MODE      in_process | split
#   RESOLVED_REDIS_URL          set only when reusing a host Redis (else empty)
#   RESOLVED_QDRANT_HOST/PORT   set only when reusing a host Qdrant (else empty)
#   RESOLVED_ACTIVE             space-separated services that will actually run
#
# Inputs (env, normally from deploy.env; callers may set the *_OVERRIDE vars
# from CLI flags before calling resolve_deploy):
#   AGENTFORGE_PRESET      light | full          (default full = current stack)
#   AGENTFORGE_PROFILES    explicit profile list (overrides the preset mapping)
#   AGENTFORGE_REDIS       container | host      (default container)
#   AGENTFORGE_QDRANT      container | host | off (default container)
#   HOST_REDIS_URL, HOST_QDRANT_HOST, HOST_QDRANT_PORT
#   PRESET_OVERRIDE, PROFILES_OVERRIDE  (from --preset / --profiles flags)

# Every profile name in docker-compose.yml. Teardown enables all of them so
# `docker compose down` removes profile-gated services (redis/qdrant/api/...),
# not just the always-on core — `down` otherwise ignores inactive profiles and
# --remove-orphans doesn't catch them (they're still defined, just gated).
ALL_COMPOSE_PROFILES="redis,qdrant,api,sidecar,dashboard,split,full"

# True if RESOLVED_COMPOSE_PROFILES contains the given profile name.
_profiles_contains() {
    case ",${RESOLVED_COMPOSE_PROFILES}," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

resolve_deploy() {
    local preset redis_mode qdrant_mode default_dispatch
    preset="${PRESET_OVERRIDE:-${AGENTFORGE_PRESET:-full}}"
    redis_mode="${AGENTFORGE_REDIS:-container}"
    qdrant_mode="${AGENTFORGE_QDRANT:-container}"
    # Topology default the caller sets: in_process for a single-host local box,
    # split for a remote stack with a native local worker on another machine.
    default_dispatch="${DEFAULT_DISPATCH:-split}"

    # --- Dispatch mode ---------------------------------------------------
    # Explicit wins; the light preset is always single-worker; otherwise the
    # caller's topology default. Decided BEFORE profiles because the tools
    # worker (split profile) only exists when dispatch is split.
    if [ -n "${AGENTFORGE_DISPATCH_MODE:-}" ]; then
        RESOLVED_DISPATCH_MODE="${AGENTFORGE_DISPATCH_MODE}"
    elif [ "${preset}" = "light" ]; then
        RESOLVED_DISPATCH_MODE="in_process"
    else
        RESOLVED_DISPATCH_MODE="${default_dispatch}"
    fi

    # --- COMPOSE_PROFILES ------------------------------------------------
    if [ -n "${PROFILES_OVERRIDE:-}" ]; then
        RESOLVED_COMPOSE_PROFILES="${PROFILES_OVERRIDE}"
    elif [ -n "${AGENTFORGE_PROFILES:-}" ]; then
        RESOLVED_COMPOSE_PROFILES="${AGENTFORGE_PROFILES}"
    else
        local list=""
        [ "${preset}" = "full" ] && list="full"
        [ "${qdrant_mode}" = "container" ] && list="${list:+${list},}qdrant"
        [ "${redis_mode}" = "container" ] && list="${list:+${list},}redis"
        # The tools worker is needed only for split dispatch (remote topology).
        [ "${RESOLVED_DISPATCH_MODE}" = "split" ] && list="${list:+${list},}split"
        RESOLVED_COMPOSE_PROFILES="${list}"
    fi

    # --- Consistency warnings (mainly for raw --profiles overrides) ------
    if [ "${RESOLVED_DISPATCH_MODE}" = "split" ] && ! _profiles_contains split; then
        echo "[resolve-deploy] split dispatch but no 'split' profile — role-routed tool jobs won't be drained. Add the split profile or use in_process." >&2
    fi
    if [ "${RESOLVED_DISPATCH_MODE}" = "in_process" ] && _profiles_contains split; then
        echo "[resolve-deploy] 'split' profile active but in_process dispatch — the tools worker will sit idle." >&2
    fi

    # --- Host vs container deps ------------------------------------------
    RESOLVED_REDIS_URL=""
    if [ "${redis_mode}" = "host" ]; then
        RESOLVED_REDIS_URL="${HOST_REDIS_URL:-redis://host.docker.internal:6379}"
    fi
    RESOLVED_QDRANT_HOST=""
    RESOLVED_QDRANT_PORT=""
    if [ "${qdrant_mode}" = "host" ]; then
        RESOLVED_QDRANT_HOST="${HOST_QDRANT_HOST:-host.docker.internal}"
        RESOLVED_QDRANT_PORT="${HOST_QDRANT_PORT:-6333}"
    fi

    # --- Active service list (for health checks / restart targeting) -----
    RESOLVED_ACTIVE="agentforge-web agentforge-worker-saq"
    _profiles_contains qdrant && RESOLVED_ACTIVE="${RESOLVED_ACTIVE} qdrant"
    _profiles_contains redis  && RESOLVED_ACTIVE="${RESOLVED_ACTIVE} redis"
    { _profiles_contains api       || _profiles_contains full; } && RESOLVED_ACTIVE="${RESOLVED_ACTIVE} agentforge-api"
    { _profiles_contains sidecar   || _profiles_contains full; } && RESOLVED_ACTIVE="${RESOLVED_ACTIVE} agentforge-sidecar"
    _profiles_contains split && RESOLVED_ACTIVE="${RESOLVED_ACTIVE} agentforge-worker-saq-tools"
    { _profiles_contains dashboard || _profiles_contains full; } && RESOLVED_ACTIVE="${RESOLVED_ACTIVE} agentforge-saq-web"

    # The trailing && tests above can leave $? non-zero; return success so a bare
    # `resolve_deploy` call doesn't trip the caller's `set -e`.
    return 0
}

# True if $1 is in the resolved active-service list.
deploy_has_service() {
    case " ${RESOLVED_ACTIVE} " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}
