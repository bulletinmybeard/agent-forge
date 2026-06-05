"""CLI entry point for the OpenAPI mapper.

Usage:
    poetry run python -m chunking.openapi.cli <openapi_json_file> [--output-dir <dir>]
    poetry run python -m chunking.openapi.cli --all [--schemas-dir <dir>] [--output-dir <dir>]

Examples:
    # Map a single file
    poetry run python -m chunking.openapi.cli data/OpenAPI-Schemas/openapi-intranet-api.json

    # Map all JSON files in the schemas directory
    poetry run python -m chunking.openapi.cli --all

    # Custom output directory
    poetry run python -m chunking.openapi.cli --all --output-dir ./chunks
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from chunking.config import settings
from chunking.openapi.mapper import map_spec_to_chunks
from chunking.openapi.parser import parse_openapi_file
from chunking.openapi.writer import write_chunks

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def process_file(filepath: Path, output_dir: Path) -> None:
    """Parse, map, and write chunks for a single OpenAPI file."""
    logger.info("Processing: %s", filepath)

    spec = parse_openapi_file(filepath)

    summary, endpoints, schemas = map_spec_to_chunks(
        spec,
        inline_schema_max_fields=settings.mapper.inline_schema_max_fields,
    )

    result_dir = write_chunks(
        output_dir=output_dir,
        source_type="openapi",
        source_name=spec.api_name_slug,
        api_version=spec.version,
        summary=summary,
        endpoints=endpoints,
        schemas=schemas,
    )

    logger.info("Done: %s → %s", filepath.name, result_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentForge OpenAPI Mapper — parse and chunk OpenAPI specs for Qdrant")
    parser.add_argument("file", nargs="?", help="Path to an OpenAPI JSON file to process")
    parser.add_argument("--all", action="store_true", help="Process all JSON files in the schemas directory")
    parser.add_argument("--schemas-dir", type=str, default=None, help="Directory containing OpenAPI JSON files")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory for chunk files")

    args = parser.parse_args()

    schemas_dir = Path(args.schemas_dir) if args.schemas_dir else Path(settings.mapper.openapi_schemas_dir)
    output_dir = Path(args.output_dir) if args.output_dir else Path(settings.mapper.chunks_output_dir)

    if args.all:
        if not schemas_dir.is_dir():
            logger.error("Schemas directory not found: %s", schemas_dir)
            sys.exit(1)

        json_files = sorted(schemas_dir.glob("*.json"))
        if not json_files:
            logger.warning("No JSON files found in %s", schemas_dir)
            sys.exit(0)

        logger.info("Found %d OpenAPI file(s) in %s", len(json_files), schemas_dir)
        for filepath in json_files:
            process_file(filepath, output_dir)

    elif args.file:
        filepath = Path(args.file)
        if not filepath.is_file():
            logger.error("File not found: %s", filepath)
            sys.exit(1)
        process_file(filepath, output_dir)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
