"""Multi-collection routing for the Knowledge Database API.

The Knowledge Base SPA uses ``knowledge_entries`` (default). AgentForge Notes
uses ``kb_note_entries``. Clients select a collection via the
``X-Knowledge-Collection`` header; WebSocket agent tools inherit the session
``source`` (``notes`` -> notes collection).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from fastapi import Header, HTTPException

from app.config import settings
from app.services.knowledge_service import KnowledgeService
from app.services.knowledge_vector_service import KnowledgeVectorService

logger = logging.getLogger(__name__)

COLLECTION_HEADER = "X-Knowledge-Collection"

_request_collection: ContextVar[str | None] = ContextVar("knowledge_request_collection", default=None)

_services: dict[str, KnowledgeService] = {}
_vector_services: dict[str, KnowledgeVectorService] = {}


def allowed_collections() -> frozenset[str]:
    return frozenset(
        {
            settings.knowledge.collection_name,
            settings.knowledge.notes_collection_name,
        }
    )


def default_collection() -> str:
    return settings.knowledge.collection_name


def notes_collection() -> str:
    return settings.knowledge.notes_collection_name


def resolve_collection(header_value: str | None = None) -> str:
    """Pick the Qdrant collection for this request."""
    allowed = allowed_collections()

    if header_value:
        name = header_value.strip()
        if name not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown knowledge collection {name!r}. Allowed: {sorted(allowed)}",
            )
        return name

    ctx = _request_collection.get()
    if ctx and ctx in allowed:
        return ctx

    return default_collection()


def set_request_knowledge_collection(collection: str | None) -> None:
    """Scope knowledge tool calls to a collection for the current asyncio task."""
    _request_collection.set(collection)


def collection_for_session_source(source: str | None) -> str | None:
    if source == "notes":
        return notes_collection()
    return None


def get_vector_service(collection: str | None = None) -> KnowledgeVectorService:
    name = collection or resolve_collection()
    if name not in _vector_services:
        _vector_services[name] = KnowledgeVectorService(collection_name=name)
    return _vector_services[name]


def get_knowledge_service(collection: str | None = None) -> KnowledgeService:
    name = collection or resolve_collection()
    if name not in _services:
        _services[name] = KnowledgeService(vector_service=get_vector_service(name))
    return _services[name]


def ensure_all_collections() -> None:
    for name in allowed_collections():
        try:
            get_vector_service(name).ensure_collection()
        except Exception as exc:
            logger.warning("Could not ensure knowledge collection %s: %s", name, exc)


def knowledge_service_dependency(
    x_knowledge_collection: str | None = Header(default=None, alias=COLLECTION_HEADER),
) -> KnowledgeService:
    return get_knowledge_service(resolve_collection(x_knowledge_collection))
