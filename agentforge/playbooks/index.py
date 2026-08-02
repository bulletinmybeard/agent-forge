"""Qdrant index over playbook retrieval-text (description + trigger examples).

Files (playbooks.yaml) are the source of truth; this is only a search layer.
Modelled on app/services/knowledge_vector_service.py but minimal — no chunking,
dedup pipeline, or facets. Heavy deps (qdrant_client, app.config,
embedding_service) are imported lazily so models/loader stay import-safe.
"""

from __future__ import annotations

import logging
import uuid

from .models import PlaybookLibrary

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "agentforge_playbooks"
_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_DNS


class PlaybookIndex:
    def __init__(self, collection_name: str | None = None) -> None:
        self._collection_name = collection_name
        self._client = None

    @property
    def _collection(self) -> str:
        if self._collection_name:
            return self._collection_name
        try:
            from app.config import settings

            return settings.playbooks.collection_name
        except Exception:
            return DEFAULT_COLLECTION

    def _get_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            from app.config import settings

            self._client = QdrantClient(host=settings.qdrant.host, port=settings.qdrant.port)
        return self._client

    @staticmethod
    def _point_id(playbook_id: str) -> str:
        """Deterministic point id so re-index upserts (not duplicates) a playbook."""
        return str(uuid.uuid5(_NAMESPACE, playbook_id))

    def ensure_collection(self) -> None:
        from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

        from app.config import settings

        client = self._get_client()
        existing = [c.name for c in client.get_collections().collections]
        if self._collection not in existing:
            distance = {"Cosine": Distance.COSINE, "Euclidean": Distance.EUCLID, "Dot": Distance.DOT}.get(
                settings.embedding.distance_metric, Distance.COSINE
            )
            client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=settings.embedding.dimension, distance=distance),
            )
            logger.info("Created collection '%s' (dim=%d)", self._collection, settings.embedding.dimension)
        for field_name in ("playbook_id", "hash"):
            try:
                client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception:
                logger.debug("Payload index '%s' already exists or failed", field_name)

    def _existing_hashes(self) -> dict[str, str]:
        """playbook_id -> stored hash, for content-addressed skip + prune."""
        client = self._get_client()
        hashes: dict[str, str] = {}
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=self._collection,
                with_payload=True,
                with_vectors=False,
                limit=256,
                offset=offset,
            )
            for p in points:
                payload = p.payload or {}
                pid = payload.get("playbook_id")
                if pid:
                    hashes[pid] = payload.get("hash", "")
            if offset is None:
                break
        return hashes

    def reindex(self, library: PlaybookLibrary) -> dict[str, int]:
        """(Re)embed changed playbooks, upsert them, prune deleted ones.

        Idempotent: a playbook whose retrieval-text hash is unchanged is skipped.
        """
        from qdrant_client.models import PointStruct

        from app.services.embedding_service import embedding_service

        self.ensure_collection()
        existing = self._existing_hashes()

        points: list = []
        skipped = 0
        for pb in library.playbooks.values():
            new_hash = pb.content_hash()
            if existing.get(pb.id) == new_hash:
                skipped += 1
                continue
            vector = embedding_service.embed(pb.index_text())
            points.append(
                PointStruct(
                    id=self._point_id(pb.id),
                    vector=vector,
                    payload={"playbook_id": pb.id, "hash": new_hash},
                )
            )

        client = self._get_client()
        if points:
            client.upsert(collection_name=self._collection, points=points)

        stale = [pid for pid in existing if pid not in library.playbooks]
        if stale:
            client.delete(collection_name=self._collection, points_selector=[self._point_id(p) for p in stale])

        result = {"indexed": len(points), "skipped": skipped, "pruned": len(stale)}
        logger.info("Playbook reindex: %s", result)
        return result

    def search_one(self, query: str) -> tuple[str, float] | None:
        """Return (playbook_id, score) for the single best match, or None."""
        from app.services.embedding_service import embedding_service

        vector = embedding_service.embed(query)
        client = self._get_client()
        response = client.query_points(
            collection_name=self._collection,
            query=vector,
            limit=1,
            with_payload=True,
        )
        if not response.points:
            return None
        hit = response.points[0]
        payload = hit.payload or {}
        pid = payload.get("playbook_id")
        if not pid:
            return None
        return (pid, float(hit.score))
