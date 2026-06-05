"""Gmail connector plugin."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from .._config import get_connector_config, get_credentials_dir

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_client_secret_json() -> dict[str, str] | None:
    """Read client_id/secret from the existing client_secret.json file.

    Uses the same path resolution as the old @email mode:
    GMAIL_CREDENTIALS_DIR env → config.yaml credentials_dir → ~/.agentforge/
    """
    creds_dir = get_credentials_dir()
    cfg = get_connector_config("gmail")
    filename = cfg.get("client_secret_file", "client_secret.json")
    path = creds_dir / filename

    if not path.exists():
        return None

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("gmail connector: failed to read %s: %s", path, exc)
        return None

    # client_secret.json has a top-level key like "installed" or "web"
    for key in ("installed", "web"):
        if key in data:
            inner = data[key]
            client_id = inner.get("client_id", "")
            client_secret = inner.get("client_secret", "")
            if client_id and client_secret:
                logger.debug("gmail connector: loaded credentials from %s (type=%s)", path, key)
                result = {"client_id": client_id, "client_secret": client_secret}
                redirect_uris = inner.get("redirect_uris") or []
                if redirect_uris:
                    result["redirect_uri"] = redirect_uris[0]
                return result

    logger.warning("gmail connector: %s exists but has no client_id/secret", path)
    return None


class GmailConnectorPlugin:
    connector_type = "gmail"
    display_name = "Gmail"
    description = "Search, read, and unsubscribe from emails (read-only + unsubscribe)"
    default_aliases = ["@gmail", "@email"]

    oauth_scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    oauth_auth_uri = "https://accounts.google.com/o/oauth2/v2/auth"
    oauth_token_uri = "https://oauth2.googleapis.com/token"

    def get_oauth_client_config(self) -> dict[str, str]:
        # 1. Try client_secret.json (same file the old @email mode used)
        creds = _load_client_secret_json()
        if creds:
            return creds

        # 2. Fall back to config.yaml / env vars
        cfg = get_connector_config("gmail")
        client_id = os.environ.get("CONNECTOR_GMAIL_CLIENT_ID") or cfg.get("client_id", "")
        client_secret = os.environ.get("CONNECTOR_GMAIL_CLIENT_SECRET") or cfg.get("client_secret", "")
        if not client_id or not client_secret:
            raise RuntimeError(
                "Gmail OAuth not configured. Place client_secret.json in "
                "~/.agentforge/ (or the configured credentials_dir), or set "
                "client_id/client_secret under connectors.gmail in config.yaml"
            )
        return {"client_id": client_id, "client_secret": client_secret}

    def create_tools(self, connection_id: str, token_accessor: Callable[[], str]) -> list[Callable[..., Any]]:
        from .tools import create_gmail_tools

        return create_gmail_tools(connection_id, token_accessor)

    def system_prompt(self, account_email: str) -> str:
        try:
            template = _PROMPT_PATH.read_text()
        except FileNotFoundError:
            template = "You are a Gmail assistant connected to {account_email}."
        return template.replace("{account_email}", account_email)

    def default_label(self, token_info: dict[str, Any]) -> str:
        access_token = token_info.get("access_token", "")
        if not access_token:
            return ""
        try:
            req = Request(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return data.get("emailAddress", "")
        except (HTTPError, URLError, Exception) as exc:
            logger.debug("gmail connector: could not fetch profile: %s", exc)
            return ""

    def test_connection(self, access_token: str) -> dict[str, Any]:
        try:
            req = Request(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return {
                "ok": True,
                "account": data.get("emailAddress", ""),
                "messages_total": int(data.get("messagesTotal", 0) or 0),
            }
        except (HTTPError, URLError) as exc:
            return {"ok": False, "error": f"HTTP error: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
