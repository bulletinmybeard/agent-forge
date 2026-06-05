"""Google Drive connector plugin."""

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
    """Read client_id/secret from client_secret.json (shared with Gmail connector)."""
    creds_dir = get_credentials_dir()
    cfg = get_connector_config("google_drive")
    filename = cfg.get("client_secret_file", "client_secret.json")
    path = creds_dir / filename

    if not path.exists():
        return None

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("drive connector: failed to read %s: %s", path, exc)
        return None

    for key in ("web", "installed"):
        if key in data:
            inner = data[key]
            client_id = inner.get("client_id", "")
            client_secret = inner.get("client_secret", "")
            if client_id and client_secret:
                result = {"client_id": client_id, "client_secret": client_secret}
                redirect_uris = inner.get("redirect_uris") or []
                if redirect_uris:
                    result["redirect_uri"] = redirect_uris[0]
                return result

    return None


class GoogleDriveConnectorPlugin:
    connector_type = "google_drive"
    display_name = "Google Drive"
    description = "List, search, and read files from Google Drive (read-only)"
    default_aliases = ["@drive"]

    oauth_scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    oauth_auth_uri = "https://accounts.google.com/o/oauth2/v2/auth"
    oauth_token_uri = "https://oauth2.googleapis.com/token"

    def get_oauth_client_config(self) -> dict[str, str]:
        creds = _load_client_secret_json()
        if creds:
            return creds

        cfg = get_connector_config("google_drive")
        client_id = os.environ.get("CONNECTOR_GOOGLE_DRIVE_CLIENT_ID") or cfg.get("client_id", "")
        client_secret = os.environ.get("CONNECTOR_GOOGLE_DRIVE_CLIENT_SECRET") or cfg.get("client_secret", "")
        if not client_id or not client_secret:
            raise RuntimeError(
                "Google Drive OAuth not configured. Place client_secret.json in "
                "~/.agentforge/ (or the configured credentials_dir), or set "
                "client_id/client_secret under connectors.google_drive in config.yaml"
            )
        return {"client_id": client_id, "client_secret": client_secret}

    def create_tools(self, connection_id: str, token_accessor: Callable[[], str]) -> list[Callable[..., Any]]:
        from .tools import create_drive_tools

        return create_drive_tools(connection_id, token_accessor)

    def system_prompt(self, account_email: str) -> str:
        try:
            template = _PROMPT_PATH.read_text()
        except FileNotFoundError:
            template = "You are a Google Drive assistant connected to {account_email}."
        return template.replace("{account_email}", account_email)

    def default_label(self, token_info: dict[str, Any]) -> str:
        access_token = token_info.get("access_token", "")
        if not access_token:
            return ""
        try:
            req = Request(
                "https://www.googleapis.com/drive/v3/about?fields=user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return data.get("user", {}).get("emailAddress", "")
        except (HTTPError, URLError, Exception) as exc:
            logger.debug("drive connector: could not fetch profile: %s", exc)
            return ""

    def test_connection(self, access_token: str) -> dict[str, Any]:
        try:
            req = Request(
                "https://www.googleapis.com/drive/v3/about?fields=user,storageQuota",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            user = data.get("user", {})
            return {
                "ok": True,
                "account": user.get("emailAddress", ""),
                "display_name": user.get("displayName", ""),
            }
        except (HTTPError, URLError) as exc:
            return {"ok": False, "error": f"HTTP error: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
