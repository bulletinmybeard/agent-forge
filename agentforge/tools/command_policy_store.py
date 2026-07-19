"""SQLite-backed runtime command policy overrides."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentforge.config import get_config
from agentforge.tools.command_policy import CommandPolicy, load_yaml_policy, merge_policies

if TYPE_CHECKING:
    from web.server.database import ChatDatabase

logger = logging.getLogger(__name__)

ToolName = Literal["shell", "ssh"]

_db: ChatDatabase | None = None
_web_unavailable: bool = False

# Same default as the web app / config.example — keep policy store on the chat DB.
_DEFAULT_CHAT_DB = "data/web_chat.db"


def _resolve_db_path() -> Path:
    env_path = os.environ.get("AGENTFORGE_CHAT_DB")
    if env_path:
        return Path(env_path).expanduser()

    cfg = get_config()
    raw_path = cfg._raw.get("web", {}).get("database_path", _DEFAULT_CHAT_DB)
    return Path(raw_path).expanduser()


def _try_get_db() -> ChatDatabase | None:
    """Return the chat DB, or ``None`` when the web package is not installed."""
    global _db, _web_unavailable
    if _db is not None:
        return _db
    if _web_unavailable:
        return None
    # Lazy: core wheel does not ship ``web`` (see package hatch targets).
    try:
        from web.server.database import ChatDatabase
    except ImportError:
        _web_unavailable = True
        return None

    db_path = _resolve_db_path()
    _db = ChatDatabase(db_path)
    try:
        _db.create_tables()
    except Exception:
        # Schema may still be usable (or will soft-fail on read). Do not block
        # shell/ssh on a migration race — YAML policy remains authoritative.
        logger.warning("command_policy_store: create_tables failed for %s", db_path, exc_info=True)
    return _db


def _require_db() -> ChatDatabase:
    db = _try_get_db()
    if db is None:
        raise RuntimeError(
            "Runtime command-policy overrides require the AgentForge web stack "
            "(module 'web' is not installed). YAML tools.*.permissions still apply."
        )
    return db


def reset_db() -> None:
    """Reset the module-level database singleton (for tests)."""
    global _db, _web_unavailable
    _db = None
    _web_unavailable = False


def set_db(db: ChatDatabase) -> None:
    """Inject a ChatDatabase instance (for tests)."""
    global _db, _web_unavailable
    _db = db
    _web_unavailable = False


def _policy_to_dict(policy: CommandPolicy) -> dict:
    return {
        "mode": policy.mode,
        "allowed_commands": list(policy.allowed_commands),
        "allowed_patterns": list(policy.allowed_patterns),
        "blocked_patterns": list(policy.blocked_patterns),
    }


def _dict_to_policy(data: dict) -> CommandPolicy:
    return CommandPolicy(
        mode=data.get("mode", "confirm"),
        allowed_commands=tuple(data.get("allowed_commands") or ()),
        allowed_patterns=tuple(data.get("allowed_patterns") or ()),
        blocked_patterns=tuple(data.get("blocked_patterns") or ()),
    )


def get_runtime_override(tool: ToolName) -> CommandPolicy | None:
    db = _try_get_db()
    if db is None:
        return None
    try:
        data = db.get_command_policy_override(tool)
    except Exception as exc:
        # Missing table / corrupt stamp: try one migration pass, then YAML-only.
        logger.warning(
            "command_policy_store: read override for %s failed (%s) — repairing schema",
            tool,
            exc,
        )
        try:
            db.create_tables()
            data = db.get_command_policy_override(tool)
        except Exception:
            logger.warning(
                "command_policy_store: schema repair failed — using YAML policy only",
                exc_info=True,
            )
            return None
    if data is None:
        return None
    return _dict_to_policy(data)


def set_runtime_override(tool: ToolName, policy: CommandPolicy) -> None:
    _require_db().upsert_command_policy_override(tool, _policy_to_dict(policy))


def clear_runtime_override(tool: ToolName | None = None) -> int:
    return _require_db().delete_command_policy_override(tool)


def get_effective_policy(tool: ToolName) -> CommandPolicy:
    return merge_policies(load_yaml_policy(tool), get_runtime_override(tool))
