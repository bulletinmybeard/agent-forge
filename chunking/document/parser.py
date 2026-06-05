"""Markdown document parser.

Reads .md files, splits them into heading-delimited sections, and
produces intermediate DocumentInfo / SectionInfo dataclasses for the
mapper stage.

Supports:
- Heading-based splitting (configurable level, default ##)
- YAML front-matter extraction
- Code block preservation (never splits mid-block)
- Auto-detection of document_type from filename
- Long-section splitting at sub-headings or paragraph boundaries
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from chunking.document.types import DocumentInfo, DocumentSource, SectionInfo

logger = logging.getLogger(__name__)

# Filename patterns → document_type.
# Checked in order; first match wins.  Patterns use word boundaries
# so they match both standalone filenames (CHANGELOG.md) and prefixed
# ones (my-nl-ix_CHANGELOG.md).
_DOCUMENT_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:^|[_\-])changelog", re.IGNORECASE), "changelog"),
    (re.compile(r"(?:^|[_\-])changes", re.IGNORECASE), "changelog"),
    (re.compile(r"(?:^|[_\-])history", re.IGNORECASE), "changelog"),
    (re.compile(r"(?:^|[_\-])release", re.IGNORECASE), "release-notes"),
    (re.compile(r"(?:^|[_\-])readme", re.IGNORECASE), "readme"),
    (re.compile(r"(?:^|[_\-])install", re.IGNORECASE), "guide"),
    (re.compile(r"(?:^|[_\-])setup", re.IGNORECASE), "guide"),
    (re.compile(r"(?:^|[_\-])getting[-_\s]?started", re.IGNORECASE), "guide"),
    (re.compile(r"(?:^|[_\-])contribut", re.IGNORECASE), "guide"),
    (re.compile(r"(?:^|[_\-])migration", re.IGNORECASE), "guide"),
    (re.compile(r"(?:^|[_\-])upgrade", re.IGNORECASE), "guide"),
]

# Default split level: sections are created at this heading level.
DEFAULT_SPLIT_LEVEL = 2

# Maximum section length (characters).  Sections exceeding this are
# split further at sub-headings or paragraph boundaries.
MAX_SECTION_CHARS = 2000

# Minimum section length worth keeping as a standalone chunk.
MIN_SECTION_CHARS = 50


def detect_document_type(filename: str) -> str:
    """Infer document_type from the filename stem."""
    stem = Path(filename).stem
    for pattern, doc_type in _DOCUMENT_TYPE_PATTERNS:
        if pattern.search(stem):
            return doc_type
    return "general"


def _extract_front_matter(text: str) -> tuple[dict, str]:
    """Extract YAML front matter (--- delimited) from the top of the file.

    Returns (metadata_dict, remaining_text).  If no front matter is
    found, returns ({}, original_text).
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_block = text[3:end].strip()
    remaining = text[end + 4 :].lstrip("\n")

    # Simple key: value parsing (no nested YAML — keeps dependency-free).
    metadata: dict = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                metadata[key] = value

    return metadata, remaining


def _count_words(text: str) -> int:
    """Fast word count (split on whitespace)."""
    return len(text.split())


def _has_code_blocks(text: str) -> bool:
    """Check whether text contains fenced code blocks."""
    return "```" in text


def _split_by_headings(text: str, split_level: int) -> list[SectionInfo]:
    """Split document text into sections at heading boundaries.

    Headings at or above ``split_level`` (e.g., ## for level 2) start
    a new section.  Content before the first heading becomes a preamble
    section with an empty title.

    Code blocks (``` ... ```) are treated as opaque — heading-like
    lines inside them are ignored.
    """
    # Regex matching headings at the split level or above.
    # e.g., for split_level=2 this matches # and ##.
    heading_re = re.compile(rf"^(#{{1,{split_level}}})\s+(.+)$", re.MULTILINE)

    sections: list[SectionInfo] = []
    current_title = ""
    current_level = 0
    current_start = 0
    section_index = 0

    # Find heading positions, but skip those inside fenced code blocks.
    in_code_block = False
    heading_positions: list[tuple[int, int, str, int]] = []  # (start, end, title, level)

    for line_match in re.finditer(r"^.*$", text, re.MULTILINE):
        line = line_match.group()
        line_start = line_match.start()

        # Toggle code block state.
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        hm = heading_re.match(line)
        if hm:
            level = len(hm.group(1))
            title = hm.group(2).strip()
            heading_positions.append((line_start, line_match.end(), title, level))

    # Build sections from heading positions.
    for i, (h_start, h_end, title, level) in enumerate(heading_positions):
        # Content before the first heading is a preamble section.
        if i == 0 and h_start > 0:
            preamble = text[:h_start].strip()
            if preamble and len(preamble) >= MIN_SECTION_CHARS:
                sections.append(
                    SectionInfo(
                        title="",
                        level=0,
                        index=section_index,
                        content=preamble,
                        word_count=_count_words(preamble),
                        has_code_blocks=_has_code_blocks(preamble),
                    )
                )
                section_index += 1

        # Close the previous section.
        if i > 0:
            prev_content = text[current_start:h_start].strip()
            if prev_content and len(prev_content) >= MIN_SECTION_CHARS:
                sections.append(
                    SectionInfo(
                        title=current_title,
                        level=current_level,
                        index=section_index,
                        content=prev_content,
                        word_count=_count_words(prev_content),
                        has_code_blocks=_has_code_blocks(prev_content),
                    )
                )
                section_index += 1

        current_title = title
        current_level = level
        current_start = h_start

    # Final section (from last heading to EOF).
    if heading_positions:
        final_content = text[current_start:].strip()
        if final_content and len(final_content) >= MIN_SECTION_CHARS:
            sections.append(
                SectionInfo(
                    title=current_title,
                    level=current_level,
                    index=section_index,
                    content=final_content,
                    word_count=_count_words(final_content),
                    has_code_blocks=_has_code_blocks(final_content),
                )
            )
    elif text.strip():
        # No headings at all — treat entire document as one section.
        sections.append(
            SectionInfo(
                title="",
                level=0,
                index=0,
                content=text.strip(),
                word_count=_count_words(text),
                has_code_blocks=_has_code_blocks(text),
            )
        )

    return sections


def _split_long_section(section: SectionInfo, max_chars: int, base_index: int) -> list[SectionInfo]:
    """Split an oversized section at sub-heading or paragraph boundaries.

    Returns a list of smaller SectionInfo objects.  If the section is
    already within the limit, returns it unchanged (as a one-element list).
    """
    if len(section.content) <= max_chars:
        return [section]

    content = section.content
    parts: list[str] = []

    # Try splitting at sub-headings first (any ### or deeper).
    sub_heading_re = re.compile(r"^(#{3,6})\s+(.+)$", re.MULTILINE)
    sub_matches = list(sub_heading_re.finditer(content))

    if sub_matches:
        prev = 0
        for m in sub_matches:
            if m.start() > prev:
                parts.append(content[prev : m.start()].strip())
            prev = m.start()
        parts.append(content[prev:].strip())
    else:
        # Fall back to paragraph splitting (double newline).
        raw_parts = re.split(r"\n\n+", content)
        # Recombine small paragraphs to avoid tiny chunks.
        current = ""
        for p in raw_parts:
            if current and len(current) + len(p) + 2 > max_chars:
                parts.append(current.strip())
                current = p
            else:
                current = current + "\n\n" + p if current else p
        if current.strip():
            parts.append(current.strip())

    # Build SectionInfo for each part.
    result: list[SectionInfo] = []
    idx = base_index
    for i, part in enumerate(parts):
        if not part or len(part) < MIN_SECTION_CHARS:
            continue
        # First part keeps the original title; subsequent parts get "(cont.)" suffix.
        title = section.title if i == 0 else f"{section.title} (cont.)"
        result.append(
            SectionInfo(
                title=title,
                level=section.level,
                index=idx,
                content=part,
                word_count=_count_words(part),
                has_code_blocks=_has_code_blocks(part),
            )
        )
        idx += 1

    return result if result else [section]


def parse_markdown_file(
    filepath: Path,
    source_root: Path | None = None,
    split_level: int = DEFAULT_SPLIT_LEVEL,
    max_section_chars: int = MAX_SECTION_CHARS,
) -> DocumentInfo:
    """Parse a single Markdown file into a DocumentInfo."""
    raw = filepath.read_text(encoding="utf-8")
    front_matter, body = _extract_front_matter(raw)

    filename = filepath.name
    doc_name = filepath.stem
    doc_ext = filepath.suffix
    doc_type = front_matter.get("type", detect_document_type(filename))

    rel_path = ""
    if source_root:
        try:
            rel_path = str(filepath.relative_to(source_root))
        except ValueError:
            rel_path = str(filepath)

    # Split into sections.
    raw_sections = _split_by_headings(body, split_level)

    # Sub-split oversized sections.
    sections: list[SectionInfo] = []
    idx = 0
    for sec in raw_sections:
        split_parts = _split_long_section(sec, max_section_chars, idx)
        for part in split_parts:
            part.index = idx
            sections.append(part)
            idx += 1

    logger.info(
        "Parsed %s: %d sections, document_type=%s",
        filename,
        len(sections),
        doc_type,
    )

    return DocumentInfo(
        filename=filename,
        file_path=rel_path,
        document_name=doc_name,
        document_type=doc_type,
        document_ext=doc_ext,
        sections=sections,
        front_matter=front_matter,
    )


def parse_directory(
    input_dir: Path,
    source_name: str,
    split_level: int = DEFAULT_SPLIT_LEVEL,
    max_section_chars: int = MAX_SECTION_CHARS,
) -> DocumentSource:
    """Parse all Markdown files in a directory tree."""
    md_files = sorted(input_dir.rglob("*.md"))

    if not md_files:
        logger.warning("No .md files found in %s", input_dir)
        return DocumentSource(source_name=source_name, documents=[])

    logger.info("Found %d Markdown file(s) in %s", len(md_files), input_dir)

    documents: list[DocumentInfo] = []
    for md_path in md_files:
        doc = parse_markdown_file(
            md_path,
            source_root=input_dir,
            split_level=split_level,
            max_section_chars=max_section_chars,
        )
        if doc.sections:
            documents.append(doc)
        else:
            logger.debug("Skipping %s (no sections after parsing)", md_path.name)

    return DocumentSource(source_name=source_name, documents=documents)
