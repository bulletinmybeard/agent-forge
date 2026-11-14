"""Qdrant neighbors. Default collection is the recommender's ``media_fingerprints``.

Existing points (taste fingerprints) are left alone. New upserts only happen when
the embed vector size matches the collection; otherwise TMDB recs still work.

Each signed-in user has their own points. The admin and a no-caller install keep
the historical point id, which is the TMDB id. Another user gets a separate id
and does not overwrite that point. Points with no ``user_id`` are stamped as
the admin the first time this process opens the collection.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any

from qdrant_client.models import Filter, PayloadSchemaType

from agentforge.config import get_config
from app.services.qdrant_owner import (
    backfill_missing_owner,
    owner_condition,
    owner_for_write,
    visible_count,
)
from web.server.identity import ADMIN_USER_ID, current_user_id

logger = logging.getLogger(__name__)

_backfill_lock = threading.Lock()
_backfilled: set[str] = set()


def _owner_filter() -> Filter | None:
    cond = owner_condition()
    if cond is None:
        return None
    return Filter(must=[cond])


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


def point_id(tmdb_id: int) -> int | str:
    """Qdrant id for this title and caller.

    Admin and no-caller keep the integer TMDB id already stored. Any other
    caller gets a UUID so the same title can exist twice.
    """
    actor = current_user_id()
    if not actor or actor == ADMIN_USER_ID:
        return int(tmdb_id)
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"agentforge-film:{actor}:{int(tmdb_id)}"))


def _ensure_owners(client, name: str) -> None:
    """Index ``user_id`` and stamp points that have none. Once per process."""
    if name in _backfilled:
        return
    with _backfill_lock:
        if name in _backfilled:
            return
        try:
            client.create_payload_index(
                collection_name=name,
                field_name="user_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception:
            logger.debug("film user_id index already exists or failed for %s", name)
        try:
            stamped = backfill_missing_owner(client, name)
        except Exception as exc:
            logger.warning("film owner backfill skipped for %s: %s", name, exc)
            return
        _backfilled.add(name)
        if stamped:
            logger.info("film collection %s: stamped user_id on %d points", name, stamped)


def point_exists(tmdb_id: int) -> bool | None:
    """True if this caller already has this TMDB id. None if Qdrant is unreachable."""
    try:
        client = _client()
        name = collection_name()
        _ensure_owners(client, name)
        found = client.retrieve(collection_name=name, ids=[point_id(tmdb_id)])
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
        _ensure_owners(client, name)
        existing = [c.name for c in client.get_collections().collections]
        if name not in existing:
            logger.debug("film collection %s missing — skip upsert", name)
            return False
        size = _collection_size(client, name)
        if size is not None and size != len(vec):
            logger.debug("film embed dim %d != collection %d — skip upsert", len(vec), size)
            return False
        pid = point_id(tmdb_id)
        found = client.retrieve(collection_name=name, ids=[pid])
        if found:
            return True
        client.upsert(
            collection_name=name,
            points=[
                PointStruct(
                    id=pid,
                    vector=vec,
                    payload={
                        "tmdb_id": tmdb_id,
                        "type": row.get("type") or "movie",
                        "title": row.get("title") or "",
                        "year": row.get("year"),
                        "overview": row.get("overview") or "",
                        "genres": row.get("genres") or "",
                        "user_id": owner_for_write(),
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
        client = _client()
        _ensure_owners(client, name)
        result = client.count(collection_name=name, exact=True)
        n = int(getattr(result, "count", result))
        return (name, visible_count(client, name, n))
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
        _ensure_owners(client, name)
        size = _collection_size(client, name)
        if size is not None and size != len(vec):
            logger.debug("film search skipped: embed dim %d != collection %d", len(vec), size)
            return []
        kwargs: dict[str, Any] = {"collection_name": name, "query_vector": vec, "limit": limit}
        filt = _owner_filter()
        if filt is not None:
            kwargs["query_filter"] = filt
        hits = client.search(**kwargs)
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
