"""YouTube connector plugin. OAuth-based, read-only YouTube Data API access."""

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
    fetch_account_email,
    require_google_client_config,
)

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


class YouTubeConnectorPlugin:
    connector_type = "youtube"
    display_name = "YouTube"
    description = "Search videos and read channel, playlist, and video metadata (read-only)"
    default_aliases = ["@youtube", "@yt"]
    auth_type = "oauth"
    listable = False

    oauth_scopes = [*GOOGLE_BASE_SCOPES, "https://www.googleapis.com/auth/youtube.readonly"]
    oauth_auth_uri = GOOGLE_AUTH_URI
    oauth_token_uri = GOOGLE_TOKEN_URI

    def get_oauth_client_config(self) -> dict[str, str]:
        return require_google_client_config(self.connector_type)

    def create_tools(self, connection_id: str, token_accessor: Callable[[], str]) -> list[Callable[..., Any]]:
        from .tools import create_youtube_tools

        return create_youtube_tools(connection_id, token_accessor)

    def system_prompt(self, account_email: str) -> str:
        try:
            template = _PROMPT_PATH.read_text()
        except FileNotFoundError:
            template = "You are a YouTube assistant connected to {account_email}."
        return template.replace("{account_email}", account_email)

    def default_label(self, token_info: dict[str, Any]) -> str:
        return fetch_account_email(token_info.get("access_token", ""))

    def test_connection(self, access_token: str) -> dict[str, Any]:
        try:
            req = Request(
                "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("items") or []
            if items:
                snippet = items[0].get("snippet", {})
                return {"ok": True, "account": snippet.get("title", ""), "channel_id": items[0].get("id", "")}
            return {"ok": True, "account": "no channel", "channel_id": ""}
        except HTTPError as exc:
            if exc.code == 403:
                return {"ok": False, "error": "YouTube Data API not enabled or insufficient permissions"}
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
        except (URLError, Exception) as exc:
            return {"ok": False, "error": str(exc)}
