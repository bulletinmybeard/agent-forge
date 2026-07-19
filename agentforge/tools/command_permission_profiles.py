"""Named command-permission profiles (tight / open / user-defined).

Similar to how Claude, Grok, or Gemini CLI with their global and project-based
JSON settings (e.g.,  ``settings.json``) handle allow/deny command lists:
a profile is a full snapshot of shell and/or ssh policy that can be applied
as the runtime override in one step.

Sources (merged by id; user wins on conflict):
  1. Built-in YAML under ``tools.command_permission_profiles``
  2. User-saved rows in SQLite (``command_permission_profiles`` table)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from agentforge.config import get_config
from agentforge.tools.command_policy import CommandPolicy
from agentforge.tools.command_policy_store import (
    _try_get_db,
    get_runtime_override,
    set_runtime_override,
)

logger = logging.getLogger(__name__)

ProfileSource = Literal["yaml", "user"]
_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,62}$")


@dataclass(frozen=True)
class PermissionProfile:
    id: str
    description: str
    source: ProfileSource
    shell: dict[str, Any] | None = None
    ssh: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "source": self.source,
            "shell": self.shell,
            "ssh": self.ssh,
            "builtin": self.source == "yaml",
        }


def _policy_dict(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    mode = data.get("mode") or "confirm"
    if mode not in ("confirm", "allowlist", "denylist"):
        mode = "confirm"
    return {
        "mode": mode,
        "allowed_commands": [str(x) for x in (data.get("allowed_commands") or []) if str(x).strip()],
        "allowed_patterns": [str(x) for x in (data.get("allowed_patterns") or []) if str(x).strip()],
        "blocked_patterns": [str(x) for x in (data.get("blocked_patterns") or []) if str(x).strip()],
    }


def _dict_to_policy(data: dict[str, Any]) -> CommandPolicy:
    return CommandPolicy(
        mode=data.get("mode", "confirm"),  # type: ignore[arg-type]
        allowed_commands=tuple(data.get("allowed_commands") or ()),
        allowed_patterns=tuple(data.get("allowed_patterns") or ()),
        blocked_patterns=tuple(data.get("blocked_patterns") or ()),
    )


def validate_profile_id(profile_id: str) -> str:
    pid = (profile_id or "").strip()
    if not _ID_RE.match(pid):
        raise ValueError(
            f"invalid profile id {profile_id!r}: use letters, digits, _ or - (start with a letter, max 63 chars)"
        )
    return pid


def _load_yaml_profiles() -> dict[str, PermissionProfile]:
    cfg = get_config()
    raw = (cfg._raw.get("tools") or {}).get("command_permission_profiles") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, PermissionProfile] = {}
    for key, body in raw.items():
        if not isinstance(body, dict):
            continue
        try:
            pid = validate_profile_id(str(key))
        except ValueError:
            logger.warning("skipping invalid profile id in YAML: %s", key)
            continue
        out[pid] = PermissionProfile(
            id=pid,
            description=str(body.get("description") or ""),
            source="yaml",
            shell=_policy_dict(body.get("shell")),
            ssh=_policy_dict(body.get("ssh")),
        )
    return out


def _load_user_profiles() -> dict[str, PermissionProfile]:
    db = _try_get_db()
    if db is None or not hasattr(db, "list_command_permission_profiles"):
        return {}
    out: dict[str, PermissionProfile] = {}
    try:
        for row in db.list_command_permission_profiles():
            pid = row.get("id")
            if not pid:
                continue
            out[str(pid)] = PermissionProfile(
                id=str(pid),
                description=str(row.get("description") or ""),
                source="user",
                shell=_policy_dict(row.get("shell")),
                ssh=_policy_dict(row.get("ssh")),
            )
    except Exception:
        logger.warning("failed to load user command permission profiles", exc_info=True)
    return out


def list_profiles() -> list[PermissionProfile]:
    """YAML builtins first (sorted), then user profiles (sorted). User overrides YAML id."""
    yaml_p = _load_yaml_profiles()
    user_p = _load_user_profiles()
    # User wins on id collision
    merged = {**yaml_p, **user_p}
    yaml_ids = sorted(yaml_p.keys())
    user_only = sorted(k for k in user_p if k not in yaml_p)
    order = yaml_ids + user_only
    # Include any stragglers
    for k in sorted(merged.keys()):
        if k not in order:
            order.append(k)
    return [merged[k] for k in order if k in merged]


def get_profile(profile_id: str) -> PermissionProfile | None:
    pid = (profile_id or "").strip()
    user_p = _load_user_profiles()
    if pid in user_p:
        return user_p[pid]
    return _load_yaml_profiles().get(pid)


def apply_profile(profile_id: str) -> PermissionProfile:
    """Copy profile shell/ssh policies into the runtime override tables."""
    profile = get_profile(profile_id)
    if profile is None:
        raise KeyError(f"unknown permission profile: {profile_id}")
    if profile.shell is not None:
        set_runtime_override("shell", _dict_to_policy(profile.shell))
    if profile.ssh is not None:
        set_runtime_override("ssh", _dict_to_policy(profile.ssh))
    db = _try_get_db()
    if db is not None and hasattr(db, "set_active_command_permission_profile"):
        try:
            db.set_active_command_permission_profile(profile.id)
        except Exception:
            logger.warning("failed to record active permission profile", exc_info=True)
    return profile


def save_user_profile(
    profile_id: str,
    *,
    description: str = "",
    shell: dict[str, Any] | None = None,
    ssh: dict[str, Any] | None = None,
    from_current_overrides: bool = False,
) -> PermissionProfile:
    """Create or update a user (SQLite) profile. Cannot overwrite YAML-only id without user row."""
    pid = validate_profile_id(profile_id)
    if from_current_overrides:
        shell_ov = get_runtime_override("shell")
        ssh_ov = get_runtime_override("ssh")
        shell = (
            {
                "mode": shell_ov.mode,
                "allowed_commands": list(shell_ov.allowed_commands),
                "allowed_patterns": list(shell_ov.allowed_patterns),
                "blocked_patterns": list(shell_ov.blocked_patterns),
            }
            if shell_ov
            else shell
        )
        ssh = (
            {
                "mode": ssh_ov.mode,
                "allowed_commands": list(ssh_ov.allowed_commands),
                "allowed_patterns": list(ssh_ov.allowed_patterns),
                "blocked_patterns": list(ssh_ov.blocked_patterns),
            }
            if ssh_ov
            else ssh
        )

    shell_d = _policy_dict(shell)
    ssh_d = _policy_dict(ssh)
    if shell_d is None and ssh_d is None:
        raise ValueError("profile must include shell and/or ssh policy")

    db = _try_get_db()
    if db is None or not hasattr(db, "upsert_command_permission_profile"):
        raise RuntimeError("user permission profiles require the AgentForge web stack + chat DB")

    db.upsert_command_permission_profile(
        pid,
        {
            "description": description or "",
            "shell": shell_d,
            "ssh": ssh_d,
        },
    )
    return PermissionProfile(
        id=pid,
        description=description or "",
        source="user",
        shell=shell_d,
        ssh=ssh_d,
    )


def delete_user_profile(profile_id: str) -> bool:
    """Delete a user-saved profile. Built-in YAML profiles cannot be deleted."""
    pid = (profile_id or "").strip()
    if pid in _load_yaml_profiles() and pid not in _load_user_profiles():
        raise ValueError(f"cannot delete built-in YAML profile: {pid}")
    db = _try_get_db()
    if db is None or not hasattr(db, "delete_command_permission_profile"):
        return False
    return bool(db.delete_command_permission_profile(pid))


def get_active_profile_id() -> str | None:
    db = _try_get_db()
    if db is None or not hasattr(db, "get_active_command_permission_profile"):
        return None
    try:
        return db.get_active_command_permission_profile()
    except Exception:
        return None


def clear_active_profile() -> None:
    """Clear the active-profile marker without changing runtime overrides."""
    db = _try_get_db()
    if db is None or not hasattr(db, "set_active_command_permission_profile"):
        return
    try:
        db.set_active_command_permission_profile(None)
    except Exception:
        logger.warning("failed to clear active permission profile", exc_info=True)


# Sentinel ids for synthetic UI/CLI presets (not stored as named profiles).
PROFILE_YAML = "__yaml__"
PROFILE_BLANK = "__blank__"


def apply_yaml_baseline() -> None:
    """Remove runtime overrides so effective policy is YAML-only; clear active id."""
    from agentforge.tools.command_policy_store import clear_runtime_override

    clear_runtime_override(None)
    clear_active_profile()


def apply_blank_slate() -> None:
    """Install empty confirm policies as runtime overrides (start a new profile).

    Unlike YAML baseline, this forces empty lists even when config YAML has items.
    """
    empty = CommandPolicy(mode="confirm")
    set_runtime_override("shell", empty)
    set_runtime_override("ssh", empty)
    db = _try_get_db()
    if db is not None and hasattr(db, "set_active_command_permission_profile"):
        try:
            # Marker so the UI can reselect "blank" after reload.
            db.set_active_command_permission_profile(PROFILE_BLANK)
        except Exception:
            logger.warning("failed to mark blank-slate profile active", exc_info=True)
