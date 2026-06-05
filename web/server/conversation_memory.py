"""Conversation Memory Service — semantic cross-session recall via Qdrant.

After each completed agent run, the query+response pair is embedded and stored
in a dedicated Qdrant collection (``conversation_memory``).  Before building the
LLM context for a new query, the service retrieves the top-N semantically
relevant past exchanges — even from other sessions — providing cross-session
recall without any model fine-tuning.

The service is intentionally fire-and-forget for writes (errors are logged, never
raised) so that a Qdrant outage cannot break the main chat flow.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from qdrant_client import QdrantClient
from qdrant_client.models import (
    DatetimeRange,
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from app.config import settings as _af_settings

if TYPE_CHECKING:
    from app.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (can be overridden via config.yaml → memory.semantic)
# ---------------------------------------------------------------------------
_DEFAULT_COLLECTION = "conversation_memory"
_DEFAULT_RECALL_TOP_K = 5
_DEFAULT_MIN_SCORE = 0.55
_DEFAULT_EXCLUDE_CURRENT = False

# Maximum characters from query+response to embed (keeps embeddings focused)
_MAX_EMBED_CHARS = _af_settings.memory.semantic_max_embed_chars


class ConversationMemory:
    """Qdrant-backed semantic memory for conversation exchanges."""

    def __init__(
        self,
        embedding_service: EmbeddingService,
        qdrant_host: str = "localhost",
        qdrant_port: int = 6333,
        collection: str = _DEFAULT_COLLECTION,
        dimension: int = _af_settings.embedding.dimension,
        recall_top_k: int = _DEFAULT_RECALL_TOP_K,
        min_score: float = _DEFAULT_MIN_SCORE,
        exclude_current_session: bool = _DEFAULT_EXCLUDE_CURRENT,
    ) -> None:
        self._emb = embedding_service
        self._host = qdrant_host
        self._port = qdrant_port
        self._collection = collection
        self._dimension = dimension
        self._recall_top_k = recall_top_k
        self._min_score = min_score
        self._exclude_current = exclude_current_session
        self._client: QdrantClient | None = None
        self._ready = False

    # -- Lazy Qdrant client ------------------------------------------------

    def _get_client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(host=self._host, port=self._port)
        return self._client

    def ensure_collection(self) -> None:
        """Create the collection if it doesn't exist (idempotent)."""
        try:
            client = self._get_client()
            names = [c.name for c in client.get_collections().collections]
            if self._collection not in names:
                client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=self._dimension,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Created conversation_memory collection '%s' (dim=%d)", self._collection, self._dimension)
            # Ensure payload indexes
            for field in ("session_id", "mode"):
                try:
                    client.create_payload_index(
                        collection_name=self._collection,
                        field_name=field,
                        field_schema=PayloadSchemaType.KEYWORD,
                    )
                except Exception:
                    pass  # already exists
            # Timestamp index for age-based filtering
            try:
                client.create_payload_index(
                    collection_name=self._collection,
                    field_name="timestamp",
                    field_schema=PayloadSchemaType.DATETIME,
                )
            except Exception:
                pass  # already exists
            self._ready = True
            logger.info("ConversationMemory ready (collection=%s)", self._collection)
        except Exception as exc:
            logger.warning("ConversationMemory init failed (Qdrant unreachable?): %s", exc)
            self._ready = False

    # -- Store -------------------------------------------------------------

    def store_exchange(
        self,
        session_id: str,
        query: str,
        response: str,
        mode: str = "",
        model: str = "",
        incognito: bool = False,
    ) -> None:
        """Embed and store a query+response pair.  Fire-and-forget.

        Both *query* and *response* are scanned for secrets and redacted
        before embedding and persistence when the secret-redaction feature
        is enabled.

        Gated by ``memory_policy.should_store_conversation`` — only FULL
        tier modes persist, and never when ``incognito`` is true. The gate
        is checked here (not only at the caller) so a forgotten upstream
        guard can't leak private data into Qdrant.
        """
        if not self._ready:
            logger.warning("ConversationMemory not ready — skipping store")
            return

        from web.server.memory_policy import should_store_conversation

        if not should_store_conversation(mode, incognito=incognito):
            logger.debug(
                "conversation_memory store skipped by policy (mode=%r, incognito=%s)",
                mode,
                incognito,
            )
            return

        # --- Secret redaction (before embedding + persistence) ---
        try:
            from agentforge.secret_redactor import get_redactor

            redactor = get_redactor()
            query = redactor.redact(query).text
            response = redactor.redact(response).text
        except Exception:
            pass  # graceful fallback — never block persistence

        try:
            # Build the text to embed — query is weighted higher by appearing first
            combined = f"Q: {query[:800]}\nA: {response[:1200]}"
            if len(combined) > _MAX_EMBED_CHARS:
                combined = combined[:_MAX_EMBED_CHARS]

            logger.info("Embedding conversation exchange (%d chars)…", len(combined))
            vector = self._emb.embed(combined)
            logger.info("Embedded OK (dim=%d), upserting to Qdrant…", len(vector))

            point_id = self._make_id(session_id, query)
            payload = {
                "session_id": session_id,
                "query": query[:500],
                "response": response[:1500],
                "mode": mode,
                "model": model,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            client = self._get_client()
            client.upsert(
                collection_name=self._collection,
                points=[PointStruct(id=point_id, vector=vector, payload=payload)],
            )
            logger.info("Stored exchange in conversation_memory (session=%s, id=%s)", session_id[:12], point_id[:12])
        except Exception as exc:
            logger.warning("Failed to store conversation exchange: %s", exc, exc_info=True)

    # -- Recall ------------------------------------------------------------

    def recall(
        self,
        query: str,
        top_k: int | None = None,
        exclude_session: str | None = None,
        max_age_days: int | None = None,
    ) -> list[dict]:
        """Retrieve semantically relevant past exchanges for the given query.

        When *max_age_days* is set, entries older than the cutoff are excluded.

        Returns a list of dicts: ``[{"query": ..., "response": ..., "session_id": ..., "score": ...}]``
        """
        if not self._ready:
            return []

        k = top_k or self._recall_top_k

        try:
            vector = self._emb.embed(query[:800])

            # Build filter conditions
            must_not: list[FieldCondition] = []
            must: list[FieldCondition] = []

            sid_to_exclude = exclude_session if self._exclude_current else None
            if sid_to_exclude:
                must_not.append(
                    FieldCondition(key="session_id", match=MatchValue(value=sid_to_exclude)),
                )

            if max_age_days:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
                must.append(
                    FieldCondition(key="timestamp", range=DatetimeRange(gte=cutoff)),
                )

            query_filter = None
            if must or must_not:
                query_filter = Filter(must=must or None, must_not=must_not or None)

            client = self._get_client()
            results = client.query_points(
                collection_name=self._collection,
                query=vector,
                limit=k,
                score_threshold=self._min_score,
                query_filter=query_filter,
                with_payload=True,
            )

            memories = []
            for hit in results.points:
                p = hit.payload or {}
                memories.append(
                    {
                        "query": p.get("query", ""),
                        "response": p.get("response", ""),
                        "session_id": p.get("session_id", ""),
                        "mode": p.get("mode", ""),
                        "score": hit.score,
                    }
                )

            if memories:
                logger.info(
                    "Recalled %d memory/ies for query (best=%.3f): %s",
                    len(memories),
                    memories[0]["score"] if memories else 0,
                    query[:80],
                )
            return memories

        except Exception as exc:
            logger.warning("Conversation recall failed: %s", exc)
            return []

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _make_id(session_id: str, query: str) -> str:
        """Deterministic UUID for a session+query pair."""
        h = hashlib.md5(f"{session_id}:{query}".encode()).hexdigest()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    def delete_by_mode(self, mode: str) -> int:
        """Delete all points matching a given mode.  Returns count deleted."""
        if not self._ready:
            return 0
        try:
            client = self._get_client()
            result = client.delete(
                collection_name=self._collection,
                points_selector=Filter(
                    must=[
                        FieldCondition(key="mode", match=MatchValue(value=mode)),
                    ],
                ),
            )
            logger.info("Deleted conversation_memory points with mode=%r: %s", mode, result)
            return -1  # Qdrant delete doesn't return count; -1 = success
        except Exception as exc:
            logger.warning("Failed to delete conversation_memory by mode=%r: %s", mode, exc)
            return 0

    def delete_exchange(self, session_id: str, query: str) -> bool:
        """Delete the single exchange point for (session_id, query).

        Mirrors the point_id scheme used by ``store_exchange``. Safe to call
        even when the collection is not ready (no-op) or the point does not
        exist (Qdrant silently ignores missing IDs).
        """
        if not self._ready:
            return False
        try:
            point_id = self._make_id(session_id, query)
            client = self._get_client()
            client.delete(
                collection_name=self._collection,
                points_selector=PointIdsList(points=[point_id]),
            )
            logger.info("Deleted conversation_memory exchange (session=%s)", session_id[:12])
            return True
        except Exception as exc:
            logger.warning("Failed to delete conversation_memory exchange: %s", exc)
            return False

    def delete_older_than(self, days: int) -> int:
        """Delete all points whose payload ``timestamp`` is older than *days*.

        Used by the nightly memory-prune task. Qdrant's ``delete`` returns
        a status rather than a count; we follow up with a filtered count
        query so the prune log line is meaningful.
        """
        if not self._ready or days <= 0:
            return 0
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            age_filter = Filter(
                must=[
                    FieldCondition(key="timestamp", range=DatetimeRange(lt=cutoff)),
                ],
            )
            client = self._get_client()
            # Count first so we can log something useful
            try:
                count_result = client.count(
                    collection_name=self._collection,
                    count_filter=age_filter,
                    exact=False,
                )
                count = int(getattr(count_result, "count", 0) or 0)
            except Exception:
                count = -1  # count is informational only

            client.delete(
                collection_name=self._collection,
                points_selector=age_filter,
            )
            logger.info(
                "Pruned conversation_memory entries older than %dd (approx %d points)",
                days,
                count,
            )
            return count
        except Exception as exc:
            logger.warning("Failed to prune conversation_memory: %s", exc)
            return 0

    def delete_all(self) -> bool:
        """Delete every point in the collection. Used by the Settings UI.

        Returns True on success, False otherwise. The collection itself is
        kept (so writes continue to work); only the points are removed.
        """
        if not self._ready:
            return False
        try:
            client = self._get_client()
            client.delete(
                collection_name=self._collection,
                points_selector=Filter(must=[]),
            )
            logger.info("Cleared all conversation_memory points")
            return True
        except Exception as exc:
            logger.warning("Failed to clear conversation_memory: %s", exc)
            return False

    def delete_by_modes(self, modes: list[str]) -> int:
        """Delete all points matching any of the given modes."""
        if not self._ready:
            return 0
        try:
            from qdrant_client.models import MatchAny

            client = self._get_client()
            result = client.delete(
                collection_name=self._collection,
                points_selector=Filter(
                    must=[
                        FieldCondition(key="mode", match=MatchAny(any=modes)),
                    ],
                ),
            )
            logger.info("Deleted conversation_memory points with modes=%r: %s", modes, result)
            return -1
        except Exception as exc:
            logger.warning("Failed to delete conversation_memory by modes=%r: %s", modes, exc)
            return 0

    def get_stats(self) -> dict:
        """Return basic stats about the conversation memory collection."""
        if not self._ready:
            return {"status": "not_ready"}
        try:
            client = self._get_client()
            info = client.get_collection(self._collection)
            return {
                "collection": self._collection,
                "points_count": info.points_count,
                "status": info.status.value if info.status else "unknown",
            }
        except Exception:
            return {"status": "error"}


# ---------------------------------------------------------------------------
# Singleton — initialized lazily from ws_endpoint on first use
# ---------------------------------------------------------------------------
_instance: ConversationMemory | None = None


def get_conversation_memory() -> ConversationMemory | None:
    """Return the singleton instance, or None if not initialized."""
    return _instance


def init_conversation_memory(
    embedding_service: EmbeddingService,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection: str = _DEFAULT_COLLECTION,
    dimension: int = _af_settings.embedding.dimension,
    recall_top_k: int = _DEFAULT_RECALL_TOP_K,
    min_score: float = _DEFAULT_MIN_SCORE,
    exclude_current_session: bool = _DEFAULT_EXCLUDE_CURRENT,
) -> ConversationMemory:
    """Create and initialize the singleton."""
    global _instance
    _instance = ConversationMemory(
        embedding_service=embedding_service,
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        collection=collection,
        dimension=dimension,
        recall_top_k=recall_top_k,
        min_score=min_score,
        exclude_current_session=exclude_current_session,
    )
    _instance.ensure_collection()
    return _instance
