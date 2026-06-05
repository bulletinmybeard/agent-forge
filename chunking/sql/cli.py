"""CLI entry point for the SQL Schema mapper.

Usage:
    poetry run python -m chunking.sql.cli <tbls_json_file> [--source-name <name>] [--version <ver>] [--output-dir <dir>]
    poetry run python -m chunking.sql.cli --all [--schemas-dir <dir>] [--output-dir <dir>]

Examples:
    # Map a single tbls JSON file
    poetry run python -m chunking.sql.cli data/SQL-Schemas/schema-portal-db.json

    # Map with explicit source name and version
    poetry run python -m chunking.sql.cli schema.json --source-name portal-db --version 2026-03-07

    # Map all tbls JSON files in a directory
    poetry run python -m chunking.sql.cli --all --schemas-dir data/SQL-Schemas

Generating tbls JSON (prerequisite):
    tbls out postgres://user:pass@localhost:5432/mydb -t json -o schema-mydb.json
    tbls out mysql://user:pass@localhost:3306/mydb -t json -o schema-mydb.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from chunking.config import settings
from chunking.sql.mapper import map_schema_to_chunks
from chunking.sql.parser import parse_tbls_json
from chunking.sql.writer import write_sql_chunks

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def process_file(
    filepath: Path,
    output_dir: Path,
    source_name: str | None = None,
    version: str | None = None,
) -> None:
    """Parse, map, and write chunks for a single tbls JSON file."""
    logger.info("Processing: %s", filepath)

    schema = parse_tbls_json(filepath)

    # Override source name if provided
    if source_name:
        schema.source_name_slug = source_name

    # Use provided version or default to today's date
    effective_version = version or date.today().isoformat()

    summary, tables, relationships = map_schema_to_chunks(schema)

    result_dir = write_sql_chunks(
        output_dir=output_dir,
        source_name=schema.source_name_slug,
        version=effective_version,
        summary=summary,
        tables=tables,
        relationships=relationships,
    )

    logger.info("Done: %s → %s", filepath.name, result_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentForge SQL Schema Mapper — parse tbls JSON and chunk for Qdrant")
    parser.add_argument("file", nargs="?", help="Path to a tbls JSON file to process")
    parser.add_argument("--all", action="store_true", help="Process all JSON files in the schemas directory")
    parser.add_argument("--schemas-dir", type=str, default=None, help="Directory containing tbls JSON files")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory for chunk files")
    parser.add_argument("--source-name", type=str, default=None, help="Override the source name slug")
    parser.add_argument("--version", type=str, default=None, help="Version string (default: today's date)")

    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else Path(settings.mapper.chunks_output_dir)

    if args.all:
        schemas_dir = Path(args.schemas_dir) if args.schemas_dir else Path("data/SQL-Schemas")
        if not schemas_dir.is_dir():
            logger.error("Schemas directory not found: %s", schemas_dir)
            sys.exit(1)

        json_files = sorted(schemas_dir.glob("*.json"))
        if not json_files:
            logger.warning("No JSON files found in %s", schemas_dir)
            sys.exit(0)

        logger.info("Found %d tbls JSON file(s) in %s", len(json_files), schemas_dir)
        for filepath in json_files:
            process_file(filepath, output_dir, version=args.version)

    elif args.file:
        filepath = Path(args.file)
        if not filepath.is_file():
            logger.error("File not found: %s", filepath)
            sys.exit(1)
        process_file(filepath, output_dir, source_name=args.source_name, version=args.version)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
