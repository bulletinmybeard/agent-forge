"""Google BigQuery connector plugin — OAuth-based, read-only query access."""

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
    """Read client_id/secret from client_secret.json (shared with Gmail/Drive)."""
    creds_dir = get_credentials_dir()
    cfg = get_connector_config("bigquery")
    filename = cfg.get("client_secret_file", "client_secret.json")
    path = creds_dir / filename

    if not path.exists():
        return None

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("bigquery connector: failed to read %s: %s", path, exc)
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


class BigQueryConnectorPlugin:
    connector_type = "bigquery"
    display_name = "BigQuery"
    description = "Run SQL queries against Google BigQuery datasets"
    default_aliases = ["@bigquery", "@bq"]

    oauth_scopes = [
        "https://www.googleapis.com/auth/bigquery",
    ]
    oauth_auth_uri = "https://accounts.google.com/o/oauth2/v2/auth"
    oauth_token_uri = "https://oauth2.googleapis.com/token"

    def get_oauth_client_config(self) -> dict[str, str]:
        creds = _load_client_secret_json()
        if creds:
            return creds

        cfg = get_connector_config("bigquery")
        client_id = os.environ.get("CONNECTOR_BIGQUERY_CLIENT_ID") or cfg.get("client_id", "")
        client_secret = os.environ.get("CONNECTOR_BIGQUERY_CLIENT_SECRET") or cfg.get("client_secret", "")
        if not client_id or not client_secret:
            raise RuntimeError(
                "BigQuery OAuth not configured. Place client_secret.json in "
                "~/.agentforge/ (or the configured credentials_dir), or set "
                "client_id/client_secret under connectors.bigquery in config.yaml"
            )
        return {"client_id": client_id, "client_secret": client_secret}

    auth_type = "oauth"

    def create_tools(
        self,
        connection_id: str,
        token_accessor: Callable[[], str],
        stored_tokens: dict[str, Any] | None = None,
    ) -> list[Callable[..., Any]]:
        from .tools import create_bigquery_tools

        project_id = (stored_tokens or {}).get("project_id", "")
        return create_bigquery_tools(connection_id, token_accessor, default_project_id=project_id)

    def system_prompt(self, account_email: str) -> str:
        try:
            template = _PROMPT_PATH.read_text()
        except FileNotFoundError:
            template = "You are a BigQuery assistant connected to {account_email}."
        return template.replace("{account_email}", account_email)

    def default_label(self, token_info: dict[str, Any]) -> str:
        access_token = token_info.get("access_token", "")
        if not access_token:
            return ""
        try:
            req = Request(
                "https://www.googleapis.com/oauth2/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return data.get("email", "")
        except (HTTPError, URLError, Exception) as exc:
            logger.debug("bigquery connector: could not fetch profile: %s", exc)
            return ""

    def test_connection(self, access_token: str) -> dict[str, Any]:
        try:
            req = Request(
                "https://bigquery.googleapis.com/bigquery/v2/projects",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            projects = data.get("projects") or []
            project_ids = [p.get("id", "") for p in projects[:5]]
            return {
                "ok": True,
                "account": ", ".join(project_ids) if project_ids else "no projects",
                "project_count": len(projects),
            }
        except HTTPError as exc:
            if exc.code == 403:
                return {"ok": False, "error": "BigQuery API not enabled or insufficient permissions"}
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
        except (URLError, Exception) as exc:
            return {"ok": False, "error": str(exc)}
