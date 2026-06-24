"""YAML-driven tool routing — the source of truth for tool/role/queue mapping.

Loads ``tool_routing.yaml`` from the agentforge root once at import time
and exposes lookups used by the registry, the agent loop, and the SAQ
dispatcher.

Glob patterns are evaluated in file order (first match wins). The
``default_role`` catches anything not matched by an explicit rule.

Decorator drift: tools may still carry ``@tool(locality=...)``. When more than
one role is configured, the registry calls :func:`check_decorator_drift` once at
startup to log mismatches between the YAML and the decorator value. On a
single-host deployment the check is skipped (localities are moot).
"""

from __future__ import annotations

import fnmatch
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Optional decorator-value -> role aliases for @tool(locality=...). Empty by
# default: a locality passes through as its own role name. Populate this if your
# deployment uses different role names than the decorator values.
_LEGACY_LOCALITY_MAP: dict[str, str] = {}

_DEFAULT_CONFIG_FILENAME = "tool_routing.yaml"


def _config_path() -> Path:
    """Resolve the YAML path. ``AGENTFORGE_TOOL_ROUTING`` env var overrides."""
    override = os.environ.get("AGENTFORGE_TOOL_ROUTING")
    if override:
        return Path(override)
    # Source checkout: agentforge/tools/routing.py -> <repo root>/tool_routing.yaml
    pkg_relative = Path(__file__).resolve().parents[2] / _DEFAULT_CONFIG_FILENAME
    if pkg_relative.exists():
        return pkg_relative
    # Pip-installed (parents[2] is site-packages): fall back to the working dir,
    # where the service deploy keeps tool_routing.yaml next to config.yaml.
    cwd_path = Path.cwd() / _DEFAULT_CONFIG_FILENAME
    if cwd_path.exists():
        return cwd_path
    return pkg_relative


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        logger.warning("tool_routing.yaml not found at %s — using empty defaults", path)
        return {"default_role": "local", "roles": {}, "rules": [], "modes": {}}

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    data.setdefault("default_role", "local")
    data.setdefault("roles", {})
    data.setdefault("rules", [])
    data.setdefault("modes", {})

    # Private overlay (gitignored) — lets you route private plugin tools without
    # naming them in the published file. Its rules take precedence (first match
    # wins), its roles/modes are merged in.
    local_path = path.parent / "tool_routing.local.yaml"
    if local_path.exists():
        try:
            with local_path.open("r", encoding="utf-8") as lf:
                local = yaml.safe_load(lf) or {}
            data["roles"] = {**data["roles"], **(local.get("roles") or {})}
            data["modes"] = {**data["modes"], **(local.get("modes") or {})}
            data["rules"] = list(local.get("rules") or []) + data["rules"]
        except Exception:
            logger.warning("Failed to load tool_routing.local.yaml", exc_info=True)

    # Validate rules reference known roles (catches typos on startup).
    known_roles = set(data["roles"].keys())
    for idx, rule in enumerate(data["rules"]):
        role = rule.get("role")
        if role not in known_roles:
            logger.error(
                "tool_routing.yaml rule #%d references unknown role %r (known: %s)",
                idx,
                role,
                sorted(known_roles),
            )
    if data["default_role"] not in known_roles:
        logger.error(
            "tool_routing.yaml default_role %r is not in roles (known: %s)",
            data["default_role"],
            sorted(known_roles),
        )

    return data


def reload() -> None:
    """Drop the cached config so the next lookup re-reads the file. Test-only."""
    _load.cache_clear()


def get_role_for_tool(tool_name: str) -> str:
    """Return the role the named tool should run on. Falls back to ``default_role``."""
    cfg = _load()
    for rule in cfg["rules"]:
        patterns = rule.get("tools") or []
        for pattern in patterns:
            if fnmatch.fnmatchcase(tool_name, pattern):
                return rule["role"]
    return cfg["default_role"]


def get_role_for_mode(mode: str) -> str | None:
    """Return the role pinned for *mode*, or None if the mode is unrouted."""
    return _load()["modes"].get(mode)


def get_queue_for_role(role: str) -> str:
    """Return the SAQ queue name for *role*. Raises ``KeyError`` if unknown."""
    roles = _load()["roles"]
    if role not in roles:
        raise KeyError(f"Unknown role {role!r}. Known: {sorted(roles)}")
    return roles[role]["queue"]


def available_roles() -> list[str]:
    return sorted(_load()["roles"].keys())


def default_role() -> str:
    return _load()["default_role"]


_VALID_DISPATCH_MODES = {"split", "in_process"}


def dispatch_mode() -> str:
    """How the agent loop dispatches tool calls across roles.

    - ``in_process`` — run every tool in the current worker (no cross-role SAQ
      dispatch). The single-host / dev default: one box runs the whole stack and
      no separate native worker is needed.
    - ``split`` — route each tool to its role's queue (multi-host). Needs a
      worker running for every role a tool can route to.

    Resolution order: ``AGENTFORGE_DISPATCH_MODE`` env (lets a deployed worker
    force ``split`` without editing the file) -> ``dispatch.mode`` in
    tool_routing.yaml -> ``split`` (backward-compatible when unset).
    """
    raw = os.environ.get("AGENTFORGE_DISPATCH_MODE")
    if raw is None:
        raw = _load().get("dispatch", {}).get("mode")
    if raw is None:
        return "split"
    mode = str(raw).strip().lower()
    if mode not in _VALID_DISPATCH_MODES:
        logger.warning(
            "Unknown dispatch mode %r (expected %s) — falling back to 'split'",
            raw,
            sorted(_VALID_DISPATCH_MODES),
        )
        return "split"
    return mode


def _dispatch_float(key: str, env: str, default: float) -> float:
    raw = os.environ.get(env)
    if raw is None:
        raw = _load().get("dispatch", {}).get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid dispatch.%s value %r — using %s", key, raw, default)
        return default


def tool_dispatch_timeout() -> float:
    """Seconds to wait for a cross-role tool job before SAQ times out.

    ``dispatch.tool_timeout_seconds`` in tool_routing.yaml (env
    ``AGENTFORGE_SAQ_TOOL_TIMEOUT``). Matches shell.py's upper bound so
    long installs (brew/npm/pip) don't get killed mid-run.
    """
    return _dispatch_float("tool_timeout_seconds", "AGENTFORGE_SAQ_TOOL_TIMEOUT", 900.0)


def agent_dispatch_timeout() -> float:
    """Seconds to wait for an agent job (LLM + tool loop + RAG) before SAQ times out.

    ``dispatch.agent_timeout_seconds`` in tool_routing.yaml (env
    ``AGENTFORGE_SAQ_AGENT_TIMEOUT``). Discovery / research runs occasionally
    take minutes; the Stop button still cancels inside this window.
    """
    return _dispatch_float("agent_timeout_seconds", "AGENTFORGE_SAQ_AGENT_TIMEOUT", 900.0)


def my_role() -> str:
    """Resolve this worker's role from ``AGENTFORGE_WORKER_ROLE``, else ``default_role``."""
    return os.environ.get("AGENTFORGE_WORKER_ROLE") or default_role()


def check_decorator_drift(tool_localities: dict[str, str]) -> list[tuple[str, str, str]]:
    """Compare ``@tool(locality=...)`` values against the YAML.

    Returns a list of ``(tool_name, decorator_role, yaml_role)`` triples for
    every tool whose decorator role disagrees with the YAML routing. Logs a
    warning per mismatch.

    *tool_localities* should be the registry's
    ``{tool_name: decorator_locality_value}`` mapping. The decorator value is
    translated through ``_LEGACY_LOCALITY_MAP`` before comparison.

    Skipped on single-host deployments (<=1 role configured), where locality
    decorators have no effect.
    """
    if len(_load()["roles"]) <= 1:
        return []

    drift: list[tuple[str, str, str]] = []
    for name, decorator_value in tool_localities.items():
        decorator_role = _LEGACY_LOCALITY_MAP.get(decorator_value, decorator_value)
        yaml_role = get_role_for_tool(name)
        if decorator_role != yaml_role:
            drift.append((name, decorator_role, yaml_role))
            logger.warning(
                "tool routing drift: %r decorator says %s, YAML routes to %s",
                name,
                decorator_role,
                yaml_role,
            )
    return drift
