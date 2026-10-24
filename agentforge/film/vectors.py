"""Qdrant neighbors. Default collection is the recommender's ``media_fingerprints``.

Existing points (taste fingerprints) are left alone. New upserts only happen when
the embed vector size matches the collection; otherwise TMDB recs still work.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from agentforge.config import get_config

logger = logging.getLogger(__name__)


def collection_name() -> str:
    cfg = str(get_config().get("tools.film.collection", "") or "").strip()
    return cfg or "media_fingerprints"


def _client():
    from qdrant_client import QdrantClient

    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    return QdrantClient(host=host, port=port, timeout=10)


def _embed(text: str) -> list[float] | None:
    try:
        from app.services.embedding_service import get_embedding_service

        return get_embedding_service().embed(text)
    except Exception as exc:
        logger.debug("film embed skipped: %s", exc)
        return None


def embed_text(title: str, year: Any, media_type: str, genres: str, overview: str) -> str:
    yr = year if year not in (None, "") else ""
    return f"{title} ({yr}) [{media_type}]. {genres}. {overview}".strip()


def _collection_size(client, name: str) -> int | None:
    try:
        info = client.get_collection(name)
        params = info.config.params.vectors
        if hasattr(params, "size"):
            return int(params.size)
        if isinstance(params, dict) and "" in params:
            return int(params[""].size)
        return None
    except Exception:
        return None


def point_exists(tmdb_id: int) -> bool | None:
    """True if the collection has this TMDB id. None if Qdrant is unreachable."""
    try:
        client = _client()
        name = collection_name()
        found = client.retrieve(collection_name=name, ids=[int(tmdb_id)])
        return bool(found)
    except Exception as exc:
        logger.debug("film Qdrant retrieve skipped: %s", exc)
        return None


def upsert_media(row: dict[str, Any]) -> bool:
    """Embed and upsert only when the collection exists and dims match.

    Does not create a new collection (reuse ``media_fingerprints``). Skips if
    the point already exists so recommender fingerprints stay intact.
    """
    tmdb_raw = row.get("tmdb_id")
    if tmdb_raw is None or tmdb_raw == "":
        return False
    tmdb_id = int(tmdb_raw)
    try:
        vec = _embed(
            embed_text(
                row.get("title") or "",
                row.get("year"),
                row.get("type") or "movie",
                row.get("genres") or "",
                row.get("overview") or "",
            )
        )
        if not vec:
            return False
        from qdrant_client.models import PointStruct

        client = _client()
        name = collection_name()
        existing = [c.name for c in client.get_collections().collections]
        if name not in existing:
            logger.debug("film collection %s missing — skip upsert", name)
            return False
        size = _collection_size(client, name)
        if size is not None and size != len(vec):
            logger.debug("film embed dim %d != collection %d — skip upsert", len(vec), size)
            return False
        found = client.retrieve(collection_name=name, ids=[tmdb_id])
        if found:
            return True
        client.upsert(
            collection_name=name,
            points=[
                PointStruct(
                    id=tmdb_id,
                    vector=vec,
                    payload={
                        "tmdb_id": tmdb_id,
                        "type": row.get("type") or "movie",
                        "title": row.get("title") or "",
                        "year": row.get("year"),
                        "overview": row.get("overview") or "",
                        "genres": row.get("genres") or "",
                    },
                )
            ],
        )
        return True
    except Exception as exc:
        logger.warning("film Qdrant upsert failed: %s", exc)
        return False


def collection_point_count() -> tuple[str, int | None]:
    """Return (collection_name, points_count). count is None if Qdrant is down."""
    name = collection_name()
    try:
        result = _client().count(collection_name=name, exact=True)
        n = getattr(result, "count", result)
        return (name, int(n))
    except Exception as exc:
        logger.debug("film Qdrant count skipped: %s", exc)
        return (name, None)


def search_neighbors(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    vec = _embed(query)
    if not vec:
        return []
    try:
        client = _client()
        name = collection_name()
        size = _collection_size(client, name)
        if size is not None and size != len(vec):
            logger.debug("film search skipped: embed dim %d != collection %d", len(vec), size)
            return []
        hits = client.search(collection_name=name, query_vector=vec, limit=limit)
        out: list[dict[str, Any]] = []
        for hit in hits:
            payload = hit.payload or {}
            out.append(
                {
                    "tmdb_id": int(payload.get("tmdb_id") or hit.id),
                    "title": payload.get("title") or "",
                    "year": payload.get("year"),
                    "type": payload.get("type") or "movie",
                    "overview": payload.get("overview") or "",
                    "genres": payload.get("genres") or "",
                    "score": float(hit.score) if hit.score is not None else None,
                }
            )
        return out
    except Exception as exc:
        logger.debug("film Qdrant search skipped: %s", exc)
        return []
