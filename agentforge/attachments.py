"""Attachment handling — files, images, and documents for pipeline and chat calls."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


class AttachmentType(Enum):
    """Broad category for an attachment."""

    IMAGE = "image"
    TEXT = "text"
    DOCUMENT = "document"  # PDF, DOCX, etc. — needs extraction
    UNKNOWN = "unknown"


# Extensions we recognise out of the box
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".svg"}
_TEXT_EXTS = {
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".ini",
    ".toml",
    ".cfg",
    ".py",
    ".js",
    ".ts",
    ".sh",
    ".bash",
    ".sql",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
}
_DOC_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".rtf"}


def _detect_type(path: Path) -> AttachmentType:
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return AttachmentType.IMAGE
    if ext in _TEXT_EXTS:
        return AttachmentType.TEXT
    if ext in _DOC_EXTS:
        return AttachmentType.DOCUMENT

    # Fallback: use mimetypes
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        major = mime.split("/")[0]
        if major == "image":
            return AttachmentType.IMAGE
        if major == "text":
            return AttachmentType.TEXT

    return AttachmentType.UNKNOWN


@dataclass
class Attachment:
    """A file attached to a chat message or pipeline context.

    Supports three ways to provide content:

    1. **File path** — ``Attachment("screenshot.png")`` — content is read lazily.
    2. **Raw bytes** — ``Attachment(data=b"...", name="img.png")`` — already in memory.
    3. **Text content** — ``Attachment(text="...", name="notes.txt")`` — inline text.

    The ``type`` is auto-detected from the file extension but can be overridden.
    """

    # Identity (normalized to Path in __post_init__)
    path: Path | None = None
    name: str = ""

    # Inline content (alternative to path)
    data: bytes | None = None
    text: str | None = None

    # Type (auto-detected if not given)
    type: AttachmentType | None = None

    # Extracted text content (populated by extraction steps / helpers)
    extracted_text: str | None = None

    # Arbitrary metadata
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Resolve path
        if self.path is not None:
            self.path = Path(self.path)
            if not self.name:
                self.name = self.path.name

        # Auto-detect type
        if self.type is None:
            if self.path:
                self.type = _detect_type(self.path)
            elif self.text is not None:
                self.type = AttachmentType.TEXT
            elif self.name:
                self.type = _detect_type(Path(self.name))
            else:
                self.type = AttachmentType.UNKNOWN

        assert self.type is not None
        logger.debug("Attachment: name=%s type=%s", self.name, self.type.value)

    # -- properties ---------------------------------------------------------

    @property
    def is_image(self) -> bool:
        return self.type == AttachmentType.IMAGE

    @property
    def is_text(self) -> bool:
        return self.type == AttachmentType.TEXT

    @property
    def is_document(self) -> bool:
        return self.type == AttachmentType.DOCUMENT

    # -- content access -----------------------------------------------------

    def read_bytes(self) -> bytes:
        """Return raw bytes — from ``data`` or by reading the file."""
        if self.data is not None:
            return self.data
        if self.path and self.path.exists():
            return self.path.read_bytes()
        raise FileNotFoundError(f"No data and file not found: {self.path}")

    def read_text(self, encoding: str = "utf-8") -> str:
        """Return text content — from ``text``, ``extracted_text``, or by reading the file."""
        if self.text is not None:
            return self.text
        if self.extracted_text is not None:
            return self.extracted_text
        if self.path and self.path.exists():
            return self.path.read_text(encoding=encoding)
        raise FileNotFoundError(f"No text content and file not found: {self.path}")

    # -- Ollama integration -------------------------------------------------

    def for_ollama_message(self) -> bytes | str | None:
        """Return the value to put in the ``images`` list of an Ollama message.

        Ollama accepts file paths (str) or raw bytes for images.
        Returns *None* for non-image attachments.
        """
        if not self.is_image:
            return None

        # Prefer file path (Ollama reads it directly — no base64 overhead)
        if self.path and self.path.exists():
            return str(self.path)

        # Fall back to raw bytes
        if self.data is not None:
            return self.data

        return None

    def as_context_text(self) -> str | None:
        """Return a text representation suitable for injecting into a prompt.

        Images return *None* (they go via Ollama's ``images`` field instead).
        Text and document types return their readable content.
        """
        if self.is_image:
            return None

        try:
            return self.read_text()
        except (FileNotFoundError, UnicodeDecodeError):
            return None

    # -- repr ---------------------------------------------------------------

    def __repr__(self) -> str:
        src = str(self.path) if self.path else "(inline)"
        kind = self.type.value if self.type is not None else "unknown"
        return f"<Attachment name={self.name!r} type={kind} src={src}>"
