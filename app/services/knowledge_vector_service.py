"""Qdrant operations for the knowledge_entries collection.

Sibling of VectorService — same patterns, dedicated to user-created
knowledge entries (notes, references, documentation, documents, cheatsheets, snippets).
"""

import hashlib
import logging

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.config import settings

logger = logging.getLogger(__name__)

DISTANCE_MAP = {
    "Cosine": Distance.COSINE,
    "Euclidean": Distance.EUCLID,
    "Dot": Distance.DOT,
}

PAYLOAD_INDEXES_KEYWORD = ["content_type", "language", "tags", "source_url", "project", "parent_id", "is_chunk"]
PAYLOAD_INDEXES_DATETIME = ["created_at", "updated_at"]


class KnowledgeVectorService:
    def __init__(self, collection_name: str | None = None) -> None:
        self._collection_name = collection_name
        self._client: QdrantClient | None = None

    @property
    def _collection(self) -> str:
        return self._collection_name or settings.knowledge.collection_name

    def _get_client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(host=settings.qdrant.host, port=settings.qdrant.port)
        return self._client

    def ensure_collection(self) -> None:
        client = self._get_client()
        collections = [c.name for c in client.get_collections().collections]

        if self._collection not in collections:
            distance = DISTANCE_MAP.get(settings.embedding.distance_metric, Distance.COSINE)
            client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=settings.embedding.dimension,
                    distance=distance,
                ),
            )
            logger.info("Created collection '%s' (dim=%d)", self._collection, settings.embedding.dimension)
        else:
            logger.info("Collection '%s' already exists", self._collection)

        for field_name in PAYLOAD_INDEXES_KEYWORD:
            try:
                client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
                logger.debug("Ensured payload index on '%s'", field_name)
            except Exception:
                logger.debug("Payload index '%s' already exists or failed", field_name)

        for field_name in PAYLOAD_INDEXES_DATETIME:
            try:
                client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=PayloadSchemaType.DATETIME,
                )
                logger.debug("Ensured payload index on '%s'", field_name)
            except Exception:
                logger.debug("Payload index '%s' already exists or failed", field_name)

    def upsert_batch(self, points: list[PointStruct]) -> None:
        if not points:
            return
        client = self._get_client()
        client.upsert(collection_name=self._collection, points=points)
        logger.debug("Upserted %d points to '%s'", len(points), self._collection)

    def get_by_id(self, point_id: str) -> dict | None:
        client = self._get_client()
        try:
            points = client.retrieve(
                collection_name=self._collection,
                ids=[point_id],
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                return None
            p = points[0]
            return {"id": str(p.id), "payload": dict(p.payload) if p.payload else {}}
        except Exception as e:
            logger.warning("Failed to retrieve point '%s': %s", point_id, e)
            return None

    def search(
        self,
        query_vector: list[float],
        limit: int = 10,
        score_threshold: float | None = None,
        content_type: str | None = None,
        language: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        parent_id: str | None = None,
    ) -> list[dict]:
        client = self._get_client()

        conditions = []
        if content_type:
            conditions.append(FieldCondition(key="content_type", match=MatchValue(value=content_type)))
        if language:
            conditions.append(FieldCondition(key="language", match=MatchValue(value=language)))
        if tags:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
        if project:
            conditions.append(FieldCondition(key="project", match=MatchValue(value=project)))
        if parent_id:
            conditions.append(FieldCondition(key="parent_id", match=MatchValue(value=parent_id)))

        # Exclude page chunks from top-level search results
        must_not = [FieldCondition(key="is_chunk", match=MatchValue(value=True))]

        query_filter = Filter(must=conditions or None, must_not=must_not)

        response = client.query_points(
            collection_name=self._collection,
            query=query_vector,
            limit=limit,
            score_threshold=score_threshold,
            query_filter=query_filter,
            with_payload=True,
        )
        return [
            {"id": str(hit.id), "score": hit.score, "payload": dict(hit.payload) if hit.payload else {}}
            for hit in response.points
        ]

    def set_payload(self, point_id: str, payload: dict) -> None:
        client = self._get_client()
        client.set_payload(
            collection_name=self._collection,
            payload=payload,
            points=[point_id],
        )

    def get_content_hashes(self, point_ids: list[str]) -> dict[str, str]:
        if not point_ids:
            return {}
        client = self._get_client()
        try:
            points = client.retrieve(
                collection_name=self._collection,
                ids=point_ids,
                with_payload=["content_hash"],
                with_vectors=False,
            )
            return {str(p.id): p.payload.get("content_hash", "") for p in points if p.payload}
        except Exception as e:
            logger.warning("Failed to retrieve content hashes: %s", e)
            return {}

    def delete_point(self, point_id: str) -> None:
        client = self._get_client()
        client.delete(
            collection_name=self._collection,
            points_selector=[point_id],
        )
        logger.debug("Deleted point '%s' from '%s'", point_id, self._collection)

    def delete_by_filter(
        self,
        tags: list[str] | None = None,
        content_type: str | None = None,
        before: str | None = None,
        project: str | None = None,
    ) -> int:
        client = self._get_client()
        conditions = []
        if tags:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
        if content_type:
            conditions.append(FieldCondition(key="content_type", match=MatchValue(value=content_type)))
        if project:
            conditions.append(FieldCondition(key="project", match=MatchValue(value=project)))
        if before:
            from qdrant_client.models import DatetimeRange

            conditions.append(FieldCondition(key="created_at", range=DatetimeRange(lt=before)))

        if not conditions:
            return 0

        client.delete(
            collection_name=self._collection,
            points_selector=Filter(must=conditions),
        )
        logger.info("Deleted points matching filter from '%s'", self._collection)
        return -1  # Qdrant delete doesn't return count; caller checks collection info

    def scroll_by_filter(
        self,
        limit: int = 50,
        content_type: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        parent_id: str | None = None,
    ) -> list[dict]:
        """Return entries matching filters without vector search."""
        client = self._get_client()
        conditions = []
        if content_type:
            conditions.append(FieldCondition(key="content_type", match=MatchValue(value=content_type)))
        if tags:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
        if project:
            conditions.append(FieldCondition(key="project", match=MatchValue(value=project)))
        if parent_id:
            conditions.append(FieldCondition(key="parent_id", match=MatchValue(value=parent_id)))

        # Exclude page chunks from filter results
        must_not = [FieldCondition(key="is_chunk", match=MatchValue(value=True))]

        query_filter = Filter(must=conditions or None, must_not=must_not)
        points, _ = client.scroll(
            collection_name=self._collection,
            scroll_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [{"id": str(p.id), "payload": dict(p.payload) if p.payload else {}} for p in points]

    def list_slim(self, limit: int = 2000) -> list[dict]:
        """Lightweight listing: entry metadata only (no content body), chunks excluded.

        A payload selector keeps the heavy content field from ever leaving Qdrant.
        """
        client = self._get_client()
        must_not = [FieldCondition(key="is_chunk", match=MatchValue(value=True))]
        points, _ = client.scroll(
            collection_name=self._collection,
            scroll_filter=Filter(must_not=must_not),
            limit=limit,
            with_payload=[
                "title",
                "content_type",
                "language",
                "tags",
                "parent_id",
                "created_at",
                "metadata",
            ],
            with_vectors=False,
        )
        return [{"id": str(p.id), "payload": dict(p.payload) if p.payload else {}} for p in points]

    def search_chunks_by_parent(
        self,
        parent_id: str,
        query_vector: list[float],
        limit: int = 10,
    ) -> list[dict]:
        """Vector search within page chunks belonging to a parent entry."""
        client = self._get_client()
        query_filter = Filter(
            must=[
                FieldCondition(key="parent_id", match=MatchValue(value=parent_id)),
                FieldCondition(key="is_chunk", match=MatchValue(value=True)),
            ]
        )
        response = client.query_points(
            collection_name=self._collection,
            query=query_vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        return [
            {"id": str(hit.id), "score": hit.score, "payload": dict(hit.payload) if hit.payload else {}}
            for hit in response.points
        ]

    def get_chunks_by_parent(self, parent_id: str) -> list[dict]:
        """Scroll all page chunks belonging to a parent entry."""
        client = self._get_client()
        query_filter = Filter(
            must=[
                FieldCondition(key="parent_id", match=MatchValue(value=parent_id)),
                FieldCondition(key="is_chunk", match=MatchValue(value=True)),
            ]
        )
        points, _ = client.scroll(
            collection_name=self._collection,
            scroll_filter=query_filter,
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
        return [{"id": str(p.id), "payload": dict(p.payload) if p.payload else {}} for p in points]

    def delete_by_parent(self, parent_id: str) -> None:
        """Delete all page chunks belonging to a parent entry."""
        client = self._get_client()
        client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[
                    FieldCondition(key="parent_id", match=MatchValue(value=parent_id)),
                    FieldCondition(key="is_chunk", match=MatchValue(value=True)),
                ]
            ),
        )
        logger.debug("Deleted chunks for parent '%s'", parent_id)

    def facet_tags(self, limit: int = 2000) -> list[dict]:
        client = self._get_client()
        try:
            response = client.facet(
                collection_name=self._collection,
                key="tags",
                limit=limit,
                exact=True,
            )
            return [{"tag": hit.value, "count": hit.count} for hit in response.hits if hit.value]
        except Exception as e:
            logger.warning("facet_tags failed: %s", e)
            return []

    def get_collection_info(self) -> dict:
        client = self._get_client()
        try:
            info = client.get_collection(self._collection)
            return {
                "name": self._collection,
                "points_count": getattr(info, "points_count", None),
                "status": info.status.value if getattr(info, "status", None) else "unknown",
            }
        except Exception:
            return {"name": self._collection, "status": "not_found", "points_count": 0}

    def count_by_content_type(self) -> dict[str, int]:
        client = self._get_client()
        try:
            response = client.facet(
                collection_name=self._collection,
                key="content_type",
                limit=100,
                exact=True,
            )
            return {hit.value: hit.count for hit in response.hits if hit.value}
        except Exception as e:
            logger.warning("count_by_content_type failed: %s", e)
            return {}

    def count_recent(self, days: int = 7) -> int:
        from datetime import datetime, timedelta, timezone

        from qdrant_client.models import DatetimeRange

        client = self._get_client()
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            result = client.count(
                collection_name=self._collection,
                count_filter=Filter(must=[FieldCondition(key="created_at", range=DatetimeRange(gte=cutoff))]),
                exact=True,
            )
            return result.count
        except Exception as e:
            logger.warning("count_recent failed: %s", e)
            return 0

    @staticmethod
    def generate_point_id(content_hash: str) -> str:
        h = hashlib.md5(content_hash.encode()).hexdigest()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


knowledge_vector_service = KnowledgeVectorService()
