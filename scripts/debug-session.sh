#!/bin/sh
# Fetch GET /api/debug/sessions/{uuid} against the deployed web app.
# Usage: scripts/debug-session.sh <session-uuid>
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

if [ "${1:-}" = "" ]; then
    echo "usage: $0 <session-uuid>" >&2
    exit 2
fi
SID=$1

if [ -f "$ROOT/deploy.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/deploy.env"
    set +a
fi

if [ -n "${PUBLIC_DOMAIN:-}" ]; then
    BASE="https://${PUBLIC_DOMAIN}"
elif [ -n "${REMOTE_HOST:-}" ]; then
    BASE="http://${REMOTE_HOST}:8200"
else
    echo "set PUBLIC_DOMAIN or REMOTE_HOST in deploy.env" >&2
    exit 2
fi

KEY=${AGENTFORGE_API_KEYS:-}
KEY=${KEY%%,*}

AUTH_ARGS=""
if [ -n "$KEY" ]; then
    AUTH_ARGS="-H Authorization: Bearer ${KEY}"
fi

# curl: fail on HTTP errors; jq if present else python json pretty-print
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

HTTP_CODE=$(curl -sS -o "$TMP" -w "%{http_code}" $AUTH_ARGS "${BASE}/api/debug/sessions/${SID}") || {
    echo "curl failed talking to ${BASE}" >&2
    exit 1
}

if [ "$HTTP_CODE" != "200" ]; then
    echo "HTTP ${HTTP_CODE} from ${BASE}/api/debug/sessions/${SID}" >&2
    cat "$TMP" >&2
    exit 1
fi

if command -v jq >/dev/null 2>&1; then
    jq . "$TMP"
else
    python3 -m json.tool "$TMP"
fi
