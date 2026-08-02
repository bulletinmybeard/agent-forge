"""Deterministic template fill: command outputs -> Jinja2 -> structured scaffold.

The render context is intentionally minimal and matches what the shell tool
actually returns (a single merged output string per command — no separate
stdout/stderr/exit_code). Per command id:

    commands.<id>.output   the (stripped, possibly truncated) command output
    commands.<id>.ok       False if the command errored, else True
    commands.<id>.command  the literal command string
    commands.<id>.cwd      working directory

jinja2 is imported at module top — this module is only used on the discovery
path where the dependency is present; loader/models stay jinja-free.
"""

from __future__ import annotations

import logging

from jinja2 import ChainableUndefined, Environment

from .models import Playbook

logger = logging.getLogger(__name__)

# ChainableUndefined so a template referencing a missing command id renders empty
# instead of raising — a template typo must not crash a live discovery run.
_ENV = Environment(
    undefined=ChainableUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def build_context(playbook: Playbook, outputs_by_command: dict[str, str]) -> dict:
    """Map each playbook command's output to an id-keyed render context.

    ``outputs_by_command`` maps the literal command string -> its output (the
    shape produced by DiscoveryRunner's ``raw_outputs`` entries). Commands with
    no captured output render as empty + ``ok=False``.
    """
    commands: dict[str, dict] = {}
    for cmd in playbook.commands:
        output = outputs_by_command.get(cmd.command)
        ok = output is not None and not output.lstrip().startswith("Error:")
        commands[cmd.id] = {
            "output": (output or "").strip(),
            "ok": ok,
            "command": cmd.command,
            "cwd": cmd.cwd,
        }
    return {
        "commands": commands,
        "playbook": {"id": playbook.id, "description": playbook.description},
    }


def render_playbook(playbook: Playbook, context: dict) -> str:
    """Render the playbook's template with the given context.

    Returns an empty string on any template error (logged) so synthesis falls
    back to the raw findings rather than failing the run.
    """
    try:
        template = _ENV.from_string(playbook.template_text)
        return template.render(**context).strip()
    except Exception:
        logger.warning("Failed to render playbook '%s' template", playbook.id, exc_info=True)
        return ""
