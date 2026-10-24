"""Simkl AUTH V2 (authorization code + PKCE + client_secret)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agentforge import __version__
from agentforge.config import get_config
from agentforge.film.store import db_path

logger = logging.getLogger(__name__)

_UA = f"agentforge/{__version__}"
_AUTHORIZE = "https://simkl.com/oauth2/authorize"
_TOKEN = "https://api.simkl.com/oauth2/token"
_ISSUER = "https://simkl.com"
_SCOPE = "media:read media:write"
# A pending login is one browser round-trip. Anything older is a leftover.
_PENDING_TTL = 600


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON readable only by the owner (tokens / PKCE verifier)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT with 0o600 covers a new file; chmod covers one that already
    # existed with looser bits (umask does not apply to an existing file).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.chmod(path, 0o600)


def client_id() -> str:
    env = os.environ.get("SIMKL_CLIENT_ID", "").strip()
    if env:
        return env
    return str(get_config().get("tools.film.simkl_client_id", "") or "").strip()


def client_secret() -> str:
    env = os.environ.get("SIMKL_CLIENT_SECRET", "").strip()
    if env:
        return env
    return str(get_config().get("tools.film.simkl_client_secret", "") or "").strip()


def redirect_uri() -> str:
    env = os.environ.get("SIMKL_REDIRECT_URI", "").strip()
    if env:
        return env
    cfg = str(get_config().get("tools.film.simkl_redirect_uri", "") or "").strip()
    if cfg:
        return cfg
    public = os.environ.get("AGENTFORGE_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        return f"{public}/api/film/simkl/callback"
    return "http://localhost:8200/api/film/simkl/callback"


def _token_path() -> Path:
    return db_path().with_name("simkl_oauth.json")


def _pending_path() -> Path:
    return db_path().with_name("simkl_oauth_pending.json")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start_login() -> dict[str, str]:
    cid = client_id()
    if not cid:
        raise RuntimeError("SIMKL_CLIENT_ID / tools.film.simkl_client_id is not set.")
    if not client_secret():
        raise RuntimeError("SIMKL_CLIENT_SECRET / tools.film.simkl_client_secret is not set.")
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(16))
    redir = redirect_uri()
    pending = {"verifier": verifier, "state": state, "redirect_uri": redir, "created": int(time.time())}
    _write_private(_pending_path(), pending)
    qs = urlencode(
        {
            "client_id": cid,
            "redirect_uri": redir,
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return {"authorize_url": f"{_AUTHORIZE}?{qs}", "redirect_uri": redir}


def _post_token(body: dict[str, str]) -> dict[str, Any]:
    data = urlencode(body).encode()
    req = Request(
        _TOKEN,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _UA},
    )
    with urlopen(req, timeout=30) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode())
    if not payload.get("access_token"):
        raise RuntimeError(f"Simkl token response missing access_token: {payload}")
    payload["obtained_at"] = int(time.time())
    _write_private(_token_path(), payload)
    pending = _pending_path()
    if pending.is_file():
        pending.unlink()
    return payload


def complete_login(*, code: str, state: str, iss: str) -> dict[str, Any]:
    if iss != _ISSUER:
        raise RuntimeError("Authorization response did not come from Simkl.")
    pending_path = _pending_path()
    if not pending_path.is_file():
        raise RuntimeError("No pending Simkl login. Start from GET /api/film/simkl/connect.")
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if int(time.time()) - int(pending.get("created") or 0) > _PENDING_TTL:
        pending_path.unlink(missing_ok=True)
        raise RuntimeError("Simkl login request expired. Start again from GET /api/film/simkl/connect.")
    if pending.get("state") != state:
        raise RuntimeError("Simkl OAuth state mismatch.")
    body = {
        "grant_type": "authorization_code",
        "client_id": client_id(),
        "client_secret": client_secret(),
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["verifier"],
    }
    return _post_token(body)


def _refresh(tokens: dict[str, Any]) -> dict[str, Any]:
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise RuntimeError("Simkl token expired and no refresh_token is stored. Re-run connect.")
    return _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": client_id(),
            "client_secret": client_secret(),
            "refresh_token": str(refresh),
        }
    )


def get_valid_token() -> str:
    env = os.environ.get("SIMKL_ACCESS_TOKEN", "").strip()
    if env:
        return env
    cfg = str(get_config().get("tools.film.simkl_access_token", "") or "").strip()
    if cfg:
        return cfg
    path = _token_path()
    if not path.is_file():
        raise RuntimeError(
            "Simkl is not connected. Open /api/film/simkl/connect (or @film connect Simkl) "
            "and approve the app, then retry import."
        )
    tokens = json.loads(path.read_text(encoding="utf-8"))
    obtained = int(tokens.get("obtained_at") or 0)
    expires = int(tokens.get("expires_in") or 604800)
    if obtained and time.time() > obtained + expires - 60:
        tokens = _refresh(tokens)
    return str(tokens["access_token"])
