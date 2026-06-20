"""Knowledge Database API router.

CRUD + semantic search for user-created knowledge entries.
"""

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, UploadFile

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


@router.post("/search/smart")
def search_smart(request: KnowledgeSearchRequest) -> dict:
    result = knowledge_service.search(request)
    result["intent"] = {"refined_query": None, "was_refined": False}
    return result


@router.get("/stats")
def get_stats() -> StatsResponse:
    result = knowledge_service.get_stats()
    return StatsResponse(**result)


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
