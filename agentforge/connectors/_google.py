"""Shared helpers for Google OAuth connectors (Gmail, Drive, BigQuery, YouTube, ...).

Every Google connector authenticates against ONE OAuth client. Same GCP project,
same consent screen. Client credentials resolve from a single place:

    connectors:
      google:
        client_id: ...
        client_secret: ...
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from ._config import get_connectors_config, get_credentials_dir

logger = get_logger(__name__)

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_BASE_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]

_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_OAUTH_NOT_CONFIGURED = (
    "Google OAuth not configured. Set connectors.google.client_id / client_secret in "
    "config.yaml, place a shared client_secret.json in the credentials_dir, or set the "
    "CONNECTOR_GOOGLE_CLIENT_ID / CONNECTOR_GOOGLE_CLIENT_SECRET env vars."
)


def read_client_secret_file() -> dict[str, str] | None:
    """Load client_id, secret, and redirect_uri from a client_secret.json file."""
    creds_dir = get_credentials_dir()
    google = get_connectors_config().get("google", {}) or {}
    filename = google.get("client_secret_file") or "client_secret.json"
    path = creds_dir / filename

    if not path.exists():
        return None

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("google connector: failed to read %s: %s", path, exc)
        return None

    for key in ("web", "installed"):
        inner = data.get(key)
        if not inner:
            continue
        client_id = inner.get("client_id", "")
        client_secret = inner.get("client_secret", "")
        if client_id and client_secret:
            result = {"client_id": client_id, "client_secret": client_secret}
            redirect_uris = inner.get("redirect_uris") or []
            if redirect_uris:
                result["redirect_uri"] = redirect_uris[0]
            return result

    return None


def get_google_client_config() -> dict[str, str]:
    """Resolve the shared Google OAuth client config."""
    creds = read_client_secret_file()
    if creds:
        return creds

    google = get_connectors_config().get("google", {}) or {}
    client_id = os.environ.get("CONNECTOR_GOOGLE_CLIENT_ID") or google.get("client_id", "")
    client_secret = os.environ.get("CONNECTOR_GOOGLE_CLIENT_SECRET") or google.get("client_secret", "")

    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}
    return {}


def require_google_client_config() -> dict[str, str]:
    """Like ``get_google_client_config`` but raise RuntimeError when unconfigured."""
    creds = get_google_client_config()
    if not creds:
        raise RuntimeError(_OAUTH_NOT_CONFIGURED)
    return creds


def fetch_account_email(access_token: str) -> str:
    """Return the connected Google account email via the OpenID userinfo endpoint."""
    if not access_token:
        return ""
    try:
        req = Request(_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data.get("email", "")
    except (HTTPError, URLError, Exception) as exc:
        logger.debug("google connector: userinfo fetch failed: %s", exc)
        return ""
