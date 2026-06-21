"""Knowledge Database API router.

CRUD + semantic search for user-created knowledge entries.
"""

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, UploadFile
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
from app.services.knowledge_service import knowledge_service


class ContextRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=30)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/entries", status_code=201)
def create_entry(request: CreateEntryRequest) -> EntryResponse:
    result = knowledge_service.create_entry(request)
    if result.get("_conflict"):
        result.pop("_conflict", None)
        raise HTTPException(
            status_code=409,
            detail={"message": "Entry with identical content already exists", "entry": result},
        )
    return EntryResponse(**result)


@router.post("/entries/batch", status_code=202)
def create_batch(request: BatchCreateRequest) -> BatchResponse:
    knowledge_service.process_batch(request.entries)
    return BatchResponse(
        job_id="sync",
        status="completed",
        entry_count=len(request.entries),
    )


@router.get("/entries/{entry_id}")
def get_entry(entry_id: str) -> EntryResponse:
    result = knowledge_service.get_entry(entry_id)
    if not result:
        raise HTTPException(status_code=404, detail="Entry not found")
    return EntryResponse(**result)


@router.put("/entries/{entry_id}")
def update_entry(entry_id: str, request: UpdateEntryRequest) -> EntryResponse:
    result = knowledge_service.update_entry(entry_id, request)
    if not result:
        raise HTTPException(status_code=404, detail="Entry not found")
    return EntryResponse(**result)


@router.delete("/entries/{entry_id}", status_code=204)
def delete_entry(entry_id: str) -> Response:
    knowledge_service.delete_entry(entry_id)
    return Response(status_code=204)


@router.delete("/entries")
def bulk_delete(request: BulkDeleteRequest) -> dict:
    return knowledge_service.delete_by_filter(request)


@router.post("/search")
def search(request: KnowledgeSearchRequest) -> SearchResponse:
    result = knowledge_service.search(request)
    return SearchResponse(**result)


@router.get("/tags")
def get_tags() -> dict:
    tags = knowledge_service.get_tags()
    return {"tags": tags}


class FilterRequest(BaseModel):
    content_type: str | None = None
    tags: list[str] | None = None
    project: str | None = None
    parent_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


@router.post("/filter")
def filter_entries(request: FilterRequest) -> dict:
    """Return entries matching filters without vector search."""
    return knowledge_service.filter_entries(
        limit=request.limit,
        content_type=request.content_type,
        tags=request.tags,
        project=request.project,
        parent_id=request.parent_id,
    )


@router.post("/search/smart")
def search_smart(request: KnowledgeSearchRequest) -> dict:
    result = knowledge_service.search(request)
    result["intent"] = {"refined_query": None, "was_refined": False}
    return result


@router.get("/stats")
def get_stats() -> StatsResponse:
    result = knowledge_service.get_stats()
    return StatsResponse(**result)


@router.post("/entries/{entry_id}/context")
def get_entry_context(entry_id: str, request: ContextRequest) -> dict:
    """Retrieve the most relevant passages from an entry for a given query."""
    result = knowledge_service.get_context(entry_id, request.query, top_k=request.top_k)
    if result is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return result


@router.post("/entries/{entry_id}/rechunk")
def rechunk_entry(entry_id: str) -> dict:
    """Re-create page chunks for an existing entry (for entries indexed before chunking was added)."""
    result = knowledge_service.rechunk_entry(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return result


@router.post("/extract")
async def extract_file(file: UploadFile) -> dict:
    """Extract text from an uploaded file (PDF, text, code, config)."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    ext = Path(file.filename).suffix.lower()
    size_bytes = 0
    pages = None

    if ext == ".pdf":
        text, pages = await _extract_pdf_upload(file)
    else:
        raw = await file.read()
        size_bytes = len(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, f"Cannot decode {file.filename} as text")

    if not text or not text.strip():
        raise HTTPException(422, f"No extractable text in {file.filename}")

    if not size_bytes:
        size_bytes = len(text.encode("utf-8"))

    metadata = {
        "filename": file.filename,
        "extension": ext,
        "size_bytes": size_bytes,
        "mime_type": file.content_type or None,
    }
    if pages is not None:
        metadata["pages"] = pages

    return {"text": text, "metadata": metadata}


async def _extract_pdf_upload(file: UploadFile) -> tuple[str, int]:
    """Extract text from a PDF upload using pdfplumber, fallback to pdftotext CLI."""
    import subprocess

    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None

    content = await file.read()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        tmp_path = Path(tmp.name)

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
                    return "\n\n".join(page_texts), num_pages
            except Exception as exc:
                logger.warning("pdfplumber extraction failed for %s: %s", file.filename, exc)

        # Fallback: CLI pdftotext
        try:
            proc = subprocess.run(
                ["pdftotext", str(tmp_path), "-"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout, None
        except Exception as exc:
            logger.warning("CLI pdftotext fallback failed for %s: %s", file.filename, exc)

    raise HTTPException(422, f"Could not extract text from {file.filename}")
