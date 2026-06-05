"""Chunk writer for docs chunks — writes JSON files to disk.

Output structure:
    chunks/docs/{source_name}/v{version}/
        _summary.json
        commands/
            {command-slug}.json
            {command-slug}__options.json   (overflow)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from chunking.models import CommandChunk, CommandOptionsChunk, DocsSummaryChunk

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a command name to a filesystem-safe slug.

    "git commit" → "git-commit"
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name).strip("-")


def write_docs_chunks(
    output_dir: str | Path,
    source_name: str,
    version: str,
    summary: DocsSummaryChunk,
    commands: list[CommandChunk],
    options_overflow: list[CommandOptionsChunk] | None = None,
) -> Path:
    """Write docs chunks to disk as JSON files."""
    version_safe = re.sub(r"[^a-zA-Z0-9._-]", "_", version)
    if not version_safe.startswith("v"):
        version_safe = f"v{version_safe}"

    result_dir = Path(output_dir) / "docs" / source_name / version_safe
    commands_dir = result_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    # Write summary
    summary_path = result_dir / "_summary.json"
    summary_path.write_text(summary.model_dump_json(indent=2))
    logger.info("Wrote summary: %s", summary_path)

    # Write command chunks
    for chunk in commands:
        slug = _slugify(chunk.payload.command)
        path = commands_dir / f"{slug}.json"
        path.write_text(chunk.model_dump_json(indent=2))
    logger.info("Wrote %d command chunks to %s", len(commands), commands_dir)

    # Write overflow options chunks
    if options_overflow:
        for chunk in options_overflow:
            slug = _slugify(chunk.payload.command)
            path = commands_dir / f"{slug}__options.json"
            path.write_text(chunk.model_dump_json(indent=2))
        logger.info("Wrote %d options-overflow chunks to %s", len(options_overflow), commands_dir)

    total = 1 + len(commands) + (len(options_overflow) if options_overflow else 0)
    logger.info("Total: %d chunks written to %s", total, result_dir)
    return result_dir
