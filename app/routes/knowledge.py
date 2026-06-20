"""Knowledge Database API router.

CRUD + semantic search for user-created knowledge entries.
"""

import logging

from fastapi import APIRouter, HTTPException, Response

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
