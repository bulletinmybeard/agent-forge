import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.services.dedup_service import dedup_service
from app.services.indexer_service import indexer_service
from app.services.vector_service import vector_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/indexer", tags=["indexer"])

# Path segments that become directory names on the disk. Reject anything that isn't
# a plain name so a client can't traverse out of the chunks dir
# (e.g., source_type="../../../app") and write arbitrary files.
_SAFE_SEGMENT = re.compile(r"^[\w.-]+$")


def _safe_segment(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value or value in (".", "..") or not _SAFE_SEGMENT.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field}: must match [A-Za-z0-9_.-] and not be '.'/'..'")
    return value


@router.get("/sources")
def list_sources() -> dict:
    """List all discovered knowledge sources and their chunk counts."""
    sources = indexer_service.discover_sources()
    return {"sources": sources, "total": len(sources)}


@router.get("/apis")
def list_apis() -> dict:
    """List all discovered sources (backward-compatible alias)."""
    sources = indexer_service.discover_sources()
    return {"apis": sources, "total": len(sources)}


@router.get("/documents")
def list_documents() -> dict:
    """List unique document names across all indexed sources."""
    documents = indexer_service.discover_documents()
    return {
        "documents": documents,
        "total": len(documents),
        "stoplist": settings.chunking.document_lookup_stoplist,
    }


@router.post("/index/{api_name}")
def index_api(
    api_name: str,
    version: str | None = Query(default=None, description="Specific version to index"),
    clean: bool = Query(default=False, description="Delete existing points before indexing"),
    source_type: str | None = Query(
        default=None, description="Source type (e.g., 'openapi'). Auto-discovers if omitted."
    ),
    batch_size: int | None = Query(default=None, description="Override embedding batch size (default: config value)"),
    embed_timeout: float | None = Query(
        default=None, description="Override Ollama embed timeout in seconds (default: 600)"
    ),
) -> dict:
    """Index chunks for a specific source into Qdrant."""
    return indexer_service.index_api(
        api_name,
        version=version,
        clean=clean,
        source_type=source_type,
        batch_size=batch_size,
        embed_timeout=embed_timeout,
    )


@router.post("/index-all")
def index_all(
    clean: bool = Query(default=False, description="Delete existing points for each API before indexing"),
) -> dict:
    """Index all discovered APIs into Qdrant."""
    results = indexer_service.index_all(clean=clean)
    total_indexed = sum(r.get("indexed", 0) for r in results)
    total_errors = sum(r.get("errors", 0) for r in results)
    return {
        "results": results,
        "total_indexed": total_indexed,
        "total_errors": total_errors,
    }


class UploadChunk(BaseModel):
    chunk_id: str
    text: str
    content_hash: str | None = None
    payload: dict = {}


class UploadRequest(BaseModel):
    source_type: str = "document"
    version: str | None = None
    clean: bool = True
    chunks: list[UploadChunk]


@router.post("/upload/{api_name}")
def upload_source(api_name: str, req: UploadRequest, background_tasks: BackgroundTasks) -> dict:
    """Write client-supplied chunks to the chunks dir, then index them.

    Lets a remote client (e.g., Felix) index a source over HTTP without
    filesystem access to the indexer host. Chunks land in the same on-disk
    layout the mappers produce, so the normal index pipeline picks them up.

    Embedding runs as a BACKGROUND task so the HTTP request returns immediately.
    A large catalog can take minutes to embed and would otherwise time out the client.
    The caller polls /indexer/collection for the resulting point count.
    """
    version = req.version or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source_type = _safe_segment(req.source_type, "source_type")
    api_name = _safe_segment(api_name, "api_name")
    version = _safe_segment(version, "version")

    chunks_root = Path(settings.indexer.chunks_dir).resolve()
    base = (chunks_root / source_type / api_name / f"v{version}").resolve()
    # Defence in depth: even with the segment regex, confirm we stayed inside.
    if not base.is_relative_to(chunks_root):
        raise HTTPException(status_code=400, detail="Resolved path escapes the chunks directory")
    sections = base / "sections"
    sections.mkdir(parents=True, exist_ok=True)

    written = 0
    for i, chunk in enumerate(req.chunks):
        if not chunk.text.strip():
            continue
        doc = chunk.payload.get("document_name") or chunk.chunk_id.split(":")[-1] or f"chunk-{i}"
        payload = chunk.payload or {
            "source_type": source_type,
            "source_name": api_name,
            "chunk_type": "document_section",
            "document_name": doc,
        }
        fname = re.sub(r"[^\w.-]", "_", str(doc))[:120] + ".json"
        (sections / fname).write_text(
            json.dumps(
                {
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "content_hash": chunk.content_hash or hashlib.sha256(chunk.text.encode()).hexdigest(),
                    "payload": payload,
                },
                indent=2,
            )
        )
        written += 1

    (base / "_summary.json").write_text(
        json.dumps(
            {
                "chunk_id": f"{api_name}:doc-summary",
                "text": f"{api_name}: {written} chunks",
                "payload": {"source_type": source_type, "source_name": api_name, "chunk_type": "document_summary"},
            }
        )
    )

    # Index in the background (version=None → auto-resolve the latest v-prefixed
    # dir we just wrote). Returns immediately; embedding continues server-side.
    background_tasks.add_task(
        indexer_service.index_api, api_name, version=None, clean=req.clean, source_type=source_type
    )
    return {"uploaded": written, "version": f"v{version}", "status": "indexing-in-background"}


@router.get("/collection")
def collection_info() -> dict:
    """Get Qdrant collection info."""
    return vector_service.get_collection_info()


@router.delete("/collection/{api_name}")
def delete_api_points(api_name: str) -> dict:
    """Delete all indexed points for a specific API."""
    vector_service.delete_by_api(api_name)
    return {"deleted": True, "api_name": api_name}


# ── Semantic Deduplication ──────────────────────────────────────────────


@router.get("/dedup/report")
def dedup_report(
    source_name: str | None = Query(default=None, description="Filter to a specific source"),
    source_type: str | None = Query(default=None, description="Filter to a specific source type"),
    limit: int = Query(default=500, description="Max points to scan"),
    threshold: float | None = Query(default=None, description="Override similarity threshold"),
) -> dict:
    """Scan indexed chunks for semantic duplicates.

    Returns pairs of chunks that are semantically near-identical,
    indicating redundancy in the knowledge base.
    """
    duplicates = dedup_service.find_duplicates(
        source_name=source_name,
        source_type=source_type,
        limit=limit,
        threshold=threshold,
    )
    return {
        "duplicates": [
            {
                "chunk_a": d.new_chunk_id,
                "chunk_b": d.existing_chunk_id,
                "score": round(d.score, 4),
                "source_type": d.existing_source_type,
                "chunk_type": d.existing_chunk_type,
                "preview": d.existing_text_preview,
            }
            for d in duplicates
        ],
        "total": len(duplicates),
        "threshold": threshold or settings.dedup.similarity_threshold,
        "enabled": settings.dedup.enabled,
    }


@router.get("/dedup/drift")
def drift_report(
    source_name: str | None = Query(default=None, description="Filter to a specific source"),
    limit: int = Query(default=200, description="Max doc chunks to check"),
    threshold: float | None = Query(default=None, description="Override drift threshold"),
) -> dict:
    """Detect documentation that has drifted from its nearest code.

    Compares doc/document chunks against code chunks using vector similarity.
    Low similarity indicates the documentation may be stale or outdated.
    """
    drift_matches = dedup_service.detect_drift(
        source_name=source_name,
        limit=limit,
        threshold=threshold,
    )
    return {
        "drift": [
            {
                "doc_chunk": d.doc_chunk_id,
                "doc_preview": d.doc_text_preview,
                "nearest_code_chunk": d.nearest_code_chunk_id,
                "code_preview": d.nearest_code_text_preview,
                "score": round(d.score, 4),
                "source_name": d.source_name,
            }
            for d in drift_matches
        ],
        "total": len(drift_matches),
        "threshold": threshold or settings.dedup.drift_threshold,
    }
