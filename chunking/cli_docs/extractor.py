"""CLI documentation extractor — discovers subcommands and captures docs text.

Two extraction strategies:

1. **Man pages** (default) — invokes ``man`` for tools that have man pages (e.g., git).
2. **--help recursive** — runs ``<tool> [subcmd...] --help`` and recursively
   discovers nested subcommands from "Available Commands" blocks.  Use this
   for cobra-based CLIs (kubectl, docker, helm, terraform) that lack man pages.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)


def get_tool_version(tool: str) -> str:
    """Try to get the tool version string.

    Attempts several common flag conventions in order, including
    ``version --client`` for tools like kubectl that require it.
    """
    # Each entry is a list of args to append after the tool name.
    # Order matters: most common first, tool-specific variants after.
    flag_variants: list[list[str]] = [
        ["--version"],
        ["version", "--client"],  # kubectl
        ["version"],
        ["-v"],
    ]
    for flags in flag_variants:
        try:
            result = subprocess.run(
                [tool] + flags,
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = (result.stdout or result.stderr).strip()
            if output and result.returncode == 0:
                # Extract version-like string (e.g., "git version 2.44.0" → "2.44.0")
                m = re.search(r"(\d+\.\d+[\.\d]*)", output)
                if m:
                    return m.group(1)
                return output.splitlines()[0][:80]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return ""


def get_man_page(page_name: str) -> str | None:
    """Capture the text of a man page."""
    try:
        # Inherit the current environment so man can find its pages,
        # but override pager/formatting vars to get plain text output.
        env = {**os.environ, "MANWIDTH": "120", "MANPAGER": "cat", "PAGER": "cat"}
        result = subprocess.run(
            ["man", page_name],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            return None
        # Strip backspace-based bold/underline formatting from man
        text = result.stdout
        # Remove backspace sequences: X\bX (bold) or _\bX (underline)
        text = re.sub(r".\x08", "", text)
        return text.strip() if text.strip() else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def discover_git_subcommands() -> list[str]:
    """Discover all git subcommands using ``git --list-cmds``."""
    commands: set[str] = set()
    try:
        result = subprocess.run(
            ["git", "--list-cmds=main,others"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                cmd = line.strip()
                if cmd:
                    commands.add(cmd)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        logger.warning("Could not run 'git --list-cmds', falling back to man page scan")

    if not commands:
        # Fallback: scan for git-* man pages
        try:
            result = subprocess.run(
                ["man", "-k", "^git-"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    m = re.match(r"git-(\S+)", line)
                    if m:
                        commands.add(m.group(1))
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            logger.warning("Fallback man -k scan also failed")

    return sorted(commands)


def extract_tool_man_pages(
    tool: str,
    subcommands: list[str],
) -> dict[str, str]:
    """Extract man page text for a tool and all its subcommands."""
    pages: dict[str, str] = {}

    # Top-level man page
    logger.info("Extracting man page: %s", tool)
    text = get_man_page(tool)
    if text:
        pages[tool] = text
    else:
        logger.warning("No man page found for: %s", tool)

    # Subcommand man pages
    for subcmd in subcommands:
        page_name = f"{tool}-{subcmd}"
        text = get_man_page(page_name)
        if text:
            pages[page_name] = text
            logger.debug("Extracted: %s", page_name)
        else:
            logger.debug("No man page for: %s (skipped)", page_name)

    logger.info(
        "Extracted %d man pages for %s (%d subcommands tried)",
        len(pages),
        tool,
        len(subcommands),
    )
    return pages


# ---------------------------------------------------------------------------
# --help based extraction (cobra-style CLIs)
# ---------------------------------------------------------------------------


def get_help_output(tool: str, subcmd_parts: list[str] | None = None) -> str | None:
    """Capture the ``--help`` output for a command."""
    cmd = [tool] + (subcmd_parts or []) + ["--help"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ},
        )
        # Some tools print help on stderr (e.g., docker), merge both
        text = (result.stdout or "") + (result.stderr or "")
        text = text.strip()
        return text if text else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("Failed to get help for %s: %s", " ".join(cmd), exc)
        return None


def _parse_available_commands(help_text: str) -> list[str]:
    """Extract subcommand names from help output command listings.

    Handles two common formats:

    1. **Standard cobra** — single "Available Commands:" block::

           Available Commands:
             apply       Apply a configuration ...
             create      Create a resource ...

    2. **Categorised groups** (kubectl-style) — multiple category headers
       each followed by indented command lines::

           Basic Commands (Beginner):
             create      Create a resource ...
             expose      Expose a service ...

           Deploy Commands:
             rollout     Manage the rollout ...
    """
    commands: list[str] = []

    # Strategy 1: single "Available Commands:" block
    avail_pattern = re.compile(
        r"^(?:Available\s+)?Commands?:\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    m = avail_pattern.search(help_text)
    if m:
        for line in help_text[m.end() :].splitlines():
            if not line or not line.startswith(" "):
                if commands:
                    break
                continue
            cm = re.match(r"^\s{2,}(\S+)", line)
            if cm:
                name = cm.group(1)
                if not name.startswith("[") and not name.startswith('"'):
                    commands.append(name)
        if commands:
            return sorted(set(commands))

    # Strategy 2: categorised groups — look for lines matching
    # "<Category Name> (<qualifier>):" or "<Category Name>:" followed
    # by indented "  <command>   <description>" lines.
    # Category headers are NOT indented and end with ":"
    #
    # We skip known cobra structural sections (Usage, Flags, etc.) to avoid
    # false positives like "  kubectl [flags]" under "Usage:".
    _NON_COMMAND_SECTIONS = frozenset(
        {
            "Usage:",
            "Aliases:",
            "Examples:",
            "Flags:",
            "Options:",
            "Global Flags:",
            "Additional help topics:",
        }
    )
    group_header = re.compile(
        r"^[A-Z][\w\s]*(?:\([^)]*\))?\s*:\s*$",
        re.MULTILINE,
    )
    in_group = False
    for line in help_text.splitlines():
        if group_header.match(line):
            if line.strip() in _NON_COMMAND_SECTIONS:
                in_group = False
            else:
                in_group = True
            continue
        if in_group:
            if not line.strip():
                # Blank line — end of this group, but more groups may follow
                in_group = False
                continue
            if not line.startswith(" "):
                # Non-indented, non-blank line — we've left the command area
                in_group = False
                continue
            cm = re.match(r"^\s{2,}(\S+)", line)
            if cm:
                name = cm.group(1)
                if not name.startswith("[") and not name.startswith('"'):
                    commands.append(name)

    return sorted(set(commands))


def discover_subcommands_from_help(
    tool: str,
    max_depth: int = 2,
    _current_depth: int = 1,
    _parent_parts: list[str] | None = None,
) -> list[list[str]]:
    """Recursively discover subcommands by parsing ``--help`` output."""
    parent_parts = _parent_parts or []
    result: list[list[str]] = []

    help_text = get_help_output(tool, parent_parts if parent_parts else None)
    if not help_text:
        return result

    child_names = _parse_available_commands(help_text)
    if not child_names:
        return result

    for name in child_names:
        subcmd_path = parent_parts + [name]
        result.append(subcmd_path)

        # Recurse if we haven't hit max depth
        if _current_depth < max_depth:
            nested = discover_subcommands_from_help(
                tool,
                max_depth=max_depth,
                _current_depth=_current_depth + 1,
                _parent_parts=subcmd_path,
            )
            result.extend(nested)

    if not parent_parts:
        logger.info(
            "Discovered %d subcommands for %s (max_depth=%d)",
            len(result),
            tool,
            max_depth,
        )
    return result


def extract_tool_help_pages(
    tool: str,
    subcommand_paths: list[list[str]],
) -> dict[str, str]:
    """Extract ``--help`` text for a tool and all its discovered subcommands."""
    pages: dict[str, str] = {}

    # Top-level help
    logger.info("Extracting help: %s", tool)
    text = get_help_output(tool)
    if text:
        pages[tool] = text
    else:
        logger.warning("No help output for: %s", tool)

    # Subcommand help
    for parts in subcommand_paths:
        key = f"{tool} {' '.join(parts)}"
        text = get_help_output(tool, parts)
        if text:
            pages[key] = text
            logger.debug("Extracted help: %s", key)
        else:
            logger.debug("No help output for: %s (skipped)", key)

    logger.info(
        "Extracted %d help pages for %s (%d subcommand paths tried)",
        len(pages),
        tool,
        len(subcommand_paths),
    )
    return pages
