"""API-key gate: liveness, worker callbacks, OAuth redirects, and Swagger are open.

The exempt list is the one place where widening a prefix silently un-gates a
whole tree (``/api`` would open every route), so pin both halves: what must
stay open, and what must not be reachable without a key.
"""

from __future__ import annotations

import pytest

from app.security import is_exempt


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/api/health",
        "/internal",
        "/internal/jobs/abc/cancelled",
        "/internal/sessions/abc/event",
        # An external provider redirects the browser here with no key to attach.
        "/api/connectors/auth/callback",
        "/api/film/simkl/callback",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    ],
)
def test_open_paths(path: str):
    assert is_exempt(path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api",
        "/api/sessions",
        "/api/tools/run",
        "/api/permissions/commands",
        "/api/film/simkl/connect",  # starts the flow — key required
        "/api/healthz",  # near-miss: exemption is exact, not a prefix
        "/healthcheck",
        "/api/connectors/auth/callbackx",
        "/internalx",
        "/docsx",
        "/redocx",
        "/openapi",
    ],
)
def test_gated_paths(path: str):
    assert not is_exempt(path)


def test_openapi_advertises_both_keys():
    from fastapi import FastAPI

    from app.security import _HTTP_API_KEY_HEADER, install_api_key_auth

    app = FastAPI()

    @app.get("/api/sessions")
    def _sessions():
        return []

    install_api_key_auth(app)
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    assert schemes["ApiKeyHeader"]["name"] == _HTTP_API_KEY_HEADER
    assert {"HTTPBearer": []} in schema["security"]
    assert {"ApiKeyHeader": []} in schema["security"]
    assert "/api/sessions" in schema["paths"]
