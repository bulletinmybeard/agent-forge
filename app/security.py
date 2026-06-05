"""Optional API-key auth for the HTTP + WebSocket surface.

Off by default: when no keys are configured the middleware is a pass-through, so local / LAN deployments are unaffected. Configure keys to require them. Useful when exposing AgentForge (which can drive an agent that runs shell/SSH/Docker) on a public host.

Keys come from ``config.yaml`` ``security.api_keys`` or the
``AGENTFORGE_API_KEYS`` env var (comma-separated; wins over the YAML).

Clients present the key as:
- ``Authorization: Bearer <key>`` or ``X-API-Key: <key>`` (any HTTP client).
- For the WebSocket from a browser (which can't set headers): a
  ``Sec-WebSocket-Protocol`` subprotocol equal to the key, or ``?api_key=<key>``.

``/health`` and the internal worker callbacks (``/internal/*``) are exempt — the
latter ride the internal Docker network; keep them off the public router too
(the remote overlay excludes ``/internal`` from Traefik).
"""

from __future__ import annotations

import hmac
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from app.config import settings

logger = logging.getLogger(__name__)

# Never gated: liveness + worker→web callbacks.
_EXEMPT_PREFIXES = ("/health", "/internal")


def api_keys() -> list[str]:
    """Configured keys. ``AGENTFORGE_API_KEYS`` env (comma-separated) wins."""
    env = os.environ.get("AGENTFORGE_API_KEYS")
    if env is not None:
        return [k.strip() for k in env.split(",") if k.strip()]
    return list(settings.security.api_keys or [])


def auth_enabled() -> bool:
    return bool(api_keys())


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


def _key_valid(candidate: str | None) -> bool:
    if not candidate:
        return False
    # Constant-time compare against each configured key.
    return any(hmac.compare_digest(candidate, k) for k in api_keys())


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
        if auth_enabled() and not is_exempt(request.url.path):
            if not _key_valid(_http_key(request)):
                return JSONResponse(
                    {"detail": "Invalid or missing API key"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)


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

    # 1. Authorization / X-API-Key headers (non-browser clients).
    auth = websocket.headers.get("authorization", "")
    header_key = auth[7:].strip() if auth[:7].lower() == "bearer " else websocket.headers.get("x-api-key")
    if header_key:
        return _key_valid(header_key), None

    # 2. Sec-WebSocket-Protocol — the one header browsers can set. Any offered
    #    subprotocol that matches a key authorizes; echo it back on accept().
    offered = websocket.headers.get("sec-websocket-protocol", "")
    for proto in (p.strip() for p in offered.split(",") if p.strip()):
        if _key_valid(proto):
            return True, proto

    # 3. Query param fallback. NOTE: query strings can land in proxy/access
    #    logs, so prefer the header or Sec-WebSocket-Protocol methods above;
    #    this is a last resort for clients that can set neither.
    if _key_valid(websocket.query_params.get("api_key")):
        return True, None

    return False, None
