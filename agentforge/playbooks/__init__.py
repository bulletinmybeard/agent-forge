"""Playbooks — curated command-combinations + Jinja2 output templates.

A playbook pairs a case-specific set of diagnostic commands with a Jinja2
template that pre-structures their output. Playbooks are retrieved semantically
by the user's query and feed the DiscoveryRunner: the commands seed the
investigation, and the rendered template feeds the synthesis step.

Distinct from Skills (prose guidance injected into the system prompt) — a
playbook is an executable gather-and-format scaffold, not reasoning guidance.

Public surface is intentionally small; heavy deps (jinja2, qdrant, ollama) are
imported lazily so `loader`/`models` stay import-safe without them.
"""

from __future__ import annotations

from .models import Playbook, PlaybookCommand, PlaybookLibrary

__all__ = ["Playbook", "PlaybookCommand", "PlaybookLibrary"]
