"""REST API for command permission management.

Mounted under ``/api/permissions``. Exposes YAML baseline, SQLite runtime
overrides, named profiles, merged effective policy, and validate for the Web UI.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from agentforge.tools.command_permission_profiles import (
    PROFILE_BLANK,
    PROFILE_YAML,
    apply_blank_slate,
    apply_profile,
    apply_yaml_baseline,
    clear_active_profile,
    delete_user_profile,
    get_active_profile_id,
    get_profile,
    list_profiles,
    save_user_profile,
)
from agentforge.tools.command_policy import (
    CommandPolicy,
    PolicyMode,
    ToolName,
    evaluate,
    load_yaml_policy,
    merge_policies,
)
from agentforge.tools.command_policy_store import (
    clear_runtime_override,
    get_effective_policy,
    get_runtime_override,
    set_runtime_override,
)

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

_TOOLS: tuple[ToolName, ...] = ("shell", "ssh")


class PolicyPayload(BaseModel):
    mode: PolicyMode = "confirm"
    allowed_commands: list[str] = Field(default_factory=list)
    allowed_patterns: list[str] = Field(default_factory=list)
    blocked_patterns: list[str] = Field(default_factory=list)


class OverridesPayload(BaseModel):
    shell: PolicyPayload | None = None
    ssh: PolicyPayload | None = None


class ValidateRequest(BaseModel):
    tool: ToolName
    command: str
    # Optional draft override from the Web UI — merged with YAML like a saved override.
    policy: PolicyPayload | None = None


class SaveProfileRequest(BaseModel):
    """Create/update a user profile. Set ``from_current_overrides`` to snapshot live policy."""

    description: str = ""
    shell: PolicyPayload | None = None
    ssh: PolicyPayload | None = None
    from_current_overrides: bool = False


def _policy_to_dict(policy: CommandPolicy) -> dict:
    return {
        "mode": policy.mode,
        "allowed_commands": list(policy.allowed_commands),
        "allowed_patterns": list(policy.allowed_patterns),
        "blocked_patterns": list(policy.blocked_patterns),
    }


def _payload_to_policy(payload: PolicyPayload) -> CommandPolicy:
    return CommandPolicy(
        mode=payload.mode,
        allowed_commands=tuple(payload.allowed_commands),
        allowed_patterns=tuple(payload.allowed_patterns),
        blocked_patterns=tuple(payload.blocked_patterns),
    )


def _payload_to_dict(payload: PolicyPayload | None) -> dict | None:
    if payload is None:
        return None
    return _policy_to_dict(_payload_to_policy(payload))


def _tool_policy_bundle(tool: ToolName) -> dict:
    yaml_policy = load_yaml_policy(tool)
    override = get_runtime_override(tool)
    effective = get_effective_policy(tool)
    return {
        "yaml": _policy_to_dict(yaml_policy),
        "override": _policy_to_dict(override) if override is not None else None,
        "effective": _policy_to_dict(effective),
    }


@router.get("/commands")
def get_command_policies() -> dict:
    """Return YAML baseline, runtime override, and effective policy per tool."""
    return {tool: _tool_policy_bundle(tool) for tool in _TOOLS}


@router.get("/commands/overrides")
def get_command_overrides() -> dict:
    """Return runtime overrides only (null when unset)."""
    result: dict[str, dict | None] = {}
    for tool in _TOOLS:
        override = get_runtime_override(tool)
        result[tool] = _policy_to_dict(override) if override is not None else None
    return result


@router.put("/commands/overrides")
def put_command_overrides(body: OverridesPayload) -> dict:
    """Upsert runtime overrides for shell and/or ssh."""
    if body.shell is not None:
        set_runtime_override("shell", _payload_to_policy(body.shell))
    if body.ssh is not None:
        set_runtime_override("ssh", _payload_to_policy(body.ssh))
    return {"ok": True}


@router.delete("/commands/overrides")
def delete_command_overrides(
    tool: Literal["shell", "ssh"] | None = Query(default=None),
) -> dict:
    """Delete one override (``?tool=shell``) or all when *tool* is omitted."""
    deleted = clear_runtime_override(tool)
    return {"deleted": deleted}


@router.post("/commands/validate")
def validate_command(body: ValidateRequest) -> dict:
    """Evaluate a command against the effective policy for *tool*."""
    if body.policy is not None:
        policy = merge_policies(load_yaml_policy(body.tool), _payload_to_policy(body.policy))
    else:
        policy = get_effective_policy(body.tool)
    verdict = evaluate(body.tool, body.command, policy)
    return {
        "action": verdict.action,
        "reason": verdict.reason,
        "source": verdict.source,
    }


@router.get("/profiles")
def get_permission_profiles() -> dict:
    """List built-in (YAML) and user-saved permission profiles."""
    profiles = [p.to_dict() for p in list_profiles()]
    return {
        "profiles": profiles,
        "active_profile_id": get_active_profile_id(),
    }


@router.delete("/profiles/active")
def clear_active_permission_profile() -> dict:
    """Clear the active-profile marker; runtime overrides are left unchanged."""
    clear_active_profile()
    return {"ok": True, "active_profile_id": None}


@router.get("/profiles/{profile_id}")
def get_permission_profile(profile_id: str) -> dict:
    if profile_id in (PROFILE_YAML, PROFILE_BLANK):
        raise HTTPException(404, f"synthetic preset: {profile_id}")
    profile = get_profile(profile_id)
    if profile is None:
        raise HTTPException(404, f"unknown profile: {profile_id}")
    return profile.to_dict()


@router.post("/profiles/{profile_id}/apply")
def apply_permission_profile(profile_id: str) -> dict:
    """Apply a profile as the live runtime override for shell/ssh."""
    if profile_id == PROFILE_YAML:
        apply_yaml_baseline()
        return {
            "ok": True,
            "applied": {"id": PROFILE_YAML, "description": "YAML baseline", "source": "yaml"},
            "active_profile_id": None,
            "effective": {tool: _tool_policy_bundle(tool) for tool in _TOOLS},
        }
    if profile_id == PROFILE_BLANK:
        apply_blank_slate()
        return {
            "ok": True,
            "applied": {
                "id": PROFILE_BLANK,
                "description": "Blank slate",
                "source": "user",
            },
            "active_profile_id": PROFILE_BLANK,
            "effective": {tool: _tool_policy_bundle(tool) for tool in _TOOLS},
        }
    try:
        profile = apply_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "ok": True,
        "applied": profile.to_dict(),
        "active_profile_id": get_active_profile_id(),
        "effective": {tool: _tool_policy_bundle(tool) for tool in _TOOLS},
    }


@router.put("/profiles/{profile_id}")
def put_permission_profile(profile_id: str, body: SaveProfileRequest) -> dict:
    """Create or update a user-saved profile (not YAML builtins)."""
    try:
        profile = save_user_profile(
            profile_id,
            description=body.description,
            shell=_payload_to_dict(body.shell),
            ssh=_payload_to_dict(body.ssh),
            from_current_overrides=body.from_current_overrides,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "profile": profile.to_dict()}


@router.delete("/profiles/{profile_id}")
def delete_permission_profile(profile_id: str) -> dict:
    """Delete a user-saved profile. Built-in YAML profiles return 400."""
    try:
        deleted = delete_user_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, f"unknown user profile: {profile_id}")
    return {"deleted": 1}
