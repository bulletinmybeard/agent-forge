"""GitHub connector plugin — token-based auth (PAT), reuses the gh CLI tool.

v1 wraps the existing ``gh_command`` tool with a per-connection ``GH_TOKEN``
instead of re-implementing the GitHub REST API.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _api_base(url: str) -> str:
    """REST API base."""
    host = (url or "").strip().rstrip("/")
    if not host or "github.com" in host:
        return "https://api.github.com"
    return f"{host}/api/v3"


def _fetch_login(token: str, url: str = "") -> tuple[str, str]:
    """Return ``(login, name)`` for the PAT. Raises on HTTP/network error."""
    req = Request(
        f"{_api_base(url)}/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AgentForge",
        },
    )
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return data.get("login", ""), data.get("name", "")


class GitHubConnectorPlugin:
    connector_type = "github"
    display_name = "GitHub"
    description = "Repos, PRs, issues, releases, actions, search (via gh CLI)"
    default_aliases = ["@github", "@gh"]

    # SaaS-only (github.com)
    needs_url = False
    default_url = "https://github.com"

    # Token-based auth (Personal Access Token), not OAuth.
    auth_type = "token"
    oauth_scopes: list[str] = []
    oauth_auth_uri = ""
    oauth_token_uri = ""

    def get_oauth_client_config(self) -> dict[str, str]:
        raise RuntimeError("GitHub uses token auth, not OAuth")

    def create_tools(self, connection_id: str, token_accessor: Callable[[], str]) -> list[Callable[..., Any]]:
        """Wrap ``gh_command`` with this connection's PAT injected via GH_TOKEN."""
        from agentforge.tools.cli_tools import (
            clear_gh_token_override,
            gh_command,
            set_gh_token_override,
        )

        @functools.wraps(gh_command)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tokens = json.loads(token_accessor())
            url = (tokens.get("url") or "").strip().rstrip("/")
            host = ""
            if url and "github.com" not in url:
                host = url.replace("https://", "").replace("http://", "").rstrip("/")
            set_gh_token_override(
                token=tokens["token"],
                host=host,
                read_write=tokens.get("read_write", True),
            )
            try:
                return gh_command(*args, **kwargs)
            finally:
                clear_gh_token_override()

        return [wrapper]

    def system_prompt(self, account_email: str, read_write: bool = True) -> str:
        try:
            template = _PROMPT_PATH.read_text()
        except FileNotFoundError:
            template = "You are a GitHub assistant connected to {account_email}."

        rw_notice = (
            (
                "IMPORTANT: You are running in READ-WRITE mode. You CAN modify GitHub "
                "resources when asked — create/edit PRs and issues, comment, manage "
                "releases, etc. — via the matching `gh` subcommands."
            )
            if read_write
            else (
                "IMPORTANT: You are running in READ-ONLY mode. Only read `gh` commands "
                "are permitted (list/view/diff/search and GET `gh api`). If the user "
                "asks for a change, explain that write access is disabled."
            )
        )

        prompt = template.replace("{account_email}", account_email)
        return f"{rw_notice}\n\n{prompt}"

    def default_label(self, token_info: dict[str, Any]) -> str:
        """Derive the connection label from the GitHub username."""
        login, _ = _fetch_login(token_info.get("token", ""), token_info.get("url", ""))
        return login or "GitHub"

    def test_connection(self, access_token: str) -> dict[str, Any]:
        """Verify the PAT by fetching the authenticated user."""
        try:
            tokens = json.loads(access_token)
            token = tokens["token"]
            url = tokens.get("url", "")
        except (json.JSONDecodeError, KeyError) as exc:
            return {"ok": False, "error": f"Invalid token data: {exc}"}

        try:
            login, name = _fetch_login(token, url)
            return {"ok": True, "account": login, "name": name}
        except HTTPError as exc:
            if exc.code == 401:
                return {"ok": False, "error": "Invalid token (401 Unauthorized)"}
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
        except (URLError, Exception) as exc:
            return {"ok": False, "error": str(exc)}
