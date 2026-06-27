"""Knowledge Database API router.

CRUD + semantic search for user-created knowledge entries.
"""

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.models.knowledge import (
    BatchCreateRequest,
    BatchResponse,
    BulkDeleteRequest,
    CreateEntryRequest,
    EntryResponse,
    KnowledgeSearchRequest,
    SearchResponse,
    StatsResponse,
    UpdateEntryRequest,
)
from app.services.knowledge_file_service import knowledge_file_service
from app.services.knowledge_registry import knowledge_service_dependency
from app.services.knowledge_service import KnowledgeService


class ContextRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=30)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/entries", status_code=201)
def create_entry(
    request: CreateEntryRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> EntryResponse:
    result = knowledge_service.create_entry(request)
    if result.get("_conflict"):
        result.pop("_conflict", None)
        raise HTTPException(
            status_code=409,
            detail={"message": "Entry with identical content already exists", "entry": result},
        )
    return EntryResponse(**result)


@router.post("/entries/batch", status_code=202)
def create_batch(
    request: BatchCreateRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> BatchResponse:
    knowledge_service.process_batch(request.entries)
    return BatchResponse(
        job_id="sync",
        status="completed",
        entry_count=len(request.entries),
    )


@router.get("/entries/{entry_id}")
def get_entry(
    entry_id: str,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> EntryResponse:
    result = knowledge_service.get_entry(entry_id)
    if not result:
        raise HTTPException(status_code=404, detail="Entry not found")
    return EntryResponse(**result)


@router.put("/entries/{entry_id}")
def update_entry(
    entry_id: str,
    request: UpdateEntryRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> EntryResponse:
    result = knowledge_service.update_entry(entry_id, request)
    if not result:
        raise HTTPException(status_code=404, detail="Entry not found")
    return EntryResponse(**result)


@router.delete("/entries/{entry_id}", status_code=204)
def delete_entry(
    entry_id: str,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> Response:
    knowledge_service.delete_entry(entry_id)
    return Response(status_code=204)


@router.delete("/entries")
def bulk_delete(
    request: BulkDeleteRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    return knowledge_service.delete_by_filter(request)


@router.post("/search")
def search(
    request: KnowledgeSearchRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> SearchResponse:
    result = knowledge_service.search(request)
    return SearchResponse(**result)


@router.get("/tags")
def get_tags(
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    tags = knowledge_service.get_tags()
    return {"tags": tags}


class FilterRequest(BaseModel):
    content_type: str | None = None
    tags: list[str] | None = None
    project: str | None = None
    parent_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


@router.post("/filter")
def filter_entries(
    request: FilterRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    """Return entries matching filters without vector search."""
    return knowledge_service.filter_entries(
        limit=request.limit,
        content_type=request.content_type,
        tags=request.tags,
        project=request.project,
        parent_id=request.parent_id,
    )


@router.get("/list")
def list_entries(
    limit: int = 2000,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    """Slim listing for the browse view: entry metadata only, no content body."""
    return knowledge_service.list_overview(limit=limit)


@router.post("/search/smart")
def search_smart(
    request: KnowledgeSearchRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    result = knowledge_service.search(request)
    result["intent"] = {"refined_query": None, "was_refined": False}
    return result


@router.get("/stats")
def get_stats(
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> StatsResponse:
    result = knowledge_service.get_stats()
    return StatsResponse(**result)


@router.post("/entries/{entry_id}/context")
def get_entry_context(
    entry_id: str,
    request: ContextRequest,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    """Retrieve the most relevant passages from an entry for a given query."""
    result = knowledge_service.get_context(entry_id, request.query, top_k=request.top_k)
    if result is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return result


@router.post("/entries/{entry_id}/rechunk")
def rechunk_entry(
    entry_id: str,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    """Re-create page chunks for an existing entry (for entries indexed before chunking was added)."""
    result = knowledge_service.rechunk_entry(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return result


@router.head("/entries/{entry_id}/file")
def head_entry_file(entry_id: str) -> Response:
    """Return 200 when the original attachment file is stored, else 404."""
    if not knowledge_file_service.exists(entry_id):
        raise HTTPException(status_code=404, detail="Original file not stored")
    return Response(status_code=200)


@router.get("/entries/{entry_id}/file")
def get_entry_file(entry_id: str) -> FileResponse:
    """Download the original attachment file when it was stored at index time."""
    path, filename, mime = knowledge_file_service.resolve_download(entry_id)
    return FileResponse(path, filename=filename, media_type=mime or "application/octet-stream")


@router.post("/entries/{entry_id}/file", status_code=201)
async def upload_entry_file(
    entry_id: str,
    file: UploadFile,
    knowledge_service: KnowledgeService = Depends(knowledge_service_dependency),
) -> dict:
    """Store the original binary for an existing entry."""
    entry = knowledge_service.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    file_meta = await knowledge_file_service.save(entry_id, file)
    merged_meta = {**(entry.get("metadata") or {}), **file_meta}
    knowledge_service.update_entry(entry_id, UpdateEntryRequest(metadata=merged_meta))
    return {"stored": True, "metadata": file_meta}


@router.post("/extract")
async def extract_file(file: UploadFile) -> dict:
    """Extract text from an uploaded file (PDF, text, code, config)."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    ext = Path(file.filename).suffix.lower()
    size_bytes = 0
    pages = None

    if ext == ".pdf":
        text, pages, size_bytes = await _extract_pdf_upload(file)
    else:
        raw = await file.read()
        size_bytes = len(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, f"Cannot decode {file.filename} as text")

    if not text or not text.strip():
        raise HTTPException(422, f"No extractable text in {file.filename}")

    extracted_bytes = len(text.encode("utf-8"))
    if not size_bytes:
        size_bytes = extracted_bytes

    metadata = {
        "filename": file.filename,
        "extension": ext,
        "size_bytes": size_bytes,
        "extracted_bytes": extracted_bytes,
        "mime_type": file.content_type or None,
    }
    if pages is not None:
        metadata["pages"] = pages

    return {"text": text, "metadata": metadata}


_LARGE_PDF_BYTES = 5 * 1024 * 1024
_PDFTOTEXT_TIMEOUT_BASE = 120
_PDFTOTEXT_TIMEOUT_MAX = 600


def _pdftotext_timeout(file_bytes: int) -> int:
    """Scale CLI timeout with upload size."""
    return min(_PDFTOTEXT_TIMEOUT_MAX, max(_PDFTOTEXT_TIMEOUT_BASE, file_bytes // (512 * 1024)))


def _format_pdftotext_pages(raw: str) -> tuple[str, int]:
    """Turn pdftotext stdout into page-marked text for downstream chunking."""
    pages = [p.strip() for p in raw.split("\f") if p.strip()]
    if not pages:
        return "", 0
    if len(pages) == 1:
        return pages[0], 1
    marked = [f"--- Page {i} ---\n{text}" for i, text in enumerate(pages, 1)]
    return "\n\n".join(marked), len(pages)


def _run_pdftotext(tmp_path: Path, timeout: int) -> tuple[str, int] | None:
    import subprocess

    try:
        proc = subprocess.run(
            ["pdftotext", str(tmp_path), "-"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("CLI pdftotext failed for %s: %s", tmp_path.name, exc)
        return None

    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return _format_pdftotext_pages(proc.stdout)


async def _extract_pdf_upload(file: UploadFile) -> tuple[str, int, int]:
    """Extract text from a PDF upload (pdftotext for large files, pdfplumber otherwise)."""
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None

    content = await file.read()
    upload_bytes = len(content)
    pdftotext_timeout = _pdftotext_timeout(upload_bytes)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        tmp_path = Path(tmp.name)

        # Large manuals: pdftotext is ~30–50× faster than per-page pdfplumber.
        if upload_bytes >= _LARGE_PDF_BYTES:
            fast = _run_pdftotext(tmp_path, pdftotext_timeout)
            if fast and fast[0].strip():
                logger.info(
                    "pdftotext extracted %s (%d bytes, %d pages)",
                    file.filename,
                    upload_bytes,
                    fast[1],
                )
                return fast[0], fast[1], upload_bytes

        if pdfplumber:
            try:
                page_texts = []
                with pdfplumber.open(tmp_path) as pdf:
                    for i, page in enumerate(pdf.pages, 1):
                        tables = page.extract_tables()
                        table_text = ""
                        if tables:
                            for table in tables:
                                rows = []
                                for row in table:
                                    cells = [str(c).strip() if c else "" for c in row]
                                    rows.append(" | ".join(cells))
                                table_text += "\n".join(rows) + "\n"

                        text = page.extract_text() or ""
                        combined = table_text.strip() if table_text.strip() else text.strip()
                        if combined:
                            page_texts.append(f"--- Page {i} ---\n{combined}")

                    num_pages = len(pdf.pages)

                if page_texts:
                    return "\n\n".join(page_texts), num_pages, upload_bytes
            except Exception as exc:
                logger.warning("pdfplumber extraction failed for %s: %s", file.filename, exc)

        fallback = _run_pdftotext(tmp_path, pdftotext_timeout)
        if fallback and fallback[0].strip():
            return fallback[0], fallback[1], upload_bytes

    raise HTTPException(422, f"Could not extract text from {file.filename}")
