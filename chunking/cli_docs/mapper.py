"""Docs mapper — transforms parsed ToolDocs into Qdrant-ready chunk models.

Produces three chunk types:
- DocsSummaryChunk: one per tool (overview + list of commands)
- CommandChunk: one per subcommand (synopsis, description, options, examples)
- CommandOptionsChunk: overflow chunk when a command's options exceed the size cap
"""

from __future__ import annotations

import hashlib
import logging
import re
import textwrap

from chunking.models import (
    CommandChunk,
    CommandOptionsChunk,
    CommandOptionsPayload,
    CommandPayload,
    DocsSummaryChunk,
    DocsSummaryPayload,
    SourceType,
)

from .types import CommandDoc, ToolDocs

logger = logging.getLogger(__name__)

# Approximate token-to-char ratio; 3000 tokens ≈ 12000 chars.
# If a command chunk exceeds this, split OPTIONS into a separate chunk.
_TEXT_SIZE_CAP = 12000

_TAG_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "that",
        "this",
        "are",
        "not",
        "use",
        "set",
        "get",
        "all",
        "git",
    }
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _infer_tags(command: str, summary: str) -> list[str]:
    """Infer search tags from command name and summary."""
    tags: set[str] = set()
    # Split command parts: "git commit" → ["git", "commit"]
    for part in re.split(r"[\s-]+", command):
        if len(part) >= 3 and part.lower() not in _TAG_STOP_WORDS:
            tags.add(part.lower())
    # Extract meaningful words from summary
    for word in re.split(r"[\s,;.()]+", summary):
        w = word.lower().strip()
        if len(w) >= 4 and w not in _TAG_STOP_WORDS:
            tags.add(w)
    return sorted(tags)[:15]


def _format_options_text(doc: CommandDoc) -> str:
    """Format the options list as readable text."""
    if not doc.options:
        return ""
    lines = ["Options:"]
    for opt in doc.options:
        if opt.description:
            lines.append(f"  {opt.flags} — {opt.description}")
        else:
            lines.append(f"  {opt.flags}")
    return "\n".join(lines)


def _generate_command_text(doc: CommandDoc, source_name: str, include_options: bool = True) -> str:
    """Generate the natural-language text for a command chunk."""
    parts: list[str] = []

    parts.append(f"Command: {doc.command}")
    parts.append(f"Source: {source_name} (docs)")

    if doc.name_line and " - " in doc.name_line:
        summary = doc.name_line.split(" - ", 1)[1].strip().split("\n")[0]
        parts.append(f"Summary: {summary}")

    if doc.synopsis:
        # Clean up synopsis whitespace
        synopsis = " ".join(doc.synopsis.split())
        parts.append(f"\nSynopsis: {synopsis}")

    if doc.description:
        parts.append(f"\nDescription:\n{textwrap.indent(doc.description, '  ')}")

    if include_options and doc.options:
        parts.append(f"\n{_format_options_text(doc)}")

    if doc.examples:
        parts.append(f"\nExamples:\n{textwrap.indent(doc.examples, '  ')}")

    if doc.see_also:
        parts.append(f"\nSee also: {', '.join(doc.see_also[:10])}")

    return "\n".join(parts)


def _generate_options_overflow_text(doc: CommandDoc, source_name: str) -> str:
    """Generate text for the overflow options chunk."""
    parts: list[str] = [
        f"Command: {doc.command} (options reference)",
        f"Source: {source_name} (docs)",
        "",
        _format_options_text(doc),
    ]
    return "\n".join(parts)


def _generate_summary_text(tool_docs: ToolDocs) -> str:
    """Generate the tool summary text listing all commands."""
    parts: list[str] = [
        f"CLI Tool: {tool_docs.tool_name}",
        f"Source: {tool_docs.source_name} (docs)",
    ]
    if tool_docs.version:
        parts.append(f"Version: {tool_docs.version}")
    if tool_docs.description:
        parts.append(f"Description: {tool_docs.description}")
    parts.append(f"Total commands: {len(tool_docs.commands)}")

    # Group: top-level command vs subcommands
    subcmds = [c for c in tool_docs.commands if c.command != tool_docs.tool_name]
    if subcmds:
        parts.append("\nAvailable commands:")
        for cmd in subcmds:
            summary = ""
            if cmd.name_line and " - " in cmd.name_line:
                summary = cmd.name_line.split(" - ", 1)[1].strip().split("\n")[0]
            if summary:
                parts.append(f"  {cmd.command} — {summary}")
            else:
                parts.append(f"  {cmd.command}")

    return "\n".join(parts)


def map_tool_to_chunks(
    tool_docs: ToolDocs,
) -> tuple[DocsSummaryChunk, list[CommandChunk], list[CommandOptionsChunk]]:
    """Map parsed tool documentation into chunk models."""
    source_name = tool_docs.source_name
    command_chunks: list[CommandChunk] = []
    options_chunks: list[CommandOptionsChunk] = []

    for doc in tool_docs.commands:
        # Generate full text first to check size
        full_text = _generate_command_text(doc, source_name, include_options=True)

        if len(full_text) > _TEXT_SIZE_CAP and doc.options:
            # Split: main chunk without options, separate options chunk
            main_text = _generate_command_text(doc, source_name, include_options=False)
            opts_text = _generate_options_overflow_text(doc, source_name)

            cmd_slug = doc.command.replace(" ", "-")

            command_chunks.append(
                CommandChunk(
                    source_type=SourceType.DOCS,
                    source_name=source_name,
                    chunk_id=f"{source_name}:cmd:{cmd_slug}",
                    text=main_text,
                    content_hash=_sha256(main_text),
                    payload=CommandPayload(
                        source_name=source_name,
                        chunk_id=f"{source_name}:cmd:{cmd_slug}",
                        tool_name=tool_docs.tool_name,
                        command=doc.command,
                        summary=doc.name_line.split(" - ", 1)[1].strip().split("\n")[0]
                        if " - " in doc.name_line
                        else "",
                        option_count=len(doc.options),
                        has_examples=bool(doc.examples),
                        tags=_infer_tags(doc.command, doc.name_line),
                        content_hash=_sha256(main_text),
                    ),
                )
            )

            options_chunks.append(
                CommandOptionsChunk(
                    source_type=SourceType.DOCS,
                    source_name=source_name,
                    chunk_id=f"{source_name}:cmd-opts:{cmd_slug}",
                    text=opts_text,
                    content_hash=_sha256(opts_text),
                    payload=CommandOptionsPayload(
                        source_name=source_name,
                        chunk_id=f"{source_name}:cmd-opts:{cmd_slug}",
                        tool_name=tool_docs.tool_name,
                        command=doc.command,
                        option_count=len(doc.options),
                        tags=_infer_tags(doc.command, "options flags"),
                        content_hash=_sha256(opts_text),
                    ),
                )
            )
        else:
            # Single chunk for this command
            cmd_slug = doc.command.replace(" ", "-")
            command_chunks.append(
                CommandChunk(
                    source_type=SourceType.DOCS,
                    source_name=source_name,
                    chunk_id=f"{source_name}:cmd:{cmd_slug}",
                    text=full_text,
                    content_hash=_sha256(full_text),
                    payload=CommandPayload(
                        source_name=source_name,
                        chunk_id=f"{source_name}:cmd:{cmd_slug}",
                        tool_name=tool_docs.tool_name,
                        command=doc.command,
                        summary=doc.name_line.split(" - ", 1)[1].strip().split("\n")[0]
                        if " - " in doc.name_line
                        else "",
                        option_count=len(doc.options),
                        has_examples=bool(doc.examples),
                        tags=_infer_tags(doc.command, doc.name_line),
                        content_hash=_sha256(full_text),
                    ),
                )
            )

    # Summary chunk
    summary_text = _generate_summary_text(tool_docs)
    subcmd_names = [c.command for c in tool_docs.commands if c.command != tool_docs.tool_name]
    summary_chunk = DocsSummaryChunk(
        source_type=SourceType.DOCS,
        source_name=source_name,
        chunk_id=f"{source_name}:docs-summary",
        text=summary_text,
        content_hash=_sha256(summary_text),
        payload=DocsSummaryPayload(
            source_name=source_name,
            chunk_id=f"{source_name}:docs-summary",
            tool_name=tool_docs.tool_name,
            tool_version=tool_docs.version,
            description=tool_docs.description,
            command_count=len(subcmd_names),
            command_names=subcmd_names,
            tags=_infer_tags(tool_docs.tool_name, tool_docs.description),
            content_hash=_sha256(summary_text),
        ),
    )

    logger.info(
        "Mapped %s: 1 summary + %d command + %d options-overflow chunks",
        source_name,
        len(command_chunks),
        len(options_chunks),
    )
    return summary_chunk, command_chunks, options_chunks
