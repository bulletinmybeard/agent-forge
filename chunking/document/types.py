"""Intermediate dataclasses for the document mapper pipeline.

These are the parser's output and the mapper's input — decoupled from
Pydantic/Qdrant so the parser stays lightweight.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SectionInfo:
    """A single heading-delimited section within a document."""

    title: str = ""
    level: int = 1
    index: int = 0
    content: str = ""
    word_count: int = 0
    has_code_blocks: bool = False


@dataclass
class DocumentInfo:
    """Parsed representation of a single document file."""

    filename: str = ""
    file_path: str = ""
    document_name: str = ""
    document_type: str = "general"
    document_ext: str = ".md"
    sections: list[SectionInfo] = field(default_factory=list)
    front_matter: dict = field(default_factory=dict)


@dataclass
class DocumentSource:
    """A collection of documents under a single source name."""

    source_name: str = ""
    documents: list[DocumentInfo] = field(default_factory=list)
