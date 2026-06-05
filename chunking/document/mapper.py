"""Document mapper — convert parsed DocumentSource into Qdrant chunk models.

Takes the parser's intermediate dataclasses and produces:
- One ``DocumentSummaryChunk`` per source (overview of all documents).
- One ``DocumentSectionChunk`` per heading-delimited section.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

from chunking.document.types import DocumentInfo, DocumentSource, SectionInfo
from chunking.models import (
    ChunkType,
    DocumentSectionChunk,
    DocumentSectionPayload,
    DocumentSummaryChunk,
    DocumentSummaryPayload,
    SourceType,
)

logger = logging.getLogger(__name__)

# Words excluded from tag inference.
_TAG_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "are",
        "was",
        "were",
        "been",
        "have",
        "has",
        "had",
        "not",
        "but",
        "all",
        "can",
        "will",
        "just",
        "more",
        "also",
        "into",
        "than",
        "then",
        "when",
        "what",
        "which",
        "who",
        "how",
        "its",
        "you",
        "your",
        "use",
        "used",
        "using",
        "about",
        "each",
        "other",
        "some",
        "new",
        "now",
        "may",
        "see",
        "set",
        "get",
        "add",
        "run",
        "let",
        "any",
    }
)

# Maximum number of tags per chunk.
_MAX_TAGS = 15


def _sha256(text: str) -> str:
    """SHA256 hex digest of a text string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(text: str) -> str:
    """Convert a heading or filename into a filesystem/ID-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80] if slug else "untitled"


def _infer_tags_from_text(text: str) -> list[str]:
    """Extract meaningful tags from a text string."""
    words = re.findall(r"[A-Za-z][a-z]{2,}", text)
    tags: set[str] = set()
    for w in words:
        w_lower = w.lower()
        if len(w_lower) > 2 and w_lower not in _TAG_STOP_WORDS:
            tags.add(w_lower)
    return sorted(tags)[:_MAX_TAGS]


def _infer_tags_for_section(section: SectionInfo, doc: DocumentInfo) -> list[str]:
    """Generate enrichment tags for a document section."""
    tags: set[str] = set()

    # From document type.
    if doc.document_type and doc.document_type != "general":
        tags.add(doc.document_type)

    # From document name (split on separators).
    name_parts = re.split(r"[_\-\s.]+", doc.document_name.lower())
    tags.update(p for p in name_parts if len(p) > 2 and p not in _TAG_STOP_WORDS)

    # From section title.
    if section.title:
        title_tags = _infer_tags_from_text(section.title)
        tags.update(title_tags)

    # From content (limited to first 500 chars to keep it fast).
    content_preview = section.content[:500]
    content_tags = _infer_tags_from_text(content_preview)
    tags.update(content_tags[:5])

    return sorted(tags)[:_MAX_TAGS]


def _build_section_text(section: SectionInfo, doc: DocumentInfo, source_name: str) -> str:
    """Build the natural-language text field for embedding.

    Includes a structural prefix so the embedding model gets context
    about where this section lives.
    """
    lines = [
        f"Document: {doc.filename} ({doc.document_type})",
        f"Source: {source_name} (document)",
    ]
    if section.title:
        lines.append(f"Section: {section.title}")

    lines.append("")
    lines.append(section.content)

    return "\n".join(lines)


def map_documents_to_chunks(
    source: DocumentSource,
) -> tuple[DocumentSummaryChunk, list[DocumentSectionChunk]]:
    """Convert a parsed DocumentSource into chunk models."""
    source_name = source.source_name
    now = datetime.utcnow()

    # ── Summary chunk ────────────────────────────────────────────────
    all_doc_names = [d.filename for d in source.documents]
    all_doc_types = sorted({d.document_type for d in source.documents})
    total_sections = sum(len(d.sections) for d in source.documents)

    summary_text_lines = [
        f"Document source: {source_name}",
        f"Documents: {len(source.documents)}",
        f"Total sections: {total_sections}",
        f"Document types: {', '.join(all_doc_types)}",
        "",
        "Files:",
    ]
    for doc in source.documents:
        summary_text_lines.append(f"  - {doc.filename} ({doc.document_type}, {len(doc.sections)} sections)")

    summary_text = "\n".join(summary_text_lines)
    summary_id = f"{source_name}:doc-summary"

    summary_chunk = DocumentSummaryChunk(
        source_type=SourceType.DOCUMENT,
        source_name=source_name,
        chunk_id=summary_id,
        chunk_type=ChunkType.DOCUMENT_SUMMARY,
        text=summary_text,
        content_hash=_sha256(summary_text),
        payload=DocumentSummaryPayload(
            source_name=source_name,
            chunk_id=summary_id,
            document_count=len(source.documents),
            section_count=total_sections,
            document_names=all_doc_names,
            document_types=all_doc_types,
            tags=sorted({dt for dt in all_doc_types if dt != "general"})[:_MAX_TAGS],
            content_hash=_sha256(summary_text),
            last_indexed=now,
        ),
    )

    # ── Section chunks ───────────────────────────────────────────────
    section_chunks: list[DocumentSectionChunk] = []

    for doc in source.documents:
        doc_slug = _slugify(doc.document_name)

        for section in doc.sections:
            section_slug = _slugify(section.title) if section.title else "preamble"
            chunk_id = f"{source_name}:section:{doc_slug}:{section.index:04d}-{section_slug}"

            text = _build_section_text(section, doc, source_name)
            tags = _infer_tags_for_section(section, doc)

            section_chunks.append(
                DocumentSectionChunk(
                    source_type=SourceType.DOCUMENT,
                    source_name=source_name,
                    chunk_id=chunk_id,
                    chunk_type=ChunkType.DOCUMENT_SECTION,
                    text=text,
                    content_hash=_sha256(text),
                    payload=DocumentSectionPayload(
                        source_name=source_name,
                        chunk_id=chunk_id,
                        document_name=doc.document_name,
                        document_type=doc.document_type,
                        document_ext=doc.document_ext,
                        file_path=doc.file_path,
                        section_title=section.title,
                        section_level=section.level,
                        section_index=section.index,
                        word_count=section.word_count,
                        has_code_blocks=section.has_code_blocks,
                        tags=tags,
                        content_hash=_sha256(text),
                        last_indexed=now,
                    ),
                )
            )

    logger.info(
        "Mapped %s: 1 summary + %d section chunks from %d documents",
        source_name,
        len(section_chunks),
        len(source.documents),
    )

    return summary_chunk, section_chunks
