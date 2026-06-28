"""Tool modules — registry, decorator, and categorized tool collections.

Re-exports :class:`ToolRegistry` and the :func:`tool` decorator from
:mod:`agentforge.tools.registry`.

Usage::

    from agentforge.tools import ToolRegistry, register_all_tools

    registry = ToolRegistry()
    count = register_all_tools(registry)
    print(f"Registered {count} tools")

``register_all_tools`` registers the built-in (generic) toolset, optional
credential-gated tools (see :func:`register_optional_tools`), and then
loads any third-party tool plugins (see :func:`load_plugin_tools`). To get
only the built-ins, call :func:`register_core_tools` directly.
"""

from __future__ import annotations

import os
from importlib import import_module
from importlib import metadata as _metadata

from .registry import ToolRegistry, _decorated_tools, tool  # noqa: F401

# Entry-point group that external packages advertise their tool registrars
# under. Each entry point resolves to a ``register(registry) -> int`` callable.
PLUGIN_ENTRY_POINT_GROUP = "agentforge.tools"
# Comma-separated ``module:function`` specs, an alternative to entry points
# for ad-hoc / in-tree plugins. e.g., "myapp.tools:register_my_tools".
PLUGIN_ENV_VAR = "AGENTFORGE_TOOL_PLUGINS"


def register_core_tools(registry: ToolRegistry) -> int:
    """Register the built-in (generic) toolset. Returns the count registered."""
    from .archive_tools import register_archive_tools
    from .audio_tools import register_audio_tools
    from .cli_tools import register_cli_tools
    from .code_edit import register_code_edit_tools
    from .code_quality_tools import register_code_quality_tools
    from .data_tools import register_data_tools
    from .docker import register_docker_tools
    from .filesystem import register_filesystem_tools
    from .git_tools import register_git_tools
    from .icon_generator import register_icon_generator_tools
    from .log_analysis import register_log_analysis_tools
    from .media_tools import register_media_tools
    from .netdiag_tools import register_netdiag_tools
    from .network_tools import register_network_tools
    from .notify import register_notify_tools
    from .qdrant_tools import register_qdrant_tools
    from .redis_tools import register_redis_tools
    from .reminders_tools import register_reminders_tools
    from .shell import register_shell_tools
    from .ssh_tools import register_ssh_tools
    from .system import register_system_tools
    from .testing_tools import register_testing_tools
    from .tmdb import register_tmdb_tools
    from .web_render import register_web_render_tools
    from .web_search import register_web_search_tools

    count = 0
    count += register_filesystem_tools(registry)
    count += register_system_tools(registry)
    count += register_docker_tools(registry)
    count += register_cli_tools(registry)
    count += register_ssh_tools(registry)
    count += register_archive_tools(registry)
    count += register_git_tools(registry)
    count += register_network_tools(registry)
    count += register_netdiag_tools(registry)
    count += register_shell_tools(registry)
    count += register_code_edit_tools(registry)
    count += register_web_search_tools(registry)
    count += register_web_render_tools(registry)
    count += register_log_analysis_tools(registry)
    count += register_media_tools(registry)
    count += register_audio_tools(registry)
    count += register_icon_generator_tools(registry)
    count += register_notify_tools(registry)
    count += register_reminders_tools(registry)
    count += register_code_quality_tools(registry)
    count += register_data_tools(registry)
    count += register_testing_tools(registry)
    count += register_qdrant_tools(registry)
    count += register_redis_tools(registry)
    count += register_tmdb_tools(registry)
    return count


def register_optional_tools(registry: ToolRegistry) -> int:
    """Register built-in tools that need credentials (Put.io, Premiumize, ...).

    Each submodule registers only when its env vars are set. Returns the count
    registered.
    """
    from .cloud_tools import register_cloud_tools

    return register_cloud_tools(registry)


def load_plugin_tools(registry: ToolRegistry) -> int:
    """Discover and register third-party tool plugins.

    Two sources, both optional:

    - Python entry points in the ``agentforge.tools`` group. A package exposes
      one via ``[project.entry-points."agentforge.tools"]`` pointing at a
      ``register(registry) -> int`` callable.
    - The ``AGENTFORGE_TOOL_PLUGINS`` env var: a comma-separated list of
      ``module:function`` specs resolved at runtime.

    A failing plugin is logged and skipped — it never breaks the core toolset.
    Returns the total number of tools registered by all plugins.
    """
    registrars: list = []

    try:
        eps = _metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
    except TypeError:  # Python <3.10 select-by-group signature
        eps = _metadata.entry_points().get(PLUGIN_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    for ep in eps:
        try:
            registrars.append(ep.load())
        except Exception:  # noqa: BLE001 — a bad plugin must not break startup
            continue

    for spec in (s.strip() for s in os.getenv(PLUGIN_ENV_VAR, "").split(",")):
        if not spec:
            continue
        module_name, _, attr = spec.partition(":")
        if not module_name or not attr:
            continue
        try:
            registrars.append(getattr(import_module(module_name), attr))
        except Exception:  # noqa: BLE001
            continue

    count = 0
    for register in registrars:
        try:
            count += int(register(registry) or 0)
        except Exception:  # noqa: BLE001
            continue
    return count


def register_all_tools(registry: ToolRegistry) -> int:
    """Register built-in tools + any installed tool plugins.

    Returns the total number of tools registered.
    """
    count = register_core_tools(registry)
    count += register_optional_tools(registry)
    count += load_plugin_tools(registry)

    # One-shot drift report — warns when a tool's @tool(locality=...) decorator
    # disagrees with tool_routing.yaml. Never raises.
    try:
        registry.check_routing_drift()
    except Exception:  # noqa: BLE001
        pass

    return count
