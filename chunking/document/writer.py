"""Document chunk file writer.

Writes chunk models to disk as JSON files in the directory structure:

chunks/
  document/
    {source_name}/
      v{version}/
        _summary.json
        sections/
          {doc_slug}__{section_slug}.json
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from chunking.models import DocumentSectionChunk, DocumentSummaryChunk

logger = logging.getLogger(__name__)


def _serialize_chunk(chunk: DocumentSummaryChunk | DocumentSectionChunk) -> str:
    """Serialize a chunk to a pretty-printed JSON string."""
    return chunk.model_dump_json(indent=2)


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80] if slug else "untitled"


def write_document_chunks(
    output_dir: Path,
    source_name: str,
    version: str,
    summary: DocumentSummaryChunk,
    sections: list[DocumentSectionChunk],
) -> Path:
    """Write all document chunks to disk in the structured directory layout."""
    safe_version = re.sub(r"[^a-zA-Z0-9._-]", "_", version)
    source_dir = output_dir / "document" / source_name / f"v{safe_version}"

    # Create directories.
    sections_dir = source_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    # Write summary.
    summary_path = source_dir / "_summary.json"
    summary_path.write_text(_serialize_chunk(summary), encoding="utf-8")
    logger.info("Wrote summary: %s", summary_path)

    # Write section chunks.
    for section in sections:
        doc_slug = _slugify(section.payload.document_name)
        sec_slug = _slugify(section.payload.section_title) if section.payload.section_title else "preamble"
        filename = f"{doc_slug}__{section.payload.section_index:04d}-{sec_slug}.json"
        filepath = sections_dir / filename
        filepath.write_text(_serialize_chunk(section), encoding="utf-8")

    logger.info("Wrote %d section chunks to %s", len(sections), sections_dir)

    total = 1 + len(sections)
    logger.info("Total: %d chunks written to %s", total, source_dir)

    return source_dir
