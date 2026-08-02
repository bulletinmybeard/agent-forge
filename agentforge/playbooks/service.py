"""Facade the discovery flow calls: cached library/index + orchestration helpers.

Keeps the ws_endpoint integration to a few calls:

    seed = playbook_service.match_and_seed(query)   # None when nothing matches
    ... run discovery with seed.area seeded, max_rounds=1 ...
    scaffold = playbook_service.render(seed.playbook, finding)

All external failures (Qdrant down, embed model cold, jinja error) degrade to
"no playbook" / empty scaffold so discovery always falls back to its LLM path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .index import PlaybookIndex
from .loader import load_library
from .models import Playbook, PlaybookLibrary

logger = logging.getLogger(__name__)

SEED_AREA_PREFIX = "playbook__"

_library: PlaybookLibrary | None = None
_index: PlaybookIndex | None = None


@dataclass
class PlaybookSeed:
    """A matched playbook plus the InvestigationArea seeded from its commands."""

    playbook: Playbook
    area: Any  # agentforge.discovery.InvestigationArea (imported lazily)
    score: float


def _registry_path() -> Path:
    """Resolve playbooks.yaml — configured path, else ./playbooks.yaml (CWD)."""
    path = "playbooks.yaml"
    try:
        from app.config import settings

        path = settings.playbooks.registry_path or path
    except Exception:
        pass
    p = Path(path)
    return p if p.is_absolute() else Path(os.getcwd()) / p


def _enabled() -> bool:
    try:
        from app.config import settings

        return bool(settings.playbooks.enabled)
    except Exception:
        return True


def get_library(*, reload: bool = False) -> PlaybookLibrary:
    global _library
    if _library is None or reload:
        _library = load_library(_registry_path())
    return _library


def get_index() -> PlaybookIndex:
    global _index
    if _index is None:
        _index = PlaybookIndex()
    return _index


def reset_cache() -> None:
    """Drop cached library + index (used by the reindex CLI and tests)."""
    global _library, _index
    _library = None
    _index = None


def match(query: str) -> tuple[Playbook, float] | None:
    """Best-match playbook for a query, gated by its score threshold."""
    if not _enabled():
        return None
    library = get_library()
    if len(library) == 0:
        return None
    try:
        result = get_index().search_one(query)
    except Exception:
        logger.debug("Playbook retrieval failed — falling back to LLM scoping", exc_info=True)
        return None
    if result is None:
        return None
    pid, score = result
    pb = library.get(pid)
    if pb is None:
        return None
    if score < library.threshold_for(pb):
        logger.info("Playbook '%s' below threshold (%.3f) — no match", pid, score)
        return None
    logger.info("Playbook matched: '%s' (score=%.3f)", pid, score)
    return pb, score


def match_and_seed(query: str) -> PlaybookSeed | None:
    """Match a playbook and build the seeded InvestigationArea from its commands."""
    matched = match(query)
    if matched is None:
        return None
    pb, score = matched

    from agentforge.discovery import InvestigationArea

    area = InvestigationArea(
        id=f"{SEED_AREA_PREFIX}{pb.id}",
        label=f"Playbook: {pb.id}",
        description=pb.description,
        probe_commands=[{"command": c.command, "cwd": c.cwd} for c in pb.commands],
        hints="Curated playbook commands — do not substitute.",
        priority=1,
        skip_analysis=True,  # the rendered template is the structured output
    )
    return PlaybookSeed(playbook=pb, area=area, score=score)


def render(playbook: Playbook, finding: Any) -> str:
    """Render the playbook template from a discovery AreaFinding's raw outputs."""
    outputs_by_command = {
        entry.get("command", ""): entry.get("output", "") for entry in getattr(finding, "raw_outputs", [])
    }
    from .render import build_context, render_playbook

    context = build_context(playbook, outputs_by_command)
    return render_playbook(playbook, context)


def reindex() -> dict[str, int]:
    """(Re)build the Qdrant index from the current registry."""
    library = get_library(reload=True)
    return get_index().reindex(library)
