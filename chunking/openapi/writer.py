"""Chunk file writer.

Writes chunk models to disk as JSON files in the directory structure
defined in the mapping spec:

chunks/
  {source_type}/
    {source_name}/
      v{version}/
        _summary.json
        endpoints/
          {method}__{path_slug}.json
        schemas/
          {schema_name}.json
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from chunking.models import (
    ApiSummaryChunk,
    EndpointChunk,
    SchemaChunk,
)

logger = logging.getLogger(__name__)


def _slugify_path(path: str) -> str:
    """Convert an endpoint path to a filesystem-safe slug.

    '/finance/purchase/{purchase_id}' → 'finance__purchase__{purchase_id}'
    '/demarcation/{demarcation_id}/latest-loa' → 'demarcation__{demarcation_id}__latest-loa'
    """
    # Strip leading slash
    slug = path.lstrip("/")
    # Replace path separators with double underscore
    slug = slug.replace("/", "__")
    return slug


def _slugify_schema_name(name: str) -> str:
    """Convert a schema name to a filesystem-safe slug.

    'intranet_api__schema__finance__read__Contract' → 'Contract__finance_read'
    'DemarcationDetails' → 'DemarcationDetails'
    """
    if "__" in name:
        parts = name.split("__")
        actual_name = parts[-1]
        qualifiers = [p for p in parts[1:-1] if p not in ("schema",)]
        if qualifiers:
            return f"{actual_name}__{'_'.join(qualifiers)}"
        return actual_name
    return name


def _serialize_chunk(chunk: ApiSummaryChunk | EndpointChunk | SchemaChunk) -> str:
    """Serialize a chunk to a pretty-printed JSON string."""
    return chunk.model_dump_json(indent=2)


def write_chunks(
    output_dir: Path,
    source_type: str,
    source_name: str,
    api_version: str,
    summary: ApiSummaryChunk,
    endpoints: list[EndpointChunk],
    schemas: list[SchemaChunk],
) -> Path:
    """Write all chunks to disk in the structured directory layout."""
    # Sanitize version for directory name
    safe_version = re.sub(r"[^a-zA-Z0-9._-]", "_", api_version)
    api_dir = output_dir / source_type / source_name / f"v{safe_version}"

    # Create directories
    endpoints_dir = api_dir / "endpoints"
    schemas_dir = api_dir / "schemas"
    endpoints_dir.mkdir(parents=True, exist_ok=True)
    schemas_dir.mkdir(parents=True, exist_ok=True)

    # Write summary
    summary_path = api_dir / "_summary.json"
    summary_path.write_text(_serialize_chunk(summary), encoding="utf-8")
    logger.info("Wrote summary: %s", summary_path)

    # Write endpoints
    for ep in endpoints:
        method = ep.payload.method.lower()
        path_slug = _slugify_path(ep.payload.path)
        filename = f"{method}__{path_slug}.json"
        filepath = endpoints_dir / filename
        filepath.write_text(_serialize_chunk(ep), encoding="utf-8")

    logger.info("Wrote %d endpoint chunks to %s", len(endpoints), endpoints_dir)

    # Write schemas
    for schema in schemas:
        name_slug = _slugify_schema_name(schema.payload.schema_name)
        filename = f"{name_slug}.json"
        filepath = schemas_dir / filename
        filepath.write_text(_serialize_chunk(schema), encoding="utf-8")

    logger.info("Wrote %d schema chunks to %s", len(schemas), schemas_dir)

    total = 1 + len(endpoints) + len(schemas)
    logger.info("Total: %d chunks written to %s", total, api_dir)

    return api_dir
