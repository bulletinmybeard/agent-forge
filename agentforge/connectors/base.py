"""Connector plugin protocol — defines the interface every connector type must implement."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ConnectorPlugin(Protocol):
    """Interface every connector type module must implement.

    A connector plugin defines how to authenticate with an external service,
    what tools to expose, and what system prompt to use when the agent runs.
    """

    connector_type: str
    display_name: str
    description: str
    default_aliases: list[str]

    # OAuth
    oauth_scopes: list[str]
    oauth_auth_uri: str
    oauth_token_uri: str

    def get_oauth_client_config(self) -> dict[str, str]:
        """Return ``{"client_id": ..., "client_secret": ...}`` from env vars or config."""
        ...

    def create_tools(self, connection_id: str, token_accessor: Callable[[], str]) -> list[Callable[..., Any]]:
        """Return tool callables parameterized with this connection's credentials."""
        ...

    def system_prompt(self, account_email: str) -> str:
        """Return the system prompt with connection context injected."""
        ...

    def default_label(self, token_info: dict[str, Any]) -> str:
        """Generate a default label from the OAuth token/profile response."""
        ...

    def test_connection(self, access_token: str) -> dict[str, Any]:
        """Make a lightweight API call to verify the token works.

        Return ``{"ok": True, "account": "user@example.com"}`` on success,
        ``{"ok": False, "error": "..."}`` on failure.
        """
        ...
