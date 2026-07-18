"""REST API for command permission management.

Mounted under ``/api/permissions``. Exposes YAML baseline, SQLite runtime
overrides, merged effective policy, and a validate endpoint for the Web UI.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

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
