"""SQL Schema chunk file writer.

Writes chunk models to disk as JSON files in the directory structure:

chunks/
  sql-schema/
    {source_name}/
      v{version}/
        _summary.json
        _relationships.json
        tables/
          {table_name}.json
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from chunking.models import (
    DatabaseSummaryChunk,
    RelationshipMapChunk,
    TableChunk,
)

logger = logging.getLogger(__name__)


def _serialize_chunk(chunk: DatabaseSummaryChunk | TableChunk | RelationshipMapChunk) -> str:
    """Serialize a chunk to a pretty-printed JSON string."""
    return chunk.model_dump_json(indent=2)


def _slugify_table_name(name: str) -> str:
    """Convert a table name to a filesystem-safe slug.

    'public.users' → 'public__users'
    'my_table' → 'my_table'
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "__", name)


def write_sql_chunks(
    output_dir: Path,
    source_name: str,
    version: str,
    summary: DatabaseSummaryChunk,
    tables: list[TableChunk],
    relationships: RelationshipMapChunk | None = None,
) -> Path:
    """Write all SQL schema chunks to disk in the structured directory layout."""
    safe_version = re.sub(r"[^a-zA-Z0-9._-]", "_", version)
    source_dir = output_dir / "sql-schema" / source_name / f"v{safe_version}"

    # Create directories
    tables_dir = source_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    # Write summary
    summary_path = source_dir / "_summary.json"
    summary_path.write_text(_serialize_chunk(summary), encoding="utf-8")
    logger.info("Wrote summary: %s", summary_path)

    # Write table chunks
    for table in tables:
        name_slug = _slugify_table_name(table.payload.table_name)
        filename = f"{name_slug}.json"
        filepath = tables_dir / filename
        filepath.write_text(_serialize_chunk(table), encoding="utf-8")

    logger.info("Wrote %d table chunks to %s", len(tables), tables_dir)

    # Write relationship map (if exists)
    if relationships:
        rel_path = source_dir / "_relationships.json"
        rel_path.write_text(_serialize_chunk(relationships), encoding="utf-8")
        logger.info("Wrote relationship map: %s", rel_path)

    total = 1 + len(tables) + (1 if relationships else 0)
    logger.info("Total: %d chunks written to %s", total, source_dir)

    return source_dir
