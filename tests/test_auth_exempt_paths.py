"""API-key gate: only liveness, worker callbacks, and OAuth redirects are open.

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
    ],
)
def test_gated_paths(path: str):
    assert not is_exempt(path)
