"""Who the current request is.

The admin user has a fixed id. Config API keys authenticate as that user.
A WebUI cookie and a per-user API key authenticate as the user that owns them.
When no caller has been resolved, :func:`current_user_id` is ``None`` and the
database does not filter rows. That is the local/LAN pass-through.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from contextvars import ContextVar, Token
from typing import Protocol

logger = logging.getLogger(__name__)

ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ADMIN_USERNAME = "admin"
# Kept for callers that want the default name. The live login name is admin_username().
ADMIN_USERNAME = DEFAULT_ADMIN_USERNAME
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
COOKIE_NAME = "af_session"
SESSION_DAYS = 30
_PBKDF2_ROUNDS = 200_000

_current_user_id: ContextVar[str | None] = ContextVar("af_current_user_id", default=None)


def current_user_id() -> str | None:
    return _current_user_id.get()


def set_current_user(user_id: str | None) -> Token[str | None]:
    return _current_user_id.set(user_id)


def reset_current_user(token: Token[str | None]) -> None:
    _current_user_id.reset(token)


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt_hex, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        if rounds < 1 or rounds > 1_000_000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), rounds)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def new_api_key() -> tuple[str, str, str]:
    """Return ``(secret, prefix, hash)``. The secret is shown once."""
    raw = "afk_" + secrets.token_urlsafe(32)
    return raw, raw[:12], hash_secret(raw)


def new_session_token() -> tuple[str, str]:
    """Return ``(cookie value, hash)``."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_secret(raw)


def normalize_username(value: str | None) -> str | None:
    """Lowercase a username. ``None`` when it is not 2-32 of ``[a-z0-9_-]`` after a letter."""
    name = (value or "").strip().lower()
    if not _USERNAME_RE.fullmatch(name):
        return None
    return name


def admin_username() -> str:
    """Login name for the fixed admin row.

    Empty ``AGENTFORGE_ADMIN_USERNAME`` keeps ``admin``. A non-empty value that
    fails the username pattern raises ``ValueError`` and is not logged.
    """
    raw = os.environ.get("AGENTFORGE_ADMIN_USERNAME", "")
    if not raw.strip():
        return DEFAULT_ADMIN_USERNAME
    name = normalize_username(raw)
    if name is None:
        raise ValueError(
            "AGENTFORGE_ADMIN_USERNAME must be 2-32 characters: a letter, then letters, digits, _ or -"
        )
    return name


class _PasswordStore(Protocol):
    def set_user_password(self, user_id: str, password_hash: str) -> None: ...


class _UsernameStore(Protocol):
    def set_admin_username(self, username: str) -> None: ...


def apply_admin_username(db: _UsernameStore) -> None:
    """Rename the admin row when ``AGENTFORGE_ADMIN_USERNAME`` is set.

    Empty keeps whatever name the row already has (``admin`` on a fresh database).
    """
    raw = os.environ.get("AGENTFORGE_ADMIN_USERNAME", "")
    if not raw.strip():
        return
    name = admin_username()
    db.set_admin_username(name)
    logger.info("Applied admin username from AGENTFORGE_ADMIN_USERNAME")


def apply_admin_password(db: _PasswordStore) -> None:
    """Overwrite the admin password hash when ``AGENTFORGE_ADMIN_PASSWORD`` is set.

    Empty means the WebUI cannot log in as admin. The config API key still works.
    The value is never logged.
    """
    raw = os.environ.get("AGENTFORGE_ADMIN_PASSWORD", "")
    if not raw.strip():
        return
    db.set_user_password(ADMIN_USER_ID, hash_password(raw))
    logger.info("Applied admin password from AGENTFORGE_ADMIN_PASSWORD")
