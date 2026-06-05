import hashlib
import logging
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
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


class VectorService:
    def __init__(self) -> None:
        self._client: QdrantClient | None = None

    def _get_client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(host=settings.qdrant.host, port=settings.qdrant.port)
        return self._client

    # Payload fields that should have keyword indexes for fast filtered search.
    PAYLOAD_INDEXES = ["source_type", "source_name", "chunk_type", "api_name", "domain_group", "document_name"]

    def ensure_collection(self) -> None:
        client = self._get_client()
        collection_name = settings.qdrant.collection_name
        collections = [c.name for c in client.get_collections().collections]

        if collection_name not in collections:
            distance = DISTANCE_MAP.get(settings.embedding.distance_metric, Distance.COSINE)
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=settings.embedding.dimension,
                    distance=distance,
                ),
            )
            logger.info("Created collection '%s' (dim=%d)", collection_name, settings.embedding.dimension)
        else:
            logger.info("Collection '%s' already exists", collection_name)

        # Ensure payload indexes exist (idempotent — safe to call on every startup)
        for field_name in self.PAYLOAD_INDEXES:
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
                logger.debug("Ensured payload index on '%s'", field_name)
            except Exception as e:
                # Index may already exist, which is fine
                logger.debug("Payload index '%s' already exists or failed: %s", field_name, e)

    def upsert_batch(self, points: list[PointStruct]) -> None:
        if not points:
            return
        client = self._get_client()
        client.upsert(
            collection_name=settings.qdrant.collection_name,
            points=points,
        )
        logger.debug("Upserted %d points", len(points))

    def upsert_point(self, point_id: str, vector: list[float], payload: dict) -> None:
        client = self._get_client()
        payload["indexed_at"] = datetime.now(timezone.utc).isoformat()
        client.upsert(
            collection_name=settings.qdrant.collection_name,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )

    def search(
        self,
        query_vector: list[float],
        limit: int = 10,
        score_threshold: float | None = None,
        source_type: str | None = None,
        source_name: str | None = None,
        source_names: list[str] | None = None,
        api_name: str | None = None,
        chunk_type: str | None = None,
        domain_group: str | None = None,
        document_name: str | None = None,
    ) -> list[dict]:
        client = self._get_client()

        conditions = []
        if source_type:
            conditions.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))
        if source_names:
            # Multiple source names → OR filter (match any of the listed sources)
            conditions.append(
                Filter(
                    should=[FieldCondition(key="source_name", match=MatchValue(value=name)) for name in source_names]
                )
            )
        elif source_name:
            conditions.append(FieldCondition(key="source_name", match=MatchValue(value=source_name)))
        if api_name:
            conditions.append(FieldCondition(key="api_name", match=MatchValue(value=api_name)))
        if chunk_type:
            conditions.append(FieldCondition(key="chunk_type", match=MatchValue(value=chunk_type)))
        if domain_group:
            conditions.append(FieldCondition(key="domain_group", match=MatchValue(value=domain_group)))
        if document_name:
            conditions.append(FieldCondition(key="document_name", match=MatchValue(value=document_name)))

        query_filter = Filter(must=conditions) if conditions else None

        response = client.query_points(
            collection_name=settings.qdrant.collection_name,
            query=query_vector,
            limit=limit,
            score_threshold=score_threshold,
            query_filter=query_filter,
            with_payload=True,
        )
        return [
            {"id": hit.id, "score": hit.score, "payload": dict(hit.payload) if hit.payload else {}}
            for hit in response.points
        ]

    def get_content_hashes(self, point_ids: list[str]) -> dict[str, str]:
        """Fetch content_hash for a list of point IDs in one call."""
        if not point_ids:
            return {}
        client = self._get_client()
        try:
            points = client.retrieve(
                collection_name=settings.qdrant.collection_name,
                ids=point_ids,
                with_payload=["content_hash"],
                with_vectors=False,
            )
            return {str(p.id): p.payload.get("content_hash", "") for p in points if p.payload}
        except Exception as e:
            logger.warning("Failed to retrieve content hashes (%d ids): %s", len(point_ids), e)
            return {}

    @staticmethod
    def _chunk_id_to_point_id(chunk_id: str) -> str:
        """Convert a chunk_id to the deterministic UUID used as Qdrant point ID."""
        h = hashlib.md5(chunk_id.encode()).hexdigest()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    def fetch_by_chunk_ids(self, chunk_ids: list[str]) -> list[dict]:
        """Fetch points by their chunk_id values (direct lookup, no vector search).

        Uses the deterministic MD5 UUID mapping from chunk_id → point_id,
        then retrieves payloads in a single batch call.

        Returns results in the same format as search() for easy merging.
        """
        if not chunk_ids:
            return []

        point_ids = [self._chunk_id_to_point_id(cid) for cid in chunk_ids]
        client = self._get_client()

        try:
            points = client.retrieve(
                collection_name=settings.qdrant.collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=False,
            )
            results = []
            for p in points:
                if p.payload:
                    results.append(
                        {
                            "id": p.id,
                            "score": 0.0,  # not from vector search — no relevance score
                            "payload": dict(p.payload),
                            "_expanded": True,  # marker: fetched via relationship expansion
                        }
                    )
            logger.debug("Fetched %d/%d points by chunk_id", len(results), len(chunk_ids))
            return results
        except Exception as e:
            logger.warning("Failed to fetch by chunk_ids (%d ids): %s", len(chunk_ids), e)
            return []

    def get_collection_info(self) -> dict:
        client = self._get_client()
        name = settings.qdrant.collection_name
        try:
            info = client.get_collection(name)
            # vectors_count was dropped from CollectionInfo in newer qdrant-client
            # versions — guard it (and status) so a missing attribute doesn't
            # derail the whole call.
            return {
                "name": name,
                "points_count": getattr(info, "points_count", None),
                "vectors_count": getattr(info, "vectors_count", None),
                "status": info.status.value if getattr(info, "status", None) else "unknown",
            }
        except Exception as exc:  # noqa: BLE001
            # Don't collapse every error into "not_found" — that hid a populated
            # collection behind a client/transport hiccup (get_collection can
            # choke on a version-mismatched response while count/search still
            # work). Only report not_found when the collection truly doesn't
            # exist; otherwise surface the error and still get the real count.
            try:
                exists = client.collection_exists(name)
            except Exception:  # noqa: BLE001
                exists = None
            if exists is False:
                return {"name": name, "status": "not_found"}
            result: dict = {"name": name, "status": "error", "error": str(exc)}
            try:
                result["points_count"] = client.count(name, exact=True).count
                result["status"] = "degraded"  # reachable + counted; full info unavailable
            except Exception:  # noqa: BLE001
                pass
            logger.warning("get_collection_info(%s) failed: %s", name, exc)
            return result

    def delete_by_api(self, api_name: str) -> int:
        """Delete all points for a specific source (matches api_name OR source_name)."""
        client = self._get_client()
        result = client.delete(
            collection_name=settings.qdrant.collection_name,
            points_selector=Filter(
                should=[
                    FieldCondition(key="api_name", match=MatchValue(value=api_name)),
                    FieldCondition(key="source_name", match=MatchValue(value=api_name)),
                ]
            ),
        )
        logger.info("Deleted points for api_name/source_name='%s'", api_name)
        return result

    def facet_documents(self, limit: int = 2000) -> list[dict]:
        """Return unique document_name values and their chunk counts from Qdrant.

        Uses the Qdrant facet API — a single indexed lookup, no file reading.
        Replaces the old approach of scanning every chunk JSON file on disk.
        """
        client = self._get_client()
        try:
            response = client.facet(
                collection_name=settings.qdrant.collection_name,
                key="document_name",
                limit=limit,
                exact=True,
            )
            return [
                {"document_name": hit.value, "chunk_count": hit.count}
                for hit in response.hits
                if hit.value  # skip empty/null values
            ]
        except Exception as exc:
            logger.warning("facet_documents failed: %s", exc)
            return []

    def check_available(self) -> bool:
        try:
            client = self._get_client()
            client.get_collections()
            return True
        except Exception:
            logger.warning("Qdrant not reachable at %s:%d", settings.qdrant.host, settings.qdrant.port)
            return False

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None


vector_service = VectorService()
