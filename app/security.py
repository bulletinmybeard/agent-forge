"""Optional auth for the HTTP + WebSocket surface.

Off when neither script keys nor the WebUI admin password are set. The
middleware is then a pass-through, so local / LAN deployments are unaffected.

The WebUI login is ``AGENTFORGE_ADMIN_USERNAME`` / ``AGENTFORGE_ADMIN_PASSWORD``
(deploy.env). Script, app, and extension keys are ``security.api_keys`` in
config.yaml. They authenticate as the admin user and are not stored.
``AGENTFORGE_API_KEYS`` replaces that yaml list only when it contains a key.
A blank value does not.

Three credentials, checked in this order. Each one resolves to a user. None of
them skips the wall:

- a per-user API key (hash row in ``api_keys``)
- the config.yaml script key (the admin user)
- the WebUI session cookie ``af_session``

Clients present a key as:
- ``Authorization: Bearer <key>`` or ``X-API-Key: <key>`` (any HTTP client).
- For the WebSocket from a browser (which can't set headers): a
  ``Sec-WebSocket-Protocol`` subprotocol equal to the key, ``?api_key=<key>``,
  or the session cookie (browsers send it on the same host).

``/health`` and the internal worker callbacks (``/internal/*``) are exempt — the
latter ride the internal Docker network and are not a person. ``POST /api/auth/login``
is the only other anonymous route, and only that exact path.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextvars import ContextVar, Token

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from app.config import settings

# The RAG image (Dockerfile.api) ships app/ and not web/. The chat image has both.
# Keep one ContextVar when web is installed. The fallback is only so the RAG
# process can import this module and serve /health and /knowledge/*.
try:
    from web.server.identity import (
        ADMIN_USER_ID,
        COOKIE_NAME,
        current_user_id,
        reset_current_user,
        set_current_user,
    )
except ModuleNotFoundError:
    ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"
    COOKIE_NAME = "af_session"
    _rag_user_id: ContextVar[str | None] = ContextVar("af_current_user_id", default=None)

    def set_current_user(user_id: str | None) -> Token[str | None]:
        return _rag_user_id.set(user_id)

    def reset_current_user(token: Token[str | None]) -> None:
        _rag_user_id.reset(token)

    def current_user_id() -> str | None:
        return _rag_user_id.get()


logger = logging.getLogger(__name__)

# ChatDatabase, set from the web lifespan once the SQLite file is open.
_auth_db = None

_LOGIN_PATH = "/api/auth/login"

# Never gated: liveness + worker→web callbacks.
#
# The two OAuth redirect targets are also exempt: the provider sends the user's
# browser there, so the request carries no API key and the gate would 401 the
# flow on any deployment that sets security.api_keys. They are not open doors —
# both require a `state` that matches a pending login this host started, and
# Simkl's also checks `iss`. Exact paths only (no prefix widening).
_EXEMPT_PREFIXES = (
    "/health",
    # The web app's own liveness route. docs/SECURITY.md has always listed it as
    # exempt and deploy-remote.sh probes it with no key — "/health" does not
    # cover it, so an authenticated deploy used to report the web tier as down.
    "/api/health",
    "/internal",
    "/api/connectors/auth/callback",
    "/api/film/simkl/callback",
)


def api_keys() -> list[str]:
    """Script keys from ``security.api_keys`` in config.yaml.

    ``AGENTFORGE_API_KEYS`` replaces that list only when it contains a key.
    A blank value does not. Compose sets the variable to an empty string when
    deploy.env leaves it unset, and that must not wipe the yaml list.
    """
    env = os.environ.get("AGENTFORGE_API_KEYS")
    if env is not None and env.strip():
        return [k.strip() for k in env.split(",") if k.strip()]
    return list(settings.security.api_keys or [])


def _admin_password_set() -> bool:
    return bool(os.environ.get("AGENTFORGE_ADMIN_PASSWORD", "").strip())


def auth_enabled() -> bool:
    """WebUI password and config.yaml script keys are separate switches.

    The wall is up when the admin password is set. Script keys authenticate
    as the admin user whether or not that password is set. Either one turns
    the gate on. Neither set stays the local pass-through.
    """
    return bool(api_keys()) or _admin_password_set()


# Treated as a fatal misconfiguration: the host Docker socket reachable from an
# unauthenticated container surface = host-daemon control for any visitor.
_DOCKER_SOCKET = "/var/run/docker.sock"


def enforce_auth_policy(surface: str) -> None:
    """Fail closed when running an exposed surface without authentication.

    Local / LAN use stays unauthenticated by design (pass-through + warning).
    Two cases are fatal:
      - running inside a container with the host Docker socket mounted but no
        API keys (an unauthenticated client would get host-daemon control);
      - ``AGENTFORGE_REQUIRE_AUTH`` set truthy with no keys configured.

    ``AGENTFORGE_ALLOW_INSECURE`` is an explicit operator opt-out: it bypasses
    the fatal checks (boots open with a loud warning). Use only on a trusted
    network during a transition — never on an untrusted/public host.

    Call once at startup. Raises ``RuntimeError`` to abort the boot.
    """
    if auth_enabled():
        return

    allow_insecure = os.environ.get("AGENTFORGE_ALLOW_INSECURE", "").strip().lower() in ("1", "true", "yes")
    require = os.environ.get("AGENTFORGE_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")
    socket_exposed = os.path.exists("/.dockerenv") and os.path.exists(_DOCKER_SOCKET)

    if require or socket_exposed:
        reason = "AGENTFORGE_REQUIRE_AUTH is set" if require else f"the Docker socket ({_DOCKER_SOCKET}) is mounted"
        if not allow_insecure:
            raise RuntimeError(
                f"Refusing to start the {surface} surface without authentication: {reason}, but no API "
                "keys are configured. Set security.api_keys in config.yaml or AGENTFORGE_API_KEYS "
                "(comma-separated) and front the service with TLS — or set AGENTFORGE_ALLOW_INSECURE=1 "
                "to explicitly run open on a trusted network."
            )
        logger.warning(
            "%s surface running WITHOUT authentication despite %s — AGENTFORGE_ALLOW_INSECURE is set. "
            "Explicit opt-out; do NOT use on an untrusted/public network.",
            surface,
            reason,
        )
        return

    logger.warning(
        "%s surface is running WITHOUT authentication (no security.api_keys / AGENTFORGE_API_KEYS). "
        "Acceptable for local/LAN only — do NOT expose this publicly.",
        surface,
    )


def set_auth_database(db) -> None:
    """Register the chat database used to resolve user keys and session cookies."""
    global _auth_db
    _auth_db = db


def _key_valid(candidate: str | None) -> bool:
    if not candidate:
        return False
    # Constant-time compare against each configured key.
    return any(hmac.compare_digest(candidate, k) for k in api_keys())


def resolve_presented_key(candidate: str | None) -> str | None:
    """User id for a presented key: stored user key, then the config admin key."""
    if not candidate:
        return None
    db = _auth_db
    if db is not None:
        user_id = db.user_id_for_api_key(candidate)
        if user_id:
            return user_id
    if _key_valid(candidate):
        return ADMIN_USER_ID
    return None


def resolve_session_cookie(raw: str | None) -> str | None:
    if not raw or _auth_db is None:
        return None
    return _auth_db.user_id_for_session_token(raw)


def _anonymous_ok(request: Request) -> bool:
    if request.method == "OPTIONS":
        return True
    return request.method == "POST" and request.url.path == _LOGIN_PATH


def _resolve_http_user(request: Request) -> str | None:
    key = _http_key(request)
    if key:
        return resolve_presented_key(key)
    return resolve_session_cookie(request.cookies.get(COOKIE_NAME))


def is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _EXEMPT_PREFIXES)


def _http_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("x-api-key") or None


def install_api_key_auth(app) -> None:
    """Register the HTTP auth middleware. No-op at request time when disabled.

    Only guards HTTP requests; the WebSocket upgrade bypasses HTTP middleware
    (different ASGI scope) and is checked in the endpoint via :func:`negotiate_ws`.
    """

    @app.middleware("http")
    async def _api_key_mw(request: Request, call_next):  # noqa: ANN001
        if not auth_enabled() or is_exempt(request.url.path) or _anonymous_ok(request):
            return await call_next(request)
        user_id = _resolve_http_user(request)
        if not user_id:
            return JSONResponse(
                {"detail": "Invalid or missing API key"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = set_current_user(user_id)
        try:
            return await call_next(request)
        finally:
            reset_current_user(token)


def install_internal_auth(app) -> None:
    """Require a shared secret on ``/internal/*`` when ``AGENTFORGE_INTERNAL_TOKEN`` is set.

    The ``/internal`` callbacks are exempt from the API-key gate (workers have no
    API key) and otherwise rely solely on Traefik's path-exclusion. This adds a
    defence-in-depth check so the worker->web callbacks can't be forged (e.g.
    injecting into other sessions' chats, forging confirmations) if the path is
    ever reachable. No-op when the token is unset (network isolation only).
    """
    token = os.environ.get("AGENTFORGE_INTERNAL_TOKEN", "").strip()
    if not token:
        return

    @app.middleware("http")
    async def _internal_mw(request: Request, call_next):  # noqa: ANN001
        path = request.url.path
        if path == "/internal" or path.startswith("/internal/"):
            if not hmac.compare_digest(request.headers.get("x-internal-token", ""), token):
                return JSONResponse({"detail": "Invalid or missing internal token"}, status_code=401)
        return await call_next(request)


def negotiate_ws(websocket: WebSocket) -> tuple[bool, str | None]:
    """Authorize a WebSocket handshake.

    Returns ``(authorized, subprotocol_to_echo)``. The subprotocol is non-None
    only when the key was supplied via ``Sec-WebSocket-Protocol`` and the caller
    must echo it back in ``accept()``. When auth is disabled this returns
    ``(True, None)`` so behaviour is unchanged.
    """
    if not auth_enabled():
        return True, None

    def _accept(user_id: str | None, subprotocol: str | None) -> tuple[bool, str | None]:
        if not user_id:
            return False, None
        set_current_user(user_id)
        return True, subprotocol

    # 1. Authorization / X-API-Key headers (non-browser clients). A bad key
    #    does not fall through to the cookie.
    auth = websocket.headers.get("authorization", "")
    header_key = auth[7:].strip() if auth[:7].lower() == "bearer " else websocket.headers.get("x-api-key")
    if header_key:
        return _accept(resolve_presented_key(header_key), None)

    # 2. Sec-WebSocket-Protocol — the one header browsers can set. Any offered
    #    subprotocol that matches a key authorizes; echo it back on accept().
    offered = websocket.headers.get("sec-websocket-protocol", "")
    for proto in (p.strip() for p in offered.split(",") if p.strip()):
        user_id = resolve_presented_key(proto)
        if user_id:
            return _accept(user_id, proto)

    # 3. Query param fallback. NOTE: query strings can land in proxy/access
    #    logs, so prefer the header or Sec-WebSocket-Protocol methods above;
    #    this is a last resort for clients that can set neither.
    query_key = websocket.query_params.get("api_key")
    if query_key:
        return _accept(resolve_presented_key(query_key), None)

    # 4. WebUI session cookie. Same host, so the browser sends it on the upgrade.
    return _accept(resolve_session_cookie(websocket.cookies.get(COOKIE_NAME)), None)
