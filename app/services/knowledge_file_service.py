from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import settings

logger = logging.getLogger(__name__)

_ENTRY_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class KnowledgeFileService:
    """Filesystem backing store for original attachments."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base = Path(base_dir or settings.knowledge.files_dir)

    def _entry_dir(self, entry_id: str) -> Path:
        if not _ENTRY_ID_RE.match(entry_id):
            raise HTTPException(400, "Invalid entry id")
        return self._base

    def _bin_path(self, entry_id: str) -> Path:
        return self._entry_dir(entry_id) / f"{entry_id}.bin"

    def _meta_path(self, entry_id: str) -> Path:
        return self._entry_dir(entry_id) / f"{entry_id}.meta.json"

    def exists(self, entry_id: str) -> bool:
        return self._bin_path(entry_id).is_file()

    def read_meta(self, entry_id: str) -> dict | None:
        path = self._meta_path(entry_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read attachment meta for %s: %s", entry_id, exc)
            return None

    def resolve_download(self, entry_id: str) -> tuple[Path, str, str | None]:
        bin_path = self._bin_path(entry_id)
        if not bin_path.is_file():
            raise HTTPException(404, "Original file not stored for this entry")
        meta = self.read_meta(entry_id) or {}
        filename = meta.get("filename") or f"{entry_id}.bin"
        mime = meta.get("mime_type")
        return bin_path, filename, mime

    async def save(self, entry_id: str, file: UploadFile) -> dict:
        """Persist upload."""
        if not file.filename:
            raise HTTPException(400, "No filename provided")

        self._base.mkdir(parents=True, exist_ok=True)
        raw = await file.read()
        if not raw:
            raise HTTPException(400, "Empty file")

        max_bytes = settings.knowledge.max_attachment_bytes
        if len(raw) > max_bytes:
            raise HTTPException(413, f"File exceeds limit of {max_bytes} bytes")

        self._bin_path(entry_id).write_bytes(raw)
        meta = {
            "filename": Path(file.filename).name,
            "mime_type": file.content_type or None,
            "size_bytes": len(raw),
        }
        self._meta_path(entry_id).write_text(json.dumps(meta, indent=2))
        logger.info("Stored original file for entry %s (%d bytes)", entry_id, len(raw))
        return {"original_file": True, **meta}

    def delete(self, entry_id: str) -> None:
        for path in (self._bin_path(entry_id), self._meta_path(entry_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to delete attachment file %s: %s", path, exc)


knowledge_file_service = KnowledgeFileService()
