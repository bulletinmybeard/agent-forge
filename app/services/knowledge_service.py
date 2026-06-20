"""Knowledge Database business logic and ingest pipeline.

Orchestrates: validate -> hash -> composite text -> embed -> dedup -> upsert.
"""

import hashlib
import logging
from datetime import datetime, timezone

from qdrant_client.models import PointStruct

from app.config import settings
from app.models.knowledge import (
    BulkDeleteRequest,
    CreateEntryRequest,
    KnowledgeSearchRequest,
    UpdateEntryRequest,
)
from app.services.dedup_service import DedupService
from app.services.dedup_service import dedup_service as _default_dedup
from app.services.embedding_service import EmbeddingService
from app.services.embedding_service import embedding_service as _default_embed
from app.services.knowledge_vector_service import (
    KnowledgeVectorService,
)
from app.services.knowledge_vector_service import (
    knowledge_vector_service as _default_vector,
)

logger = logging.getLogger(__name__)

REEMBED_FIELDS = {"content", "title", "notes"}


class KnowledgeService:
    def __init__(
        self,
        vector_service: KnowledgeVectorService | None = None,
        embedding_service: EmbeddingService | None = None,
        dedup_service: DedupService | None = None,
    ) -> None:
        self._vector = vector_service or _default_vector
        self._embed = embedding_service or _default_embed
        self._dedup = dedup_service or _default_dedup

    def _build_composite_text(self, title: str, notes: str | None, content: str) -> str:
        template = settings.knowledge.composite_template
        return template.format(title=title, notes=notes or "", content=content).strip()

    @staticmethod
    def _compute_content_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    def create_entry(self, request: CreateEntryRequest) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        content_hash = self._compute_content_hash(request.content)
        point_id = self._vector.generate_point_id(content_hash)

        existing_hashes = self._vector.get_content_hashes([point_id])
        if existing_hashes.get(point_id) == content_hash:
            existing = self._vector.get_by_id(point_id)
            if existing:
                return {**self._payload_to_response(point_id, existing["payload"]), "_conflict": True}

        composite_text = self._build_composite_text(request.title, request.notes, request.content)
        vector = self._embed.embed(composite_text)

        payload = {
            "text": composite_text,
            "title": request.title,
            "content": request.content,
            "content_type": request.content_type,
            "language": request.language,
            "tags": request.tags,
            "source_url": request.source_url,
            "notes": request.notes,
            "project": request.project,
            "content_hash": content_hash,
            "created_at": now,
            "updated_at": now,
            "indexed_at": now,
        }

        point = PointStruct(id=point_id, vector=vector, payload=payload)
        self._vector.upsert_batch([point])

        return self._payload_to_response(point_id, payload)

    def process_batch(self, entries: list[CreateEntryRequest]) -> dict:
        indexed = 0
        skipped = 0
        deduped = 0
        errors = 0

        texts = []
        chunks = []
        point_ids = []

        for entry in entries:
            content_hash = self._compute_content_hash(entry.content)
            pid = self._vector.generate_point_id(content_hash)
            composite = self._build_composite_text(entry.title, entry.notes, entry.content)
            texts.append(composite)
            chunks.append({"entry": entry, "content_hash": content_hash, "composite": composite})
            point_ids.append(pid)

        existing_hashes = self._vector.get_content_hashes(point_ids)
        filtered_texts = []
        filtered_chunks = []
        filtered_pids = []

        for i, pid in enumerate(point_ids):
            if existing_hashes.get(pid) == chunks[i]["content_hash"]:
                skipped += 1
            else:
                filtered_texts.append(texts[i])
                filtered_chunks.append(chunks[i])
                filtered_pids.append(pid)

        if not filtered_texts:
            return {"indexed": 0, "skipped": skipped, "deduped": 0, "errors": 0}

        try:
            embeddings = self._embed.embed_batch(filtered_texts)
        except Exception as e:
            logger.error("Batch embedding failed: %s", e)
            return {"indexed": 0, "skipped": skipped, "deduped": 0, "errors": len(filtered_texts)}

        dedup_chunks = [{"chunk_id": pid, "text": t} for pid, t in zip(filtered_pids, filtered_texts)]
        dedup_result = self._dedup.filter_duplicates(embeddings, dedup_chunks, filtered_pids)
        deduped = len(dedup_result.duplicates)

        now = datetime.now(timezone.utc).isoformat()
        points: list[PointStruct] = []
        for idx in dedup_result.kept:
            chunk = filtered_chunks[idx]
            entry = chunk["entry"]
            pid = filtered_pids[idx]
            try:
                payload = {
                    "text": chunk["composite"],
                    "title": entry.title,
                    "content": entry.content,
                    "content_type": entry.content_type,
                    "language": entry.language,
                    "tags": entry.tags,
                    "source_url": entry.source_url,
                    "notes": entry.notes,
                    "project": entry.project,
                    "content_hash": chunk["content_hash"],
                    "created_at": now,
                    "updated_at": now,
                    "indexed_at": now,
                }
                points.append(PointStruct(id=pid, vector=embeddings[idx], payload=payload))
                indexed += 1
            except Exception as e:
                logger.error("Failed to prepare point: %s", e)
                errors += 1

        if points:
            try:
                self._vector.upsert_batch(points)
            except Exception as e:
                logger.error("Batch upsert failed: %s", e)
                return {"indexed": 0, "skipped": skipped, "deduped": deduped, "errors": len(points)}

        return {"indexed": indexed, "skipped": skipped, "deduped": deduped, "errors": errors}

    def get_entry(self, point_id: str) -> dict | None:
        result = self._vector.get_by_id(point_id)
        if not result:
            return None
        return self._payload_to_response(point_id, result["payload"])

    def update_entry(self, point_id: str, request: UpdateEntryRequest) -> dict | None:
        existing = self._vector.get_by_id(point_id)
        if not existing:
            return None

        payload = dict(existing["payload"])
        now = datetime.now(timezone.utc).isoformat()

        updates = request.model_dump(exclude_none=True)
        needs_reembed = bool(updates.keys() & REEMBED_FIELDS)

        for key, value in updates.items():
            payload[key] = value
        payload["updated_at"] = now

        if needs_reembed:
            if "content" in updates:
                payload["content_hash"] = self._compute_content_hash(payload["content"])
            composite = self._build_composite_text(payload["title"], payload.get("notes"), payload["content"])
            payload["text"] = composite
            payload["indexed_at"] = now
            vector = self._embed.embed(composite)
            new_point_id = point_id
            if "content" in updates:
                new_point_id = self._vector.generate_point_id(payload["content_hash"])
                if new_point_id != point_id:
                    self._vector.delete_point(point_id)
            point = PointStruct(id=new_point_id, vector=vector, payload=payload)
            self._vector.upsert_batch([point])
            return self._payload_to_response(new_point_id, payload)

        self._vector.set_payload(point_id, payload)
        return self._payload_to_response(point_id, payload)

    def delete_entry(self, point_id: str) -> None:
        self._vector.delete_point(point_id)

    def delete_by_filter(self, request: BulkDeleteRequest) -> dict:
        self._vector.delete_by_filter(
            tags=request.tags,
            content_type=request.content_type,
            before=request.before,
            project=request.project,
        )
        return {"deleted": -1}

    def search(self, request: KnowledgeSearchRequest) -> dict:
        vector = self._embed.embed(request.query)
        results = self._vector.search(
            query_vector=vector,
            limit=request.limit,
            score_threshold=request.score_threshold,
            content_type=request.content_type,
            language=request.language,
            tags=request.tags,
            project=request.project,
        )
        return {
            "query": request.query,
            "results": [
                {
                    "id": r["id"],
                    "score": r["score"],
                    "title": r["payload"].get("title", ""),
                    "content": r["payload"].get("content", ""),
                    "content_type": r["payload"].get("content_type", ""),
                    "language": r["payload"].get("language"),
                    "tags": r["payload"].get("tags", []),
                    "source_url": r["payload"].get("source_url"),
                    "notes": r["payload"].get("notes"),
                    "project": r["payload"].get("project", "Uncategorized"),
                    "created_at": r["payload"].get("created_at", ""),
                }
                for r in results
            ],
            "count": len(results),
        }

    def get_tags(self) -> list[dict]:
        return self._vector.facet_tags()

    def get_stats(self) -> dict:
        info = self._vector.get_collection_info()
        by_type = self._vector.count_by_content_type()
        recent = self._vector.count_recent(days=7)
        tags = self._vector.facet_tags()
        return {
            "total_entries": info.get("points_count", 0) or 0,
            "by_content_type": by_type,
            "recent_entries": recent,
            "tag_count": len(tags),
        }

    @staticmethod
    def _payload_to_response(point_id: str, payload: dict) -> dict:
        return {
            "id": point_id,
            "title": payload.get("title", ""),
            "content": payload.get("content", ""),
            "content_type": payload.get("content_type", ""),
            "language": payload.get("language"),
            "tags": payload.get("tags", []),
            "source_url": payload.get("source_url"),
            "notes": payload.get("notes"),
            "project": payload.get("project", "Uncategorized"),
            "created_at": payload.get("created_at", ""),
            "updated_at": payload.get("updated_at", ""),
        }


knowledge_service = KnowledgeService()
