"""Owner payload for personal Qdrant collections.

The indexed document collection (``qdrant.collection_name``, what ``@qdrant``
searches) stays shared. Notes, mail, snippets, the knowledge-base entries, and
``conversation_memory`` do not.

No caller (auth off, or a startup thread): no filter. New points are still
stamped with the admin id so a later login wall does not hide them. A caller
sees a point only when its ``user_id`` matches. Points with no ``user_id`` are
not visible to that caller. Startup backfill stamps those as the admin user,
which is the key the macOS apps send.
"""

from __future__ import annotations

import logging
import os

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchValue,
    PayloadField,
)

from app.config import settings
from app.security import ADMIN_USER_ID, current_user_id

logger = logging.getLogger(__name__)

_SCROLL = 256


def personal_collection_names() -> frozenset[str]:
    """Collections whose points belong to one user."""
    names = {
        settings.knowledge.collection_name,
        settings.knowledge.notes_collection_name,
        settings.knowledge.mail_collection_name,
        settings.knowledge.snippet_collection_name,
        "conversation_memory",
    }
    return frozenset(name for name in names if name)


def owner_for_write() -> str:
    """User id stored on a new point. No caller still lands on the admin."""
    return current_user_id() or ADMIN_USER_ID


def owner_condition() -> FieldCondition | None:
    """Match clause for the caller, or None when no caller is set."""
    actor = current_user_id()
    if not actor:
        return None
    return FieldCondition(key="user_id", match=MatchValue(value=actor))


def payload_visible(payload: dict | None) -> bool:
    """True when this payload may be returned to the caller."""
    actor = current_user_id()
    if not actor:
        return True
    if not payload:
        return False
    return payload.get("user_id") == actor


def stamp_payload(payload: dict) -> dict:
    """Set ``user_id`` when the point does not already have one."""
    if not payload.get("user_id"):
        payload["user_id"] = owner_for_write()
    return payload


def hash_material(content_hash: str) -> str:
    """Content-addressed id material.

    The admin keeps the historical id, so existing points still dedup. Any
    other caller gets a prefixed id and cannot overwrite those points.
    """
    actor = current_user_id()
    if not actor or actor == ADMIN_USER_ID:
        return content_hash
    return f"{actor}:{content_hash}"


def restrict_filter(collection: str, qfilter: Filter | None) -> Filter | None:
    """Add the caller match to a personal collection. Other collections stay put."""
    if collection not in personal_collection_names():
        return qfilter
    cond = owner_condition()
    if cond is None:
        return qfilter
    if qfilter is None:
        return Filter(must=[cond])
    must = list(qfilter.must or [])
    if not any(getattr(item, "key", None) == "user_id" for item in must):
        must.append(cond)
    kwargs: dict = {"must": must, "should": qfilter.should, "must_not": qfilter.must_not}
    min_should = getattr(qfilter, "min_should", None)
    if min_should is not None:
        kwargs["min_should"] = min_should
    return Filter(**kwargs)


def scope_scroll_filter(collection: str) -> Filter | None:
    """Scroll filter for a personal collection, or None when the collection is shared."""
    if collection not in personal_collection_names():
        return None
    cond = owner_condition()
    if cond is None:
        return None
    return Filter(must=[cond])


def visible_count(client, collection: str, fallback: int) -> int:
    """Point count the caller is allowed to see."""
    filt = scope_scroll_filter(collection)
    if filt is None:
        return fallback
    try:
        counted = client.count(collection_name=collection, count_filter=filt, exact=True)
        return int(getattr(counted, "count", 0) or 0)
    except Exception as exc:
        logger.warning("scoped count failed for %s: %s", collection, exc)
        return fallback


def filter_owned_ids(client, collection: str, ids: list[str]) -> list[str]:
    """Drop ids the caller does not own. Shared collections return the input."""
    if collection not in personal_collection_names() or owner_condition() is None:
        return ids
    actor = current_user_id()
    if not actor or not ids:
        return []
    try:
        points = client.retrieve(
            collection_name=collection,
            ids=ids,
            with_payload=["user_id"],
            with_vectors=False,
        )
    except Exception as exc:
        logger.warning("owned-id retrieve failed for %s: %s", collection, exc)
        return []
    return [str(point.id) for point in points if (point.payload or {}).get("user_id") == actor]


def backfill_missing_owner(client, collection: str) -> int:
    """Stamp points that have no ``user_id`` with the admin id.

    Returns how many points were updated. Safe to run again: a point that
    already has ``user_id`` is left alone.
    """
    stamped = 0
    offset = None
    use_filter = True
    empty = Filter(must=[IsEmptyCondition(is_empty=PayloadField(key="user_id"))])
    while True:
        kwargs = {
            "collection_name": collection,
            "limit": _SCROLL,
            "offset": offset,
            "with_payload": ["user_id"],
            "with_vectors": False,
        }
        try:
            if use_filter:
                points, offset = client.scroll(scroll_filter=empty, **kwargs)
            else:
                points, offset = client.scroll(**kwargs)
        except Exception:
            if not use_filter:
                raise
            use_filter = False
            offset = None
            stamped = 0
            continue
        if use_filter:
            missing = [point.id for point in points]
        else:
            missing = [point.id for point in points if not (point.payload or {}).get("user_id")]
        if missing:
            client.set_payload(
                collection_name=collection,
                payload={"user_id": ADMIN_USER_ID},
                points=missing,
            )
            stamped += len(missing)
        if offset is None:
            break
    return stamped


def delete_personal_points(user_id: str, *, client: QdrantClient | None = None) -> None:
    """Delete one user's points from the personal knowledge collections.

    ``conversation_memory`` is cleared by ``ConversationMemory.delete_for_user``.
    A unit test that does not pass ``client`` does not open the dev Qdrant.
    """
    if not user_id:
        return
    if client is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return
    own = personal_collection_names() - {"conversation_memory"}
    if client is None:
        try:
            client = QdrantClient(host=settings.qdrant.host, port=settings.qdrant.port, timeout=5)
        except Exception as exc:
            logger.warning("knowledge owner delete skipped (no client): %s", exc)
            return
    filt = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])
    for name in sorted(own):
        try:
            if not client.collection_exists(name):
                continue
            client.delete(collection_name=name, points_selector=filt)
        except Exception as exc:
            logger.warning("knowledge owner delete failed for %s: %s", name, exc)
