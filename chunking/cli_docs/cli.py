"""CLI entry point for docs mapper.

Usage:
    # Extract and chunk git man pages (default: man page mode)
    poetry run python -m chunking.cli_docs.cli git --source-name gitcli

    # Extract using --help (for tools without man pages)
    poetry run python -m chunking.cli_docs.cli kubectl --source-name kubectlcli --help-max-depth 2

    # With explicit version
    poetry run python -m chunking.cli_docs.cli git --source-name gitcli --version 2.44.0

    # Custom output directory
    poetry run python -m chunking.cli_docs.cli git --source-name gitcli --output-dir data/chunks
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable

from chunking.config import settings

from .extractor import (
    discover_git_subcommands,
    discover_subcommands_from_help,
    extract_tool_help_pages,
    extract_tool_man_pages,
    get_tool_version,
)
from .mapper import map_tool_to_chunks
from .parser import parse_tool_help_pages, parse_tool_man_pages
from .writer import write_docs_chunks

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool-specific subcommand discoverers (man-page mode only)
# ---------------------------------------------------------------------------

_SUBCOMMAND_DISCOVERERS: dict[str, Callable[..., list[str]]] = {
    "git": discover_git_subcommands,
}


def _process_man_pages(
    tool: str,
    source_name: str,
    version: str,
    output_dir: str,
) -> None:
    """Man-page extraction path (existing behaviour)."""

    # 1. Discover subcommands
    discoverer = _SUBCOMMAND_DISCOVERERS.get(tool)
    if discoverer:
        subcommands = discoverer()
        logger.info("Discovered %d subcommands for %s", len(subcommands), tool)
    else:
        logger.warning(
            "No subcommand discoverer for '%s'; will only extract top-level man page. "
            "Consider adding a discoverer to _SUBCOMMAND_DISCOVERERS.",
            tool,
        )
        subcommands = []

    # 2. Extract man pages
    man_pages = extract_tool_man_pages(tool, subcommands)
    if not man_pages:
        logger.error("No man pages found for %s — aborting", tool)
        sys.exit(1)

    # 3. Parse
    tool_docs = parse_tool_man_pages(tool, source_name, version, man_pages)

    # 4. Map to chunks
    summary, commands, options_overflow = map_tool_to_chunks(tool_docs)

    # 5. Write to disk
    result_dir = write_docs_chunks(
        output_dir=output_dir,
        source_name=source_name,
        version=version,
        summary=summary,
        commands=commands,
        options_overflow=options_overflow,
    )

    total = 1 + len(commands) + len(options_overflow)
    print(f"\nDone: {tool} (man pages) → {total} chunks written to {result_dir}")


def _process_help(
    tool: str,
    source_name: str,
    version: str,
    output_dir: str,
    max_depth: int,
) -> None:
    """--help recursive extraction path."""

    # 1. Discover subcommands recursively via --help
    subcommand_paths = discover_subcommands_from_help(tool, max_depth=max_depth)
    if not subcommand_paths:
        logger.warning("No subcommands discovered for %s via --help", tool)

    # 2. Extract help text for all commands
    help_pages = extract_tool_help_pages(tool, subcommand_paths)
    if not help_pages:
        logger.error("No help output captured for %s — aborting", tool)
        sys.exit(1)

    # 3. Parse
    tool_docs = parse_tool_help_pages(tool, source_name, version, help_pages)

    # 4. Map to chunks
    summary, commands, options_overflow = map_tool_to_chunks(tool_docs)

    # 5. Write to disk
    result_dir = write_docs_chunks(
        output_dir=output_dir,
        source_name=source_name,
        version=version,
        summary=summary,
        commands=commands,
        options_overflow=options_overflow,
    )

    total = 1 + len(commands) + len(options_overflow)
    print(f"\nDone: {tool} (--help, depth={max_depth}) → {total} chunks written to {result_dir}")


def process_tool(
    tool: str,
    output_dir: str,
    source_name: str | None = None,
    version: str | None = None,
    help_max_depth: int = 0,
) -> None:
    """End-to-end: discover → extract → parse → map → write."""
    source_name = source_name or f"{tool}cli"
    version = version or get_tool_version(tool) or "unknown"

    logger.info(
        "Processing tool: %s (source_name=%s, version=%s, mode=%s)",
        tool,
        source_name,
        version,
        f"help(depth={help_max_depth})" if help_max_depth > 0 else "man-pages",
    )

    if help_max_depth > 0:
        _process_help(tool, source_name, version, output_dir, help_max_depth)
    else:
        _process_man_pages(tool, source_name, version, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract CLI documentation and generate indexable chunks.",
    )
    parser.add_argument(
        "tool",
        help="CLI tool name (e.g., 'git', 'kubectl')",
    )
    parser.add_argument(
        "--source-name",
        default=None,
        help="Source name slug for Qdrant (default: {tool}cli)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Tool version (default: auto-detect via --version flag)",
    )
    parser.add_argument(
        "--output-dir",
        default=settings.mapper.chunks_output_dir,
        help=f"Base chunks output directory (default: {settings.mapper.chunks_output_dir})",
    )
    parser.add_argument(
        "--help-max-depth",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Use --help extraction instead of man pages. "
            "N controls recursion depth: 1 = top-level subcommands only, "
            "2 = one level of nesting (e.g., 'kubectl create deployment'). "
            "Default 0 = man page mode."
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    process_tool(
        tool=args.tool,
        output_dir=args.output_dir,
        source_name=args.source_name,
        version=args.version,
        help_max_depth=args.help_max_depth,
    )


if __name__ == "__main__":
    main()
