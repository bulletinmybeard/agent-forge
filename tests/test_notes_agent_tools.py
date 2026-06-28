"""@notes custom agent must include Apple Reminders tools."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

NOTES_REMINDERS_TOOLS = [
    "reminders_status",
    "reminders_lists",
    "reminders_show",
    "reminders_find",
    "reminders_add",
    "reminders_edit",
    "reminders_complete",
    "reminders_delete",
]


def _load_merged_agents() -> dict:
    """Mirror SearchRuntime merge: example (or yaml) then local overlay."""
    example_path = ROOT / "custom_agents.example.yaml"
    with open(example_path) as f:
        agents = dict((yaml.safe_load(f) or {}).get("agents", {}))

    local_path = ROOT / "custom_agents.local.yaml"
    if local_path.exists():
        with open(local_path) as f:
            agents.update((yaml.safe_load(f) or {}).get("agents", {}))

    return agents


def test_notes_agent_includes_reminders_tools():
    agents = _load_merged_agents()
    notes = agents.get("notes")
    assert isinstance(notes, dict), "notes agent must be defined in custom_agents"

    tools = notes.get("tools")
    assert isinstance(tools, list), "notes agent must declare an explicit tools allowlist"

    missing = [name for name in NOTES_REMINDERS_TOOLS if name not in tools]
    assert not missing, f"@notes is missing reminders tools: {missing}"
