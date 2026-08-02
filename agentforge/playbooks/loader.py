"""Load the playbook registry (playbooks.yaml) + resolve template files.

Mirrors the skills.yaml + markdown/skills/*.md idiom: a top-level YAML registry
whose entries point at a Jinja template file under markdown/playbooks/. Files are
the source of truth; the Qdrant index (see index.py) is just a search layer.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .models import SYNTHESIS_MODES, Playbook, PlaybookCommand, PlaybookLibrary

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.55


def load_library(registry_path: str | Path, base_dir: str | Path | None = None) -> PlaybookLibrary:
    """Read playbooks.yaml and return a validated PlaybookLibrary.

    ``base_dir`` roots the relative ``template_file`` paths; defaults to the
    registry's own directory. Invalid entries (missing id/commands/template) are
    skipped with a warning rather than aborting the whole load.
    """
    registry_path = Path(registry_path)
    if not registry_path.exists():
        logger.info("playbooks.yaml not found at %s — playbooks disabled", registry_path)
        return PlaybookLibrary()

    base = Path(base_dir) if base_dir else registry_path.parent

    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse %s: %s — playbooks disabled", registry_path, exc)
        return PlaybookLibrary()

    default_threshold = float(raw.get("score_threshold", DEFAULT_THRESHOLD))
    entries: dict = raw.get("playbooks", {}) or {}

    library = PlaybookLibrary(default_threshold=default_threshold)
    for pb_id, cfg in entries.items():
        pb = _build_playbook(pb_id, cfg or {}, base)
        if pb is not None:
            library.playbooks[pb.id] = pb

    logger.info("Loaded %d playbook(s) from %s", len(library), registry_path)
    return library


def _build_playbook(pb_id: str, cfg: dict, base: Path) -> Playbook | None:
    description = str(cfg.get("description", "")).strip()
    if not description:
        logger.warning("Playbook '%s' has no description — skipped", pb_id)
        return None

    commands = _parse_commands(pb_id, cfg.get("commands", []) or [])
    if not commands:
        logger.warning("Playbook '%s' has no valid commands — skipped", pb_id)
        return None

    template_path = str(cfg.get("template_file", "")).strip()
    if not template_path:
        logger.warning("Playbook '%s' has no template_file — skipped", pb_id)
        return None

    resolved = base / template_path
    if not resolved.exists():
        logger.warning("Playbook '%s' template not found: %s — skipped", pb_id, resolved)
        return None
    template_text = resolved.read_text(encoding="utf-8")

    threshold = cfg.get("score_threshold")
    synthesis = str(cfg.get("synthesis", "diagnostic")).strip().lower()
    if synthesis not in SYNTHESIS_MODES:
        logger.warning("Playbook '%s': unknown synthesis '%s' — using 'diagnostic'", pb_id, synthesis)
        synthesis = "diagnostic"
    return Playbook(
        id=pb_id,
        description=description,
        trigger_examples=[str(e) for e in (cfg.get("trigger_examples", []) or [])],
        commands=commands,
        template_path=template_path,
        template_text=template_text,
        score_threshold=float(threshold) if threshold is not None else None,
        synthesis=synthesis,
    )


def _parse_commands(pb_id: str, raw_commands: list) -> list[PlaybookCommand]:
    commands: list[PlaybookCommand] = []
    seen: set[str] = set()
    for entry in raw_commands:
        if not isinstance(entry, dict):
            logger.warning("Playbook '%s': command entry is not a mapping — skipped", pb_id)
            continue
        cmd_id = str(entry.get("id", "")).strip()
        command = str(entry.get("command", "")).strip()
        if not cmd_id or not command:
            logger.warning("Playbook '%s': command missing id/command — skipped", pb_id)
            continue
        if cmd_id in seen:
            logger.warning("Playbook '%s': duplicate command id '%s' — skipped", pb_id, cmd_id)
            continue
        seen.add(cmd_id)
        commands.append(PlaybookCommand(id=cmd_id, command=command, cwd=str(entry.get("cwd", "/tmp"))))
    return commands
