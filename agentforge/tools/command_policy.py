"""Segment-aware command permission evaluator for shell and ssh tools."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Literal

from agentforge.config import get_config

# Share shell parsing semantics with readonly_guard (segment splitting).
from agentforge.tools.readonly_guard import _split_segments

logger = logging.getLogger(__name__)

PolicyMode = Literal["confirm", "allowlist", "denylist"]
PolicyAction = Literal["allow", "deny", "confirm"]
ToolName = Literal["shell", "ssh"]


@dataclass(frozen=True)
class CommandPolicy:
    mode: PolicyMode = "confirm"
    allowed_commands: tuple[str, ...] = ()
    allowed_patterns: tuple[str, ...] = ()
    blocked_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyVerdict:
    action: PolicyAction
    reason: str = ""
    source: str = ""


def _segment_base_command(segment: str) -> str:
    parts = segment.strip().split()
    idx = 0
    while idx < len(parts) and "=" in parts[idx] and not parts[idx].startswith("-"):
        idx += 1
    while idx < len(parts) and parts[idx] in ("sudo", "command", "nice", "ionice", "time"):
        idx += 1
    if idx >= len(parts):
        return ""
    return os.path.basename(parts[idx].rsplit("/", 1)[-1])


def _pattern_matches(pattern: str, segment: str) -> bool:
    try:
        return bool(re.search(pattern, segment, re.IGNORECASE))
    except re.error:
        logger.warning("Invalid regex pattern in command policy: %s", pattern)
        return False


def _segment_passes_allowlist(segment: str, policy: CommandPolicy) -> bool:
    base = _segment_base_command(segment)
    if policy.allowed_commands and base in policy.allowed_commands:
        return True
    for pattern in policy.allowed_patterns:
        if _pattern_matches(pattern, segment):
            return True
    return False


def _find_blocked_segment(command: str, policy: CommandPolicy) -> tuple[str, str] | None:
    for segment in _split_segments(command):
        for pattern in policy.blocked_patterns:
            if _pattern_matches(pattern, segment):
                return segment, pattern
    return None


def evaluate(tool: ToolName, command: str, policy: CommandPolicy) -> PolicyVerdict:
    if not command.strip():
        return PolicyVerdict(action="allow", reason="empty", source="noop")

    if policy.mode == "confirm":
        blocked = _find_blocked_segment(command, policy)
        if blocked is not None:
            _segment, pattern = blocked
            return PolicyVerdict(
                action="deny",
                reason=f"Command matches blocked pattern ({pattern})",
                source="policy_blocked_pattern",
            )
        return PolicyVerdict(action="confirm", reason="", source="policy_confirm")

    if policy.mode == "denylist":
        blocked = _find_blocked_segment(command, policy)
        if blocked is not None:
            _segment, pattern = blocked
            return PolicyVerdict(
                action="deny",
                reason=f"Command segment matches blocked pattern ({pattern})",
                source="policy_denylist",
            )
        return PolicyVerdict(action="allow", reason="", source="policy_denylist")

    # allowlist mode
    for segment in _split_segments(command):
        if not _segment_passes_allowlist(segment, policy):
            base = _segment_base_command(segment)
            return PolicyVerdict(
                action="deny",
                reason=f"Command '{base}' is not allowed",
                source="policy_allowlist",
            )
    return PolicyVerdict(action="allow", reason="", source="policy_allowlist")


def load_yaml_policy(tool: ToolName) -> CommandPolicy:
    try:
        cfg = get_config()
        tool_cfg = cfg._raw.get("tools", {}).get(tool, {})
        perms = tool_cfg.get("permissions", {})

        mode = perms.get("mode", "confirm")
        allowed_commands = tuple(perms.get("allowed_commands") or ())
        allowed_patterns = tuple(perms.get("allowed_patterns") or ())
        blocked_patterns = tuple(perms.get("blocked_patterns") or ())

        if not allowed_commands:
            legacy_allowed = tool_cfg.get("allowed_commands") or []
            if legacy_allowed:
                allowed_commands = tuple(legacy_allowed)

        if not blocked_patterns:
            legacy_blocked = tool_cfg.get("blocked_patterns") or []
            if legacy_blocked:
                blocked_patterns = tuple(legacy_blocked)

        return CommandPolicy(
            mode=mode,
            allowed_commands=allowed_commands,
            allowed_patterns=allowed_patterns,
            blocked_patterns=blocked_patterns,
        )
    except Exception:
        logger.warning("Failed to load YAML command policy for %s; using defaults", tool)
        return CommandPolicy()


def merge_policies(base: CommandPolicy, override: CommandPolicy | None) -> CommandPolicy:
    """Merge YAML baseline with a runtime override."""
    if override is None:
        return base
    return CommandPolicy(
        mode=override.mode,
        allowed_commands=override.allowed_commands,
        allowed_patterns=override.allowed_patterns,
        blocked_patterns=override.blocked_patterns,
    )
