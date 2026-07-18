"""SQLite-backed runtime command policy overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentforge.tools.command_policy import CommandPolicy, load_yaml_policy, merge_policies

if TYPE_CHECKING:
    from web.server.database import ChatDatabase

ToolName = Literal["shell", "ssh"]

_db: ChatDatabase | None = None


def _resolve_db_path() -> Path:
    env_path = os.environ.get("AGENTFORGE_CHAT_DB")
    if env_path:
        return Path(env_path).expanduser()

    from agentforge.config import get_config

    cfg = get_config()
    raw_path = cfg._raw.get("web", {}).get("database_path", "data/agentforge_chat.db")
    return Path(raw_path).expanduser()


def _get_db() -> ChatDatabase:
    global _db
    if _db is None:
        from web.server.database import ChatDatabase

        db_path = _resolve_db_path()
        _db = ChatDatabase(db_path)
        _db.create_tables()
    return _db


def reset_db() -> None:
    """Reset the module-level database singleton (for tests)."""
    global _db
    _db = None


def set_db(db: ChatDatabase) -> None:
    """Inject a ChatDatabase instance (for tests)."""
    global _db
    _db = db


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
    data = _get_db().get_command_policy_override(tool)
    if data is None:
        return None
    return _dict_to_policy(data)


def set_runtime_override(tool: ToolName, policy: CommandPolicy) -> None:
    _get_db().upsert_command_policy_override(tool, _policy_to_dict(policy))


def clear_runtime_override(tool: ToolName | None = None) -> int:
    return _get_db().delete_command_policy_override(tool)


def get_effective_policy(tool: ToolName) -> CommandPolicy:
    return merge_policies(load_yaml_policy(tool), get_runtime_override(tool))
