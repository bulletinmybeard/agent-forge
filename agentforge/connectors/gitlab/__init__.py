"""GitLab connector plugin — token-based auth (not OAuth)."""

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

# All 20 GitLab tool function names (from gitlab_tools.py)
_GITLAB_TOOL_NAMES = [
    "gitlab_projects",
    "gitlab_project",
    "gitlab_create_project",
    "gitlab_delete_project",
    "gitlab_update_project",
    "gitlab_file_content",
    "gitlab_commit_diff",
    "gitlab_branches",
    "gitlab_create_branch",
    "gitlab_delete_branch",
    "gitlab_create_merge_request",
    "gitlab_merge_requests",
    "gitlab_merge_request",
    "gitlab_merge_request_changes",
    "gitlab_merge_request_update",
    "gitlab_merge_request_approve",
    "gitlab_merge_request_merge",
    "gitlab_pipelines",
    "gitlab_pipeline_jobs",
    "gitlab_job_log",
    "gitlab_pipeline_retry",
    "gitlab_pipeline_cancel",
    "gitlab_runners",
    "gitlab_runner",
    "gitlab_runner_pause",
    "gitlab_runner_resume",
    "gitlab_users",
    "gitlab_user_events",
]


class GitLabConnectorPlugin:
    connector_type = "gitlab"
    display_name = "GitLab"
    description = "Projects, merge requests, pipelines, runners, users"
    default_aliases = ["@gitlab"]

    # Token-based auth, not OAuth
    auth_type = "token"
    oauth_scopes: list[str] = []
    oauth_auth_uri = ""
    oauth_token_uri = ""

    def get_oauth_client_config(self) -> dict[str, str]:
        raise RuntimeError("GitLab uses token auth, not OAuth")

    def create_tools(self, connection_id: str, token_accessor: Callable[[], str]) -> list[Callable[..., Any]]:
        """Wrap existing gitlab_tools functions with connector config injection.

        Each tool closure sets the thread-local override before calling the
        original tool function, then clears it afterward.
        """
        import agentforge.tools.gitlab_tools as gt
        from agentforge.tools.gitlab_tools import (
            clear_connector_override,
            set_connector_override,
        )

        def _make_wrapper(original_fn: Callable) -> Callable:
            @functools.wraps(original_fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                tokens = json.loads(token_accessor())
                set_connector_override(
                    url=tokens["url"],
                    token=tokens["token"],
                    read_write=tokens.get("read_write", True),
                )
                try:
                    return original_fn(*args, **kwargs)
                finally:
                    clear_connector_override()

            return wrapper

        tools = []
        for name in _GITLAB_TOOL_NAMES:
            fn = getattr(gt, name, None)
            if fn is not None:
                tools.append(_make_wrapper(fn))
            else:
                logger.warning("gitlab connector: tool %s not found in gitlab_tools", name)
        return tools

    def system_prompt(self, account_email: str, read_write: bool = True) -> str:
        try:
            template = _PROMPT_PATH.read_text()
        except FileNotFoundError:
            template = "You are a GitLab assistant connected to {account_email}."

        rw_notice = (
            (
                "IMPORTANT: You are running in READ-WRITE mode. You CAN and SHOULD "
                "modify GitLab resources when asked. You have tools to update merge "
                "requests, approve MRs, merge MRs, retry/cancel pipelines, and "
                "pause/resume runners."
            )
            if read_write
            else (
                "IMPORTANT: You are running in READ-ONLY mode. You can browse and "
                "inspect GitLab resources but cannot modify them. If the user asks "
                "to make a change, explain that write access is disabled."
            )
        )

        prompt = template.replace("{account_email}", account_email)
        return f"{rw_notice}\n\n{prompt}"

    def default_label(self, token_info: dict[str, Any]) -> str:
        return token_info.get("url", "GitLab")

    def test_connection(self, access_token: str) -> dict[str, Any]:
        """Test by fetching the authenticated user profile."""
        try:
            tokens = json.loads(access_token)
            url = tokens["url"].rstrip("/")
            token = tokens["token"]
        except (json.JSONDecodeError, KeyError) as exc:
            return {"ok": False, "error": f"Invalid token data: {exc}"}

        try:
            req = Request(
                f"{url}/api/v4/user",
                headers={
                    "PRIVATE-TOKEN": token,
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return {
                "ok": True,
                "account": data.get("username", ""),
                "name": data.get("name", ""),
                "url": url,
            }
        except HTTPError as exc:
            if exc.code == 401:
                return {"ok": False, "error": "Invalid token (401 Unauthorized)"}
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
        except (URLError, Exception) as exc:
            return {"ok": False, "error": str(exc)}
