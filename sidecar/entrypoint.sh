#!/bin/bash
# Start Xvfb virtual display for headed Firefox, then exec the CMD.
set -e

# Start Xvfb on display :99 (matches DISPLAY env var)
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
XVFB_PID=$!

# Wait for Xvfb to be ready
sleep 1

echo "[sidecar] Xvfb started on :99 (PID $XVFB_PID)"
echo "[sidecar] Browser: ${SIDECAR_BROWSER:-firefox}, Headless: ${SIDECAR_HEADLESS:-false}"
echo "[sidecar] Starting uvicorn..."

# Execute the CMD (uvicorn)
exec "$@"
