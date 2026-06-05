"""ConnectionManager — CRUD, token lifecycle, and dynamic agent registration."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from . import ConnectorRegistry
from .encryption import decrypt_tokens, encrypt_tokens

if TYPE_CHECKING:
    from agentforge.tools.registry import ToolRegistry

logger = get_logger(__name__)


class ConnectionManager:
    """Manages connector instances: creation, deletion, token refresh, agent wiring."""

    def __init__(
        self, db_session_factory: Callable, registry: ConnectorRegistry, tool_registry: "ToolRegistry"
    ) -> None:
        self._db_session_factory = db_session_factory
        self._registry = registry
        self._tool_registry = tool_registry
        self._tool_names: dict[str, list[str]] = {}
        self._agents: dict[str, dict] = {}

    def load_connections(self, custom_agents: dict) -> None:
        """Load all active connections from DB and register dynamic agents."""
        from web.server.database.models import Connection

        session = self._db_session_factory()
        try:
            rows = session.query(Connection).filter(Connection.status == "active").all()
            conns = [self._to_dict(row) for row in rows]
        finally:
            session.close()

        for conn in conns:
            try:
                self._register_agent(conn, custom_agents)
                logger.info("Loaded connector agent: %s (%s)", conn["label"], conn["connector_type"])
            except Exception as exc:
                logger.warning("Failed to load connector %s: %s", conn["id"], exc)

    def create_connection(self, connector_type: str, label: str, tokens: dict, custom_agents: dict) -> dict:
        """Create a new connection, store encrypted tokens, register agent."""
        from web.server.database.models import Connection

        plugin = self._registry.get(connector_type)
        if plugin is None:
            raise ValueError(f"Unknown connector type: {connector_type}")

        account_id = ""
        try:
            account_id = plugin.default_label(tokens)
        except Exception as exc:
            logger.debug("Could not derive account identifier: %s", exc)

        if not label:
            label = account_id or plugin.display_name

        # Enforce unique labels (used for #hashtag routing)
        existing = self.list_connections()
        existing_slugs = {re.sub(r"[^a-z0-9]+", "-", c["label"].lower()).strip("-") for c in existing}
        new_slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        if new_slug in existing_slugs:
            raise ValueError(f"Label '{label}' conflicts with an existing connection. Choose a unique label.")

        connection_id = str(uuid.uuid4())
        encrypted = encrypt_tokens(tokens)

        session = self._db_session_factory()
        try:
            row = Connection(
                id=connection_id,
                connector_type=connector_type,
                label=label,
                account_identifier=account_id,
                encrypted_tokens=encrypted,
                status="active",
            )
            session.add(row)
            session.commit()
            result = self._to_dict(row)
        finally:
            session.close()

        self._register_agent(result, custom_agents)
        return result

    def delete_connection(self, connection_id: str, custom_agents: dict) -> bool:
        """Delete a connection and unregister its agent."""
        from web.server.database.models import Connection

        self._unregister_agent(connection_id, custom_agents)

        session = self._db_session_factory()
        try:
            row = session.query(Connection).filter(Connection.id == connection_id).first()
            if not row:
                return False
            session.delete(row)
            session.commit()
            return True
        finally:
            session.close()

    def update_label(self, connection_id: str, label: str, custom_agents: dict) -> dict | None:
        """Update a connection's label and re-register aliases."""
        from web.server.database.models import Connection

        session = self._db_session_factory()
        try:
            row = session.query(Connection).filter(Connection.id == connection_id).first()
            if not row:
                return None
            row.label = label
            session.commit()
            result = self._to_dict(row)
        finally:
            session.close()

        self._unregister_agent(connection_id, custom_agents)
        self._register_agent(result, custom_agents)
        return result

    def get_connection(self, connection_id: str) -> dict | None:
        from web.server.database.models import Connection

        session = self._db_session_factory()
        try:
            row = session.query(Connection).filter(Connection.id == connection_id).first()
            return self._to_dict(row) if row else None
        finally:
            session.close()

    def list_connections(self) -> list[dict]:
        from web.server.database.models import Connection

        session = self._db_session_factory()
        try:
            rows = session.query(Connection).order_by(Connection.created_at).all()
            return [self._to_dict(row) for row in rows]
        finally:
            session.close()

    def get_access_token(self, connection_id: str) -> str:
        """Return a valid access token, refreshing if needed.

        For OAuth connectors: returns the access_token string (refreshes if expired).
        For token-based connectors (GitLab): returns a JSON string with all credentials.
        """
        from web.server.database.models import Connection

        session = self._db_session_factory()
        try:
            row = session.query(Connection).filter(Connection.id == connection_id).first()
            if not row:
                raise ValueError(f"Connection {connection_id} not found")

            tokens = decrypt_tokens(row.encrypted_tokens)

            # Token-based connectors (no refresh_token, no expiry) — return full blob
            plugin = self._registry.get(row.connector_type)
            auth_type = getattr(plugin, "auth_type", "oauth") if plugin else "oauth"
            if auth_type == "token":
                row.last_used_at = datetime.now()
                session.commit()
                return json.dumps(tokens)

            # OAuth connectors — refresh if needed
            expiry_str = tokens.get("expiry")

            needs_refresh = True
            if expiry_str:
                try:
                    expiry = datetime.fromisoformat(expiry_str)
                    needs_refresh = datetime.now() >= expiry - timedelta(minutes=5)
                except (ValueError, TypeError):
                    needs_refresh = True

            if needs_refresh and tokens.get("refresh_token"):
                try:
                    new_tokens = self._refresh_token(tokens)
                    tokens.update(new_tokens)
                    row.encrypted_tokens = encrypt_tokens(tokens)
                    row.status = "active"
                    row.last_error = None
                    session.commit()
                except Exception as exc:
                    row.status = "expired"
                    row.last_error = str(exc)
                    session.commit()
                    raise RuntimeError(f"Token refresh failed: {exc}") from exc

            row.last_used_at = datetime.now()
            session.commit()
            return tokens["access_token"]
        finally:
            session.close()

    def test_connection(self, connection_id: str) -> dict:
        """Test a connection by making a lightweight API call."""
        conn = self.get_connection(connection_id)
        if not conn:
            return {"ok": False, "error": "Connection not found"}

        plugin = self._registry.get(conn["connector_type"])
        if not plugin:
            return {"ok": False, "error": f"Unknown connector type: {conn['connector_type']}"}

        try:
            token = self.get_access_token(connection_id)
            return plugin.test_connection(token)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def reconnect(self, connection_id: str) -> dict | None:
        """Return the connection info needed to start a re-auth flow."""
        return self.get_connection(connection_id)

    # -- internal -----------------------------------------------------------

    def _refresh_token(self, tokens: dict) -> dict:
        """Exchange a refresh token for new access + expiry."""
        data = urlencode(
            {
                "client_id": tokens["client_id"],
                "client_secret": tokens["client_secret"],
                "refresh_token": tokens["refresh_token"],
                "grant_type": "refresh_token",
            }
        ).encode()
        req = Request(tokens["token_uri"], data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Token refresh HTTP error: {exc}") from exc

        expires_in = int(result.get("expires_in", 3600))
        expiry = datetime.now() + timedelta(seconds=expires_in)
        refreshed = {
            "access_token": result["access_token"],
            "expiry": expiry.isoformat(),
        }
        if "refresh_token" in result:
            refreshed["refresh_token"] = result["refresh_token"]
        return refreshed

    def _register_agent(self, conn: dict, custom_agents: dict) -> None:
        """Register a connection as a dynamic custom agent."""
        plugin = self._registry.get(conn["connector_type"])
        if not plugin:
            return

        connection_id = conn["id"]

        def token_accessor() -> str:
            return self.get_access_token(connection_id)

        # Load stored tokens for plugins that need extra config (e.g., BigQuery project_id)
        stored_tokens = {}
        try:
            from web.server.database.models import Connection

            session = self._db_session_factory()
            try:
                row = session.query(Connection).filter(Connection.id == connection_id).first()
                if row:
                    stored_tokens = decrypt_tokens(row.encrypted_tokens)
            finally:
                session.close()
        except Exception:
            pass

        try:
            tools = plugin.create_tools(connection_id, token_accessor, stored_tokens=stored_tokens)
        except TypeError:
            tools = plugin.create_tools(connection_id, token_accessor)

        tool_names = []
        for fn in tools:
            # Scope tool names per connection to avoid collisions
            scoped_name = f"{fn.__name__}_{connection_id[:8]}"
            fn.__name__ = scoped_name
            fn.__qualname__ = scoped_name
            # in_process: the closure holds this connection's live credentials,
            # so it only exists in this process. Pin it so the agent loop never
            # cross-dispatches it to a worker (whose registry never had it).
            self._tool_registry.register(fn, name=scoped_name, in_process=True)
            tool_names.append(scoped_name)
            logger.info("Registered connector tool: %s (connection=%s)", scoped_name, connection_id[:8])
        self._tool_names[connection_id] = tool_names

        account_email = conn.get("account_identifier") or conn.get("label", "")
        aliases = self._generate_aliases(conn)

        agent_cfg = {
            "id": f"connector:{connection_id}",
            "description": f"{plugin.display_name} -- {conn['label']}",
            "profile": "cloud-heavy",
            "tools": tool_names,
            "max_iterations": 15,
            "aliases": aliases,
            "prompt_text": self._build_prompt(plugin, conn, account_email),
            "no_history": True,
            "source": "connector",
        }
        self._agents[connection_id] = agent_cfg

        for alias in aliases:
            custom_agents[alias.lower()] = agent_cfg

    def _build_prompt(self, plugin: Any, conn: dict, account_email: str) -> str:
        """Build the system prompt, passing extra args for token-based connectors."""
        auth_type = getattr(plugin, "auth_type", "oauth")
        if auth_type == "token":
            tokens = decrypt_tokens(conn.get("_encrypted_tokens", "")) if conn.get("_encrypted_tokens") else {}
            if not tokens:
                from web.server.database.models import Connection

                session = self._db_session_factory()
                try:
                    row = session.query(Connection).filter(Connection.id == conn["id"]).first()
                    if row:
                        tokens = decrypt_tokens(row.encrypted_tokens)
                finally:
                    session.close()
            read_write = tokens.get("read_write", True)
            return plugin.system_prompt(account_email, read_write=read_write)
        return plugin.system_prompt(account_email)

    def _unregister_agent(self, connection_id: str, custom_agents: dict) -> None:
        """Remove a connection's agent and tools."""
        tool_names = self._tool_names.pop(connection_id, [])
        for name in tool_names:
            self._tool_registry.unregister(name)

        agent_cfg = self._agents.pop(connection_id, None)
        if agent_cfg:
            for alias in agent_cfg.get("aliases", []):
                custom_agents.pop(alias.lower(), None)

    def _generate_aliases(self, conn: dict) -> list[str]:
        """Generate @-aliases for a connector agent."""
        plugin = self._registry.get(conn["connector_type"])
        if not plugin:
            return []

        aliases = []

        same_type = [
            c
            for c in self.list_connections()
            if c["connector_type"] == conn["connector_type"] and c["status"] == "active"
        ]
        if len(same_type) <= 1:
            aliases.extend(plugin.default_aliases)

        slug = re.sub(r"[^a-z0-9]+", "-", conn["label"].lower()).strip("-")
        label_alias = f"@{slug}"
        if label_alias not in aliases:
            aliases.append(label_alias)

        return aliases

    @staticmethod
    def _to_dict(row: Any) -> dict:
        return {
            "id": row.id,
            "connector_type": row.connector_type,
            "label": row.label,
            "account_identifier": row.account_identifier,
            "status": row.status,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "last_error": row.last_error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
