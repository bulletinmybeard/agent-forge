"""CLI for direct database schema extraction → chunk pipeline.

Replaces the manual tbls step.  Connects to a database defined in
config.yaml, extracts the schema via SQLAlchemy, runs the mapper, and
writes chunk files — all in one command.

Usage:
    # Export a single database defined in config.yaml
    poetry run python -m chunking.db.cli export <name>

    # Export with explicit version
    poetry run python -m chunking.db.cli export <name> --version 2026-03-07

    # Export all configured databases
    poetry run python -m chunking.db.cli export-all

    # List configured databases
    poetry run python -m chunking.db.cli list

Examples:
    poetry run python -m chunking.db.cli list
    poetry run python -m chunking.db.cli export my-db
    poetry run python -m chunking.db.cli export-all --version 2026-03-07
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from chunking.config import settings
from chunking.db.schema_extractor import extract_schema
from chunking.sql.mapper import map_schema_to_chunks
from chunking.sql.writer import write_sql_chunks

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def _export_database(
    name: str,
    output_dir: Path,
    version: str | None = None,
) -> None:
    """Full pipeline: connect → extract → map → write chunks."""
    databases = settings.databases

    if name not in databases:
        logger.error(
            "Database '%s' not found in config.yaml. Available: %s",
            name,
            ", ".join(databases.keys()) or "(none)",
        )
        sys.exit(1)

    db_config = databases[name]
    effective_version = version or date.today().isoformat()

    logger.info("Exporting database: %s (source_name=%s)", name, db_config.source_name)

    # Step 1: Extract schema via SQLAlchemy
    schema = extract_schema(
        url=db_config.url,
        source_name=db_config.source_name,
        schema=db_config.schema,
    )

    # Step 2: Map to chunks (same mapper as tbls pipeline)
    summary, tables, relationships = map_schema_to_chunks(schema)

    # Step 3: Write chunk files to disk
    result_dir = write_sql_chunks(
        output_dir=output_dir,
        source_name=db_config.source_name,
        version=effective_version,
        summary=summary,
        tables=tables,
        relationships=relationships,
    )

    total = 1 + len(tables) + (1 if relationships else 0)
    logger.info("Done: %d chunks written to %s", total, result_dir)
    print(f"\nExported {schema.name}: {len(tables)} tables, {len(schema.relations)} relations -> {result_dir}")


def cmd_list() -> None:
    """List configured database connections."""
    databases = settings.databases
    if not databases:
        print("No databases configured in config.yaml.")
        print("Add a 'databases:' section — see config.yaml for examples.")
        return

    print(f"Configured databases ({len(databases)}):\n")
    for name, cfg in databases.items():
        # Mask the password in the URL for display
        url = cfg.url
        if "@" in url:
            pre, post = url.rsplit("@", 1)
            # Mask between :// ... : and @
            try:
                scheme_end = pre.index("://") + 3
                user_end = pre.index(":", scheme_end)
                url = pre[: user_end + 1] + "***@" + post
            except ValueError:
                url = pre[:20] + "***@" + post

        schema_str = f"  schema: {cfg.schema}" if cfg.schema else ""
        print(f"  {name}:")
        print(f"    url: {url}")
        print(f"    source_name: {cfg.source_name}")
        if schema_str:
            print(f"  {schema_str}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentForge Database Schema Extractor — connect, extract, chunk")
    subparsers = parser.add_subparsers(dest="command")

    # list
    subparsers.add_parser("list", help="List configured database connections")

    # export
    export_parser = subparsers.add_parser("export", help="Export a single database schema")
    export_parser.add_argument("name", help="Database name (as defined in config.yaml databases section)")
    export_parser.add_argument("--version", type=str, default=None, help="Version string (default: today's date)")
    export_parser.add_argument("--output-dir", type=str, default=None, help="Output directory for chunk files")

    # export-all
    all_parser = subparsers.add_parser("export-all", help="Export all configured databases")
    all_parser.add_argument("--version", type=str, default=None, help="Version string (default: today's date)")
    all_parser.add_argument("--output-dir", type=str, default=None, help="Output directory for chunk files")

    args = parser.parse_args()
    output_dir = (
        Path(args.output_dir)
        if hasattr(args, "output_dir") and args.output_dir
        else Path(settings.mapper.chunks_output_dir)
    )

    if args.command == "list":
        cmd_list()
    elif args.command == "export":
        _export_database(args.name, output_dir, version=args.version)
    elif args.command == "export-all":
        databases = settings.databases
        if not databases:
            print("No databases configured in config.yaml.")
            sys.exit(1)
        for name in databases:
            _export_database(name, output_dir, version=args.version)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
