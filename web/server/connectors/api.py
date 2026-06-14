"""Connectors REST API — CRUD, OAuth flow, and connection testing."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request as HttpRequest
from urllib.request import urlopen

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

# -- Module-level singletons (set during init) ------------------------------

_connection_manager = None
_connector_registry = None
_custom_agents: dict | None = None
_redis_client = None


class OAuthStartRequest(BaseModel):
    connector_type: str
    label: str = ""
    products: list[str] = []


class TokenConnectRequest(BaseModel):
    connector_type: str
    url: str
    token: str
    label: str = ""
    read_write: bool = True


class ConnectionUpdate(BaseModel):
    label: str | None = None
    read_write: bool | None = None


def init_connectors_api(
    connection_manager: Any,
    connector_registry: Any,
    custom_agents: dict,
    redis_client: Any = None,
) -> None:
    global _connection_manager, _connector_registry, _custom_agents, _redis_client
    _connection_manager = connection_manager
    _connector_registry = connector_registry
    _custom_agents = custom_agents
    _redis_client = redis_client


def _require_init() -> None:
    if _connection_manager is None:
        raise HTTPException(503, "Connector system not initialised")


def _public_base_url(request: Request) -> str:
    """Canonical app origin for OAuth redirect/postMessage.

    Prefer a configured value so an attacker can't steer the OAuth
    ``redirect_uri`` via spoofed ``Host`` / ``X-Forwarded-*`` headers. Falls
    back to request headers only when nothing is configured (local dev).
    """
    base = os.environ.get("AGENTFORGE_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return base
    domain = os.environ.get("PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    return f"{proto}://{host}"


def _build_redirect_uri(request: Request) -> str:
    """Build the OAuth callback URI from the canonical app origin."""
    return f"{_public_base_url(request)}/api/connectors/auth/callback"


# -- Connector types --------------------------------------------------------


@router.get("/types")
async def list_connector_types():
    _require_init()
    return {"types": _connector_registry.list_types()}


# -- Connection CRUD --------------------------------------------------------


@router.get("")
async def list_connections():
    _require_init()
    return {"connections": _connection_manager.list_connections()}


@router.get("/{connection_id}")
async def get_connection(connection_id: str):
    _require_init()
    conn = _connection_manager.get_connection(connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    return conn


@router.patch("/{connection_id}")
async def update_connection(connection_id: str, body: ConnectionUpdate):
    _require_init()
    if body.read_write is None and body.label is None:
        raise HTTPException(400, "Nothing to update")
    result = None
    if body.read_write is not None:
        result = _connection_manager.set_read_write(connection_id, body.read_write, _custom_agents)
    if body.label is not None:
        result = _connection_manager.update_label(connection_id, body.label, _custom_agents)
    if result is None:
        raise HTTPException(404, "Connection not found")
    return result


@router.delete("/{connection_id}")
async def delete_connection(connection_id: str):
    _require_init()
    deleted = _connection_manager.delete_connection(connection_id, _custom_agents)
    if not deleted:
        raise HTTPException(404, "Connection not found")
    return {"status": "deleted"}


@router.post("/{connection_id}/test")
async def test_connection(connection_id: str):
    _require_init()
    return _connection_manager.test_connection(connection_id)


@router.post("/auth/token")
async def connect_with_token(body: TokenConnectRequest):
    """Create a connection using an API token (for non-OAuth connectors like GitLab)."""
    _require_init()

    plugin = _connector_registry.get(body.connector_type)
    if not plugin:
        raise HTTPException(400, f"Unknown connector type: {body.connector_type}")

    tokens = {
        "url": body.url.rstrip("/"),
        "token": body.token,
        "read_write": body.read_write,
    }

    # Verify the credentials before persisting
    # to prevent creating dead connections.
    try:
        check = plugin.test_connection(json.dumps(tokens))
    except Exception as exc:
        logger.warning("Token verification errored (%s): %s", body.connector_type, exc)
        raise HTTPException(400, f"Could not verify connection: {exc}") from exc
    if not check.get("ok"):
        raise HTTPException(400, check.get("error") or "Token verification failed")

    try:
        conn = _connection_manager.create_connection(
            connector_type=body.connector_type,
            label=body.label,
            tokens=tokens,
            custom_agents=_custom_agents,
        )
        logger.info("Created token connection: %s (%s)", conn["label"], body.connector_type)
    except Exception as exc:
        logger.error("Failed to create token connection: %s", exc)
        raise HTTPException(500, str(exc)) from exc

    return conn


@router.post("/{connection_id}/reconnect")
async def reconnect_connection(connection_id: str):
    """Start a re-auth flow for an expired connection."""
    _require_init()
    conn = _connection_manager.get_connection(connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    return {
        "status": "redirect",
        "connector_type": conn["connector_type"],
        "connection_id": connection_id,
        "message": "Start a new OAuth flow for this connector type to refresh credentials.",
    }


# -- OAuth flow -------------------------------------------------------------


def _scopes_for_plugin(plugin, products: list[str]) -> list[str]:
    """Per-connection OAuth scopes."""
    scopes_for = getattr(plugin, "scopes_for", None)
    return scopes_for(products) if callable(scopes_for) else plugin.oauth_scopes


@router.post("/auth/start")
async def start_oauth(body: OAuthStartRequest, request: Request):
    _require_init()

    plugin = _connector_registry.get(body.connector_type)
    if not plugin:
        raise HTTPException(400, f"Unknown connector type: {body.connector_type}")

    try:
        client_config = plugin.get_oauth_client_config()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc

    # Use redirect_uri from client_secret.json if available, otherwise derive from request
    redirect_uri = client_config.get("redirect_uri") or _build_redirect_uri(request)
    logger.info(
        "OAuth start: type=%s, redirect_uri=%s, from_config=%s",
        body.connector_type,
        redirect_uri,
        "redirect_uri" in client_config,
    )

    state = secrets.token_urlsafe(32)
    state_data = {
        "connector_type": body.connector_type,
        "label": body.label,
        "redirect_uri": redirect_uri,
        "products": body.products,
    }

    if _redis_client:
        _redis_client.setex(f"oauth_state:{state}", 600, json.dumps(state_data))
    else:
        # Fallback: store in-memory (single-process only)
        if not hasattr(start_oauth, "_state_store"):
            start_oauth._state_store = {}
        start_oauth._state_store[state] = state_data

    params = {
        "client_id": client_config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_scopes_for_plugin(plugin, body.products)),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = f"{plugin.oauth_auth_uri}?{urlencode(params)}"

    return {"auth_url": auth_url, "state": state}


@router.get("/auth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    if error:
        return HTMLResponse(_callback_html(success=False, error=error))

    if not code or not state:
        return HTMLResponse(_callback_html(success=False, error="Missing code or state"))

    # Retrieve state data
    state_data = None
    if _redis_client:
        raw = _redis_client.get(f"oauth_state:{state}")
        if raw:
            state_data = json.loads(raw)
            _redis_client.delete(f"oauth_state:{state}")
    elif hasattr(start_oauth, "_state_store"):
        state_data = start_oauth._state_store.pop(state, None)

    if not state_data:
        return HTMLResponse(_callback_html(success=False, error="Invalid or expired OAuth state"))

    connector_type = state_data["connector_type"]
    label = state_data.get("label", "")
    products = state_data.get("products", [])

    plugin = _connector_registry.get(connector_type)
    if not plugin:
        return HTMLResponse(_callback_html(success=False, error=f"Unknown connector: {connector_type}"))

    client_config = plugin.get_oauth_client_config()
    # Must use the exact same redirect_uri that was sent in the auth request
    redirect_uri = state_data.get("redirect_uri") or client_config.get("redirect_uri") or _build_redirect_uri(request)

    # Exchange code for tokens
    try:
        token_data = _exchange_code(
            code=code,
            client_id=client_config["client_id"],
            client_secret=client_config["client_secret"],
            redirect_uri=redirect_uri,
            token_uri=plugin.oauth_token_uri,
        )
    except Exception as exc:
        logger.error("OAuth token exchange failed: %s", exc)
        return HTMLResponse(_callback_html(success=False, error=f"Token exchange failed: {exc}"))

    # Store the token with client config for later refresh
    expires_in = int(token_data.get("expires_in", 3600))
    expiry = datetime.now() + timedelta(seconds=expires_in)

    tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "token_uri": plugin.oauth_token_uri,
        "client_id": client_config["client_id"],
        "client_secret": client_config["client_secret"],
        "expiry": expiry.isoformat(),
        "products": products,
    }

    # For BigQuery: fetch the GCP project_id at connection time (needed for billing).
    # The BigQuery projects API is intermittently empty — retry up to 3 times.
    if connector_type == "bigquery" or "bigquery" in products:
        for _attempt in range(3):
            try:
                bq_req = HttpRequest(
                    "https://bigquery.googleapis.com/bigquery/v2/projects?maxResults=1",
                    headers={"Authorization": f"Bearer {token_data['access_token']}"},
                )
                with urlopen(bq_req, timeout=15) as bq_resp:
                    bq_data = json.loads(bq_resp.read().decode())
                bq_projects = bq_data.get("projects") or []
                if bq_projects:
                    tokens["project_id"] = bq_projects[0].get("id", "")
                    logger.info("BigQuery project_id detected: %s", tokens["project_id"])
                    break
                logger.debug("BigQuery projects list empty (attempt %d/3)", _attempt + 1)
            except Exception as exc:
                logger.warning("BigQuery project_id detection attempt %d failed: %s", _attempt + 1, exc)
            time.sleep(1)

    try:
        conn = _connection_manager.create_connection(
            connector_type=connector_type,
            label=label,
            tokens=tokens,
            custom_agents=_custom_agents,
        )
        logger.info(
            "Created connector: %s (%s) — %s",
            conn["label"],
            connector_type,
            conn["id"],
        )
    except Exception as exc:
        logger.error("Failed to create connection: %s", exc)
        return HTMLResponse(_callback_html(success=False, error=f"Failed to save connection: {exc}"))

    return HTMLResponse(_callback_html(success=True, connection_id=conn["id"]))


def _exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    token_uri: str,
) -> dict:
    data = urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()

    req = HttpRequest(token_uri, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _callback_html(success: bool, connection_id: str = "", error: str = "") -> str:
    if success:
        return """<!DOCTYPE html>
<html><head><title>Connected</title></head>
<body style="background:#111;color:#e5e7eb;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center">
<h2 style="color:#818cf8">Connected</h2>
<p>You can close this window.</p>
</div>
<script>
if (window.opener) {
  window.opener.postMessage({type:'connector-auth-complete'}, '*');
}
setTimeout(() => window.close(), 1500);
</script>
</body></html>"""
    else:
        return f"""<!DOCTYPE html>
<html><head><title>Connection Failed</title></head>
<body style="background:#111;color:#e5e7eb;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center">
<h2 style="color:#f87171">Connection Failed</h2>
<p>{error}</p>
<p style="color:#9ca3af;font-size:0.875rem">Close this window and try again.</p>
</div>
</body></html>"""
