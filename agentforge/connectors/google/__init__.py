"""Unified Google connector. One connection, user-selected products."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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

_PRODUCTS: dict[str, dict[str, str]] = {
    "gmail": {"label": "Gmail", "scope": "https://www.googleapis.com/auth/gmail.readonly"},
    "drive": {"label": "Google Drive", "scope": "https://www.googleapis.com/auth/drive.readonly"},
    "bigquery": {"label": "BigQuery", "scope": "https://www.googleapis.com/auth/bigquery"},
    "youtube": {"label": "YouTube", "scope": "https://www.googleapis.com/auth/youtube.readonly"},
}


def _selected_keys(products: list[str] | None) -> list[str]:
    """Valid product keys from a selection. Defaulting to all when empty/unknown."""
    keys = [p for p in (products or []) if p in _PRODUCTS]
    return keys or list(_PRODUCTS)


class GoogleConnectorPlugin:
    connector_type = "google"
    display_name = "Google"
    description = "Gmail, Drive, BigQuery, YouTube — pick which to connect (read-only)"
    default_aliases = ["@google"]
    auth_type = "oauth"

    # Scopes are chosen per connection.
    # This maximal set is only a fallback if a caller ignores product selection.
    oauth_scopes = [*GOOGLE_BASE_SCOPES, *(p["scope"] for p in _PRODUCTS.values())]
    oauth_auth_uri = GOOGLE_AUTH_URI
    oauth_token_uri = GOOGLE_TOKEN_URI

    @staticmethod
    def available_products() -> list[dict[str, str]]:
        """Products the connect UI offers, in display order."""
        return [{"key": k, "label": v["label"]} for k, v in _PRODUCTS.items()]

    def scopes_for(self, products: list[str] | None) -> list[str]:
        """OAuth scopes for the selected products (all if none given)."""
        return [*GOOGLE_BASE_SCOPES, *(_PRODUCTS[k]["scope"] for k in _selected_keys(products))]

    def product_display_names(self, stored_tokens: dict[str, Any] | None) -> list[str]:
        keys = _selected_keys((stored_tokens or {}).get("products"))
        return [_PRODUCTS[k]["label"] for k in keys]

    def get_oauth_client_config(self) -> dict[str, str]:
        return require_google_client_config()

    def create_tools(
        self,
        connection_id: str,
        token_accessor: Callable[[], str],
        stored_tokens: dict[str, Any] | None = None,
    ) -> list[Callable[..., Any]]:
        from ..bigquery.tools import create_bigquery_tools
        from ..gmail.tools import create_gmail_tools
        from ..google_drive.tools import create_drive_tools
        from ..youtube.tools import create_youtube_tools

        keys = _selected_keys((stored_tokens or {}).get("products"))
        project_id = (stored_tokens or {}).get("project_id", "")
        tools: list[Callable[..., Any]] = []
        if "gmail" in keys:
            tools += create_gmail_tools(connection_id, token_accessor)
        if "drive" in keys:
            tools += create_drive_tools(connection_id, token_accessor)
        if "bigquery" in keys:
            tools += create_bigquery_tools(connection_id, token_accessor, default_project_id=project_id)
        if "youtube" in keys:
            tools += create_youtube_tools(connection_id, token_accessor)
        return tools

    def system_prompt(self, account_email: str) -> str:
        try:
            template = _PROMPT_PATH.read_text()
        except FileNotFoundError:
            template = "You are a Google assistant connected to {account_email}."
        return template.replace("{account_email}", account_email)

    def default_label(self, token_info: dict[str, Any]) -> str:
        return fetch_account_email(token_info.get("access_token", ""))

    def test_connection(self, access_token: str) -> dict[str, Any]:
        email = fetch_account_email(access_token)
        if email:
            return {"ok": True, "account": email}
        return {"ok": False, "error": "Could not verify the Google account (token or scope issue)."}
