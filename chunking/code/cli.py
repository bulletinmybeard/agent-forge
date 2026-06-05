"""CLI entry point for code mapper.

Usage:
    # Extract and chunk a Python project directly
    poetry run python -m chunking.code.cli /path/to/project \\
        --source-name my-api --version v2026-03-08

    # Load from pre-extracted JSON (from test-scripts/extract_python.py)
    poetry run python -m chunking.code.cli --from-json /path/to/chunks.json \\
        --source-name my-api --version v2026-03-08

    # Custom output directory
    poetry run python -m chunking.code.cli /path/to/project \\
        --source-name my-api --output-dir data/chunks

    # Show stats only (no chunk files written)
    poetry run python -m chunking.code.cli /path/to/project --stats
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from chunking.config import settings

from .extractor import extract_project
from .mapper import map_code_to_chunks
from .parser import parse_extraction_json
from .writer import write_code_chunks

logger = logging.getLogger(__name__)


def _print_stats(summary, classes, functions, modules) -> None:
    """Print a summary of the mapping results."""
    print("\n═══ Code Mapper Summary ═══")
    print(f"Project: {summary.payload.project_name}")
    print(f"Framework: {summary.payload.framework or 'generic Python'}")
    print(f"Files processed: {summary.payload.file_count}")
    print(f"Total chunks: {1 + len(classes) + len(functions) + len(modules)}")
    print("  Summary: 1")
    print(f"  Classes: {len(classes)}")
    print(f"  Functions: {len(functions)}")
    print(f"  Modules: {len(modules)}")

    if classes:
        tag_counts = Counter(c.payload.tag for c in classes)
        print("\nClass distribution by tag:")
        for tag, count in tag_counts.most_common():
            print(f"  {tag:25s} {count:>5d}")

    # Docstring coverage
    classes_with_docs = sum(1 for c in classes if c.payload.has_docstring)
    funcs_with_docs = sum(1 for f in functions if f.payload.has_docstring)
    total_items = len(classes) + len(functions)
    total_with_docs = classes_with_docs + funcs_with_docs
    if total_items:
        print(f"\nDocstring coverage: {total_with_docs}/{total_items} ({100 * total_with_docs / total_items:.0f}%)")

    # Top classes by method count
    if classes:
        sorted_classes = sorted(classes, key=lambda c: c.payload.method_count, reverse=True)
        print("\nTop 10 classes by method count:")
        for cls in sorted_classes[:10]:
            print(f"  {cls.payload.class_name:40s} {cls.payload.method_count:>3d} methods  [{cls.payload.tag}]")

    # Sample chunk text (first class)
    if classes:
        print("\n═══ Sample class chunk text ═══")
        print(classes[0].text[:500])
        if len(classes[0].text) > 500:
            print(f"  ... ({len(classes[0].text)} chars total)")


def process_code(
    project_path: Path | None,
    json_path: Path | None,
    source_name: str,
    project_name: str | None,
    version: str,
    output_dir: str,
    stats_only: bool = False,
) -> None:
    """End-to-end: extract → map → write."""
    project_name = project_name or source_name

    # 1. Extract or parse
    if json_path:
        logger.info("Loading pre-extracted JSON: %s", json_path)
        meta = parse_extraction_json(json_path, project_name)
    elif project_path:
        logger.info("Extracting from project: %s", project_path)
        meta = extract_project(project_path, project_name)
    else:
        logger.error("Either project_path or json_path must be provided")
        sys.exit(1)

    # 2. Map to chunks
    summary, classes, functions, modules = map_code_to_chunks(meta, source_name)

    if stats_only:
        _print_stats(summary, classes, functions, modules)
        return

    # 3. Write to disk
    result_dir = write_code_chunks(
        output_dir=output_dir,
        source_name=source_name,
        version=version,
        summary=summary,
        classes=classes,
        functions=functions,
        modules=modules,
    )

    total = 1 + len(classes) + len(functions) + len(modules)
    print(f"\nDone: {project_name} → {total} chunks written to {result_dir}")

    _print_stats(summary, classes, functions, modules)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Python/Django code and generate indexable chunks for AgentForge.",
    )
    parser.add_argument(
        "project_path",
        nargs="?",
        default=None,
        help="Path to the Python project root (for direct AST extraction)",
    )
    parser.add_argument(
        "--from-json",
        default=None,
        metavar="PATH",
        help="Load pre-extracted JSON instead of running AST extraction",
    )
    parser.add_argument(
        "--source-name",
        required=True,
        help="Source name slug for Qdrant (e.g., 'my-api')",
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help="Human-readable project name (default: same as source-name)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Version string (default: v{today's date})",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.mapper.chunks_output_dir,
        help=f"Base chunks output directory (default: {settings.mapper.chunks_output_dir})",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print summary statistics without writing chunk files",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Validate inputs
    if not args.project_path and not args.from_json:
        parser.error("Either project_path or --from-json must be provided")

    project_path = Path(args.project_path).resolve() if args.project_path else None
    json_path = Path(args.from_json).resolve() if args.from_json else None

    if project_path and not project_path.is_dir():
        parser.error(f"{project_path} is not a directory")
    if json_path and not json_path.is_file():
        parser.error(f"{json_path} is not a file")

    version = args.version or f"v{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    process_code(
        project_path=project_path,
        json_path=json_path,
        source_name=args.source_name,
        project_name=args.project_name,
        version=version,
        output_dir=args.output_dir,
        stats_only=args.stats,
    )


if __name__ == "__main__":
    main()
