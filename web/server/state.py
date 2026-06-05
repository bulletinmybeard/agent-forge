"""Shared in-process state for agentforge-web.

A thin module that both ws_endpoint.py and api.py can import without
creating circular dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

# Maps session_id -> currently connected WebSocket.
# Populated by ws_endpoint when a client connects; cleared on disconnect.
# Read by the /internal/sessions/{session_id}/event endpoint to broadcast
# worker messages to any live browser connection in real-time.
active_ws: dict[str, "WebSocket"] = {}

# Maps "{session_id}:{request_id}" -> {"confirmed": bool, "auto_accept": bool}.
# Written by ws_endpoint when the browser sends a confirm.response WS message.
# Read (and consumed once) by the worker via:
#   GET /internal/sessions/{session_id}/confirm/{request_id}
# This enables the HttpConfirmationBroker in jobs_common.py to poll for the user's
# answer while the worker thread is waiting for confirmation.
confirm_responses: dict[str, dict] = {}

# Maps "{session_id}:{request_id}" -> {"value": str} or {"cancelled": True}.
# Written by ws_endpoint when the browser sends a secret.response WS message
# (the masked sudo-password prompt). Read (and consumed once) by the native
# local worker via GET /internal/sessions/{session_id}/secret/{request_id} so
# WorkerSecretProvider can fetch a password for a sudo command running there.
# Memory-only + popped on first read; never logged or persisted.
secret_responses: dict[str, dict] = {}
