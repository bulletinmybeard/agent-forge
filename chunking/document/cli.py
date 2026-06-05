"""CLI entry point for document mapper.

Usage:
    # Parse all Markdown files in a directory
    poetry run python -m chunking.document.cli \\
        --input docs/ \\
        --source-name agentforge-docs

    # With explicit version
    poetry run python -m chunking.document.cli \\
        --input docs/ \\
        --source-name agentforge-docs \\
        --version 2026-03-08

    # Custom split level (split at ### instead of ##)
    poetry run python -m chunking.document.cli \\
        --input docs/ \\
        --source-name agentforge-docs \\
        --split-level 3

    # Custom output directory
    poetry run python -m chunking.document.cli \\
        --input docs/ \\
        --source-name agentforge-docs \\
        --output-dir data/chunks
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from chunking.config import settings

from .mapper import map_documents_to_chunks
from .parser import parse_directory
from .writer import write_document_chunks

logger = logging.getLogger(__name__)


def process_documents(
    input_dir: str,
    source_name: str,
    output_dir: str,
    version: str | None = None,
    split_level: int = 2,
    max_section_chars: int = 2000,
) -> None:
    """End-to-end: scan → parse → map → write."""
    version = version or date.today().isoformat()
    input_path = Path(input_dir)

    if not input_path.is_dir():
        logger.error("Input directory does not exist: %s", input_dir)
        raise SystemExit(1)

    logger.info(
        "Processing documents: input=%s, source_name=%s, version=%s, split_level=%d",
        input_dir,
        source_name,
        version,
        split_level,
    )

    # 1. Parse all Markdown files.
    source = parse_directory(
        input_dir=input_path,
        source_name=source_name,
        split_level=split_level,
        max_section_chars=max_section_chars,
    )

    if not source.documents:
        logger.warning("No documents found in %s — nothing to write", input_dir)
        print(f"\nNo .md files found in {input_dir}")
        return

    # 2. Map to chunks.
    summary, sections = map_documents_to_chunks(source)

    # 3. Write to disk.
    result_dir = write_document_chunks(
        output_dir=Path(output_dir),
        source_name=source_name,
        version=version,
        summary=summary,
        sections=sections,
    )

    total = 1 + len(sections)
    print(
        f"\nDone: {source_name} → {total} chunks "
        f"({len(source.documents)} documents, {len(sections)} sections) "
        f"written to {result_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse Markdown documents and generate indexable chunks.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Directory containing Markdown files (scanned recursively)",
    )
    parser.add_argument(
        "--source-name",
        required=True,
        help="Source name slug for Qdrant (e.g., 'agentforge-docs')",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Version string (default: today's date, e.g., '2026-03-08')",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.mapper.chunks_output_dir,
        help=f"Base chunks output directory (default: {settings.mapper.chunks_output_dir})",
    )
    parser.add_argument(
        "--split-level",
        type=int,
        default=2,
        help="Heading level at which to split sections (default: 2 = ##)",
    )
    parser.add_argument(
        "--max-section-chars",
        type=int,
        default=2000,
        help="Maximum characters per section before sub-splitting (default: 2000)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    process_documents(
        input_dir=args.input,
        source_name=args.source_name,
        output_dir=args.output_dir,
        version=args.version,
        split_level=args.split_level,
        max_section_chars=args.max_section_chars,
    )


if __name__ == "__main__":
    main()
