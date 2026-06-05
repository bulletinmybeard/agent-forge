"""CLI docs text parser — converts raw man page or ``--help`` output into
structured :class:`CommandDoc`.

Two parsing modes:

1. **Man pages** — standard section headers (NAME, SYNOPSIS, DESCRIPTION,
   OPTIONS, EXAMPLES, SEE ALSO).
2. **--help output** — cobra-style sections (Usage, Examples, Flags,
   Available Commands, etc.).
"""

from __future__ import annotations

import logging
import re

from .types import CommandDoc, OptionInfo, ToolDocs

logger = logging.getLogger(__name__)

# Standard man page section headers (uppercase, at start of line)
_SECTION_PATTERN = re.compile(r"^([A-Z][A-Z _/-]+)$", re.MULTILINE)


def _split_sections(text: str) -> dict[str, str]:
    """Split man page text into named sections."""
    sections: dict[str, str] = {}
    matches = list(_SECTION_PATTERN.finditer(text))

    for i, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections[name] = body
    return sections


def _parse_options(options_text: str) -> list[OptionInfo]:
    """Parse the OPTIONS section into individual option entries.

    Handles man page option formatting like:
        -a, --all
            Stage all modified and deleted files.

        --amend
            Replace the tip of the current branch.
    """
    options: list[OptionInfo] = []

    # Split into option blocks: each starts with an indented flag line
    # Flag lines typically start with whitespace then a dash
    blocks: list[tuple[str, list[str]]] = []
    current_flags: str | None = None
    current_desc_lines: list[str] = []

    for line in options_text.splitlines():
        stripped = line.strip()

        # Detect a new option flag line:
        # - Starts with optional whitespace, then "-" or "--"
        # - But NOT a description continuation that happens to start with a dash
        if re.match(r"^\s{1,8}-[\w-]", line) and not line.startswith("        " * 2):
            # Save previous block
            if current_flags is not None:
                blocks.append((current_flags, current_desc_lines))
            current_flags = stripped
            current_desc_lines = []
        elif current_flags is not None:
            current_desc_lines.append(stripped)
        # else: preamble text before first option, skip

    # Save last block
    if current_flags is not None:
        blocks.append((current_flags, current_desc_lines))

    for flags, desc_lines in blocks:
        # Clean up description: join lines, collapse whitespace
        desc = " ".join(line for line in desc_lines if line).strip()
        # Truncate very long descriptions to keep chunks manageable
        if len(desc) > 300:
            desc = desc[:297] + "..."
        options.append(OptionInfo(flags=flags, description=desc))

    return options


def _extract_see_also(text: str) -> list[str]:
    """Extract referenced man pages from SEE ALSO section.

    Matches patterns like: git-commit(1), gitcli(7)
    """
    return re.findall(r"([\w-]+)\(\d+\)", text)


def _clean_description(text: str, max_length: int = 2000) -> str:
    """Clean and optionally truncate a description section."""
    # Remove excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading/trailing whitespace per line while preserving structure
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines).strip()
    if len(text) > max_length:
        text = text[:max_length] + "\n..."
    return text


def parse_man_page(page_name: str, text: str) -> CommandDoc:
    """Parse a single man page into a CommandDoc."""
    sections = _split_sections(text)

    # Derive command name: "git-commit" → "git commit"
    command = page_name.replace("-", " ", 1) if "-" in page_name else page_name

    name_line = sections.get("NAME", "")
    synopsis = sections.get("SYNOPSIS", "")
    description = _clean_description(sections.get("DESCRIPTION", ""))
    options = _parse_options(sections.get("OPTIONS", ""))
    examples = _clean_description(sections.get("EXAMPLES", ""), max_length=1500)
    see_also = _extract_see_also(sections.get("SEE ALSO", ""))

    doc = CommandDoc(
        command=command,
        name_line=name_line,
        synopsis=synopsis,
        description=description,
        options=options,
        examples=examples,
        see_also=see_also,
        raw_sections=sections,
    )

    logger.debug("Parsed %s: %d options, %d see-also refs", command, len(options), len(see_also))
    return doc


def parse_tool_man_pages(
    tool_name: str,
    source_name: str,
    version: str,
    man_pages: dict[str, str],
) -> ToolDocs:
    """Parse all man pages for a tool into a ToolDocs structure."""
    commands: list[CommandDoc] = []
    top_level_desc = ""

    for page_name, text in sorted(man_pages.items()):
        doc = parse_man_page(page_name, text)
        commands.append(doc)

        # Capture top-level tool description
        if page_name == tool_name:
            if " - " in doc.name_line:
                top_level_desc = doc.name_line.split(" - ", 1)[1].strip().split("\n")[0]

    tool_docs = ToolDocs(
        tool_name=tool_name,
        source_name=source_name,
        version=version,
        description=top_level_desc,
        commands=commands,
    )

    logger.info(
        "Parsed %s: %d command man pages, version %s",
        tool_name,
        len(commands),
        version or "(unknown)",
    )
    return tool_docs


# ---------------------------------------------------------------------------
# --help output parser (cobra-style CLIs)
# ---------------------------------------------------------------------------

# Cobra section headers: "Usage:", "Aliases:", "Examples:", "Available Commands:",
# "Flags:", "Global Flags:", "Additional help topics:", "Use ..."
_HELP_SECTION_PATTERN = re.compile(
    r"^([A-Z][\w\s]*?):\s*$",
    re.MULTILINE,
)

# Only these section names are recognised as real cobra structural sections.
# Anything else (e.g., "Deploy Commands:", "Troubleshooting Commands:") is a
# category header that belongs to the description/preamble.
_KNOWN_HELP_SECTIONS = frozenset(
    {
        "Usage",
        "Aliases",
        "Examples",
        "Available Commands",
        "Commands",
        "Flags",
        "Options",
        "Global Flags",
        "Additional help topics",
        "Additional Commands",
    }
)


def _split_help_sections(text: str) -> tuple[str, dict[str, str]]:
    """Split cobra --help output into a preamble and named sections.

    Only recognised cobra section headers (see ``_KNOWN_HELP_SECTIONS``) are
    extracted.  Everything else — including kubectl-style category group
    headers like "Deploy Commands:" — stays in the preamble so it becomes
    part of the description.
    """
    sections: dict[str, str] = {}
    # Filter to only known cobra sections
    all_matches = list(_HELP_SECTION_PATTERN.finditer(text))
    matches = [m for m in all_matches if m.group(1).strip() in _KNOWN_HELP_SECTIONS]

    preamble = text[: matches[0].start()].strip() if matches else text.strip()

    for i, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections[name] = body
    return preamble, sections


def _parse_help_flags(flags_text: str) -> list[OptionInfo]:
    """Parse a cobra Flags / Options section into OptionInfo list.

    Handles two flag formats:

    1. **Standard cobra** (single-line)::

          -n, --namespace string   If present, the namespace scope
          -o, --output string      Output format (default "")

    2. **kubectl-style** (multi-line, colon-terminated)::

          -A, --all-namespaces=false:
              If present, list across all namespaces.

          --chunk-size=500:
              Return large lists in chunks rather than all at once.
    """
    options: list[OptionInfo] = []
    lines = flags_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip blank lines and "Use ..." footer lines
        if not stripped or stripped.startswith("Use "):
            i += 1
            continue

        # --- Format 2: kubectl multi-line flags ---
        # Flag line ends with ":" after a default value like "=false:" or "=500:"
        # Pattern: optional short flag, long flag with optional =default, colon
        ml_match = re.match(
            r"^\s+"
            r"((?:-\w,\s+)?"  # optional short flag: "-A, "
            r"--[\w-]+"  # long flag: "--all-namespaces"
            r"(?:=\S*)?)"  # optional =default: "=false"
            r":\s*$",  # trailing colon
            line,
        )
        if ml_match:
            flags = ml_match.group(1).strip()
            # Collect indented description lines that follow
            desc_lines: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                # Description lines are tab-indented or deeper-space-indented
                if next_line and (next_line.startswith("\t") or next_line.startswith("        ")):
                    desc_lines.append(next_line.strip())
                    i += 1
                elif not next_line.strip():
                    # Blank line — end of this flag's description
                    i += 1
                    break
                else:
                    break
            desc = " ".join(desc_lines).strip()
            if len(desc) > 300:
                desc = desc[:297] + "..."
            options.append(OptionInfo(flags=flags, description=desc))
            continue

        # --- Format 1: standard cobra single-line flags ---
        sl_match = re.match(
            r"^\s+"  # leading whitespace
            r"("  # capture full flags string
            r"(?:-\w,\s+)?"  # optional short flag: "-n, "
            r"--[\w-]+"  # long flag: "--namespace"
            r"(?:\s+\S+)?"  # optional type: " string"
            r")"
            r"\s{2,}"  # gap before description (2+ spaces)
            r"(.+)",  # description
            line,
        )
        if sl_match:
            flags = sl_match.group(1).strip()
            desc = sl_match.group(2).strip()
            if len(desc) > 300:
                desc = desc[:297] + "..."
            options.append(OptionInfo(flags=flags, description=desc))
            i += 1
            continue

        # Might be a flag without a description (rare)
        fm = re.match(r"^\s+((?:-\w,\s+)?--[\w-]+(?:\s+\S+)?)\s*$", line)
        if fm:
            options.append(OptionInfo(flags=fm.group(1).strip(), description=""))

        i += 1

    return options


def _extract_help_see_also(text: str) -> list[str]:
    """Extract referenced commands from "Use ... --help" or "Additional help" sections."""
    refs: list[str] = []
    # Match: Use "kubectl <subcommand> --help" for more information
    for m in re.finditer(r'"([\w][\w\s-]+?)(?:\s+--help)?"', text):
        ref = m.group(1).strip()
        if ref and len(ref) < 60:
            refs.append(ref)
    return refs


def parse_help_output(command_key: str, text: str) -> CommandDoc:
    """Parse a single cobra ``--help`` output into a CommandDoc."""
    preamble, sections = _split_help_sections(text)

    # Description: preamble text (before first section header)
    description = _clean_description(preamble)

    # Synopsis from "Usage:" section
    synopsis = sections.get("Usage", "")
    if synopsis:
        synopsis = " ".join(synopsis.split())

    # Examples
    examples = _clean_description(sections.get("Examples", ""), max_length=1500)

    # Flags — merge "Flags" and "Global Flags"
    options: list[OptionInfo] = []
    for key in ("Flags", "Options"):
        if key in sections:
            options.extend(_parse_help_flags(sections[key]))
    if "Global Flags" in sections:
        options.extend(_parse_help_flags(sections["Global Flags"]))

    # See also
    see_also: list[str] = []
    # Check the tail of the help output for "Use ... --help" lines
    tail = text.rsplit("\n", 5)[-5:] if "\n" in text else [text]
    for line in tail:
        see_also.extend(_extract_help_see_also(line))
    if "Additional help topics" in sections:
        see_also.extend(_extract_help_see_also(sections["Additional help topics"]))

    # Synthesize name_line in man-page format for compatibility with mapper
    # "kubectl create deployment - Create a deployment"
    summary_line = description.split("\n")[0].strip().rstrip(".") if description else ""
    if len(summary_line) > 120:
        summary_line = summary_line[:117] + "..."
    name_line = f"{command_key} - {summary_line}" if summary_line else command_key

    # Aliases
    aliases_text = sections.get("Aliases", "")

    doc = CommandDoc(
        command=command_key,
        name_line=name_line,
        synopsis=synopsis,
        description=description,
        options=options,
        examples=examples,
        see_also=see_also,
        raw_sections={k: v for k, v in sections.items()},
    )

    # Store aliases in raw_sections if present
    if aliases_text:
        doc.raw_sections["Aliases"] = aliases_text

    logger.debug(
        "Parsed help: %s (%d options, %d see-also refs)",
        command_key,
        len(options),
        len(see_also),
    )
    return doc


def parse_tool_help_pages(
    tool_name: str,
    source_name: str,
    version: str,
    help_pages: dict[str, str],
) -> ToolDocs:
    """Parse all --help pages for a tool into a ToolDocs structure."""
    commands: list[CommandDoc] = []
    top_level_desc = ""

    for command_key, text in sorted(help_pages.items()):
        doc = parse_help_output(command_key, text)
        commands.append(doc)

        # Capture top-level tool description
        if command_key == tool_name:
            if " - " in doc.name_line:
                top_level_desc = doc.name_line.split(" - ", 1)[1].strip().split("\n")[0]

    tool_docs = ToolDocs(
        tool_name=tool_name,
        source_name=source_name,
        version=version,
        description=top_level_desc,
        commands=commands,
    )

    logger.info(
        "Parsed %s: %d help pages, version %s",
        tool_name,
        len(commands),
        version or "(unknown)",
    )
    return tool_docs
