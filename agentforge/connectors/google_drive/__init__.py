"""Google Drive connector plugin."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from .._google import (
    GOOGLE_AUTH_URI,
    GOOGLE_BASE_SCOPES,
    GOOGLE_TOKEN_URI,
    require_google_client_config,
)

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


class GoogleDriveConnectorPlugin:
    connector_type = "google_drive"
    display_name = "Google Drive"
    description = "List, search, and read files from Google Drive (read-only)"
    default_aliases = ["@drive"]
    listable = False

    oauth_scopes = [*GOOGLE_BASE_SCOPES, "https://www.googleapis.com/auth/drive.readonly"]
    oauth_auth_uri = GOOGLE_AUTH_URI
    oauth_token_uri = GOOGLE_TOKEN_URI

    def get_oauth_client_config(self) -> dict[str, str]:
        return require_google_client_config(self.connector_type)

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
