"""Intermediate dataclasses for parsed CLI man page data.

These represent the structured output of the man page parser before
transformation into Qdrant-ready chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OptionInfo:
    """A single command-line option/flag."""

    flags: str  # e.g., "-a, --all"
    description: str = ""


@dataclass
class CommandDoc:
    """Parsed documentation for a single CLI command or subcommand."""

    command: str  # e.g., "git commit"
    name_line: str = ""  # e.g., "git-commit - Record changes to the repository"
    synopsis: str = ""
    description: str = ""
    options: list[OptionInfo] = field(default_factory=list)
    examples: str = ""
    see_also: list[str] = field(default_factory=list)
    raw_sections: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolDocs:
    """Complete parsed documentation for a CLI tool and all its subcommands."""

    tool_name: str  # e.g., "git"
    source_name: str  # e.g., "gitcli"
    version: str = ""
    description: str = ""  # One-line from top-level NAME
    commands: list[CommandDoc] = field(default_factory=list)
