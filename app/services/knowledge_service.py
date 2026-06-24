"""Knowledge Database business logic and ingest pipeline.

Orchestrates: validate -> hash -> composite text -> embed -> dedup -> upsert.
"""

import hashlib
import logging
import math
import re
import threading
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
PAGE_MARKER_RE = re.compile(r"---\s*Page\s+(\d+)\s*---")

_CHUNK_LOCK = threading.Lock()


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

        pages = self._split_pages(request.content)
        has_chunks = len(pages) > 1

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
            "metadata": request.metadata,
            "parent_id": request.parent_id,
            "content_hash": content_hash,
            "created_at": now,
            "updated_at": now,
            "indexed_at": now,
        }

        if has_chunks:
            payload["has_chunks"] = True
            payload["chunk_count"] = len(pages)

        point = PointStruct(id=point_id, vector=vector, payload=payload)
        self._vector.upsert_batch([point])

        if has_chunks:
            threading.Thread(
                target=self._create_page_chunks,
                args=(point_id, pages, payload, now),
                daemon=True,
            ).start()

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
                    "metadata": entry.metadata,
                    "parent_id": entry.parent_id,
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
                    self._vector.delete_by_parent(point_id)
                    self._vector.delete_point(point_id)

            # Re-create page chunks if content changed
            if "content" in updates:
                self._vector.delete_by_parent(new_point_id)
                pages = self._split_pages(payload["content"])
                if len(pages) > 1:
                    payload["has_chunks"] = True
                    payload["chunk_count"] = len(pages)
                else:
                    payload.pop("has_chunks", None)
                    payload.pop("chunk_count", None)

            point = PointStruct(id=new_point_id, vector=vector, payload=payload)
            self._vector.upsert_batch([point])

            if "content" in updates and payload.get("has_chunks"):
                threading.Thread(
                    target=self._create_page_chunks,
                    args=(new_point_id, pages, payload, now),
                    daemon=True,
                ).start()

            return self._payload_to_response(new_point_id, payload)

        self._vector.set_payload(point_id, payload)
        return self._payload_to_response(point_id, payload)

    def delete_entry(self, point_id: str) -> None:
        self._vector.delete_by_parent(point_id)
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
            parent_id=request.parent_id,
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
                    "metadata": r["payload"].get("metadata"),
                    "parent_id": r["payload"].get("parent_id"),
                    "created_at": r["payload"].get("created_at", ""),
                }
                for r in results
            ],
            "count": len(results),
        }

    def get_context(self, point_id: str, query: str, top_k: int = 8, chunk_size: int = 500) -> dict | None:
        """Retrieve the most relevant passages from an entry.

        For entries with page chunks: native Qdrant vector search filtered by
        parent_id, plus adjacent pages for context.
        For entries without chunks: BM25 pre-filter + semantic re-ranking fallback.
        """
        entry = self._vector.get_by_id(point_id)
        if not entry:
            return None

        payload = entry["payload"]
        content = payload.get("content", "")
        if not content.strip():
            return {"passages": [], "entry_title": payload.get("title", "")}

        if payload.get("has_chunks"):
            return self._get_context_from_chunks(point_id, query, top_k, payload)

        return self._get_context_bm25(content, query, top_k, chunk_size, payload)

    def _get_context_from_chunks(self, parent_id: str, query: str, top_k: int, payload: dict) -> dict:
        """Use native Qdrant vector search across page chunks."""
        query_vector = self._embed.embed(query)

        hits = self._vector.search_chunks_by_parent(parent_id, query_vector, limit=top_k)
        if not hits:
            logger.info("get_context_chunks: no vector hits for parent=%s", parent_id)
            return {
                "passages": [],
                "entry_title": payload.get("title", ""),
                "total_chunks": payload.get("chunk_count", 0),
            }

        # Collect matched page numbers + adjacent pages
        matched_pages = set()
        for hit in hits:
            page_num = hit["payload"].get("page_number", 0)
            matched_pages.add(page_num)
            matched_pages.add(page_num - 1)
            matched_pages.add(page_num + 1)
        matched_pages.discard(0)

        total_pages = payload.get("chunk_count", 0)
        if total_pages:
            matched_pages = {p for p in matched_pages if p <= total_pages}

        # Fetch adjacent pages that weren't in the search results
        hit_pages = {h["payload"].get("page_number", 0) for h in hits}
        adjacent_needed = matched_pages - hit_pages

        all_pages = {h["payload"].get("page_number", 0): h for h in hits}

        if adjacent_needed:
            all_chunks = self._vector.get_chunks_by_parent(parent_id)
            for chunk in all_chunks:
                pn = chunk["payload"].get("page_number", 0)
                if pn in adjacent_needed and pn not in all_pages:
                    all_pages[pn] = chunk

        sorted_pages = sorted(all_pages.items(), key=lambda x: x[0])

        passages = []
        for page_num, page_data in sorted_pages:
            score = page_data.get("score", 0.0)
            is_adjacent = page_num not in hit_pages
            passages.append(
                {
                    "text": page_data["payload"].get("content", ""),
                    "score": round(score, 4) if score else 0.0,
                    "position": page_num,
                    "page_number": page_num,
                    "is_adjacent": is_adjacent,
                }
            )

        logger.info(
            "get_context_chunks: %d passages (%d matched + %d adjacent) from %d total pages",
            len(passages),
            len(hit_pages),
            len(passages) - len(hit_pages),
            total_pages,
        )

        return {
            "passages": passages,
            "entry_title": payload.get("title", ""),
            "total_chunks": total_pages,
        }

    def _get_context_bm25(self, content: str, query: str, top_k: int, chunk_size: int, payload: dict) -> dict:
        """Fallback: BM25 pre-filter + semantic re-ranking for non-chunked entries."""
        chunks = self._chunk_content(content, chunk_size)
        if not chunks:
            return {"passages": [], "entry_title": payload.get("title", "")}

        logger.info("get_context_bm25: %d chunks from %d chars, query=%r", len(chunks), len(content), query[:80])

        candidates = self._bm25_prefilter(chunks, query, 15)
        if not candidates:
            return {"passages": [], "entry_title": payload.get("title", ""), "total_chunks": len(chunks)}

        texts_to_embed = [query] + [c["text"] for c in candidates]
        vectors = self._embed.embed_batch(texts_to_embed)
        query_vec = vectors[0]

        scored = []
        for i, chunk_vec in enumerate(vectors[1:]):
            sim = self._cosine_similarity(query_vec, chunk_vec)
            scored.append((sim, candidates[i]))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]
        top.sort(key=lambda x: x[1]["index"])

        return {
            "passages": [
                {"text": item["text"], "score": round(score, 4), "position": item["index"]} for score, item in top
            ],
            "entry_title": payload.get("title", ""),
            "total_chunks": len(chunks),
        }

    _MIN_PAGE_CHARS = 50
    _MERGE_THRESHOLD = 200
    _SIZE_CHUNK_CHARS = 2000
    _SIZE_CHUNK_OVERLAP = 200

    @staticmethod
    def _split_by_size(content: str) -> list[dict]:
        """Window marker-less content into overlapping fixed-size pages for retrieval."""
        size = KnowledgeService._SIZE_CHUNK_CHARS
        step = max(size - KnowledgeService._SIZE_CHUNK_OVERLAP, 1)
        pages = []
        page_num = 1
        for start in range(0, len(content), step):
            text = content[start : start + size]
            if text.strip():
                pages.append({"page_number": page_num, "text": text})
                page_num += 1
            if start + size >= len(content):
                break
        return pages

    @staticmethod
    def _split_pages(content: str) -> list[dict]:
        """Split content on page markers, merging short pages into neighbors.

        Marker-less content (JSON/code/single-line text) falls back to size-based windowing so
        large documents are still chunked and individually retrievable.
        """
        parts = PAGE_MARKER_RE.split(content)
        if len(parts) <= 1:
            if len(content) > KnowledgeService._SIZE_CHUNK_CHARS:
                return KnowledgeService._split_by_size(content)
            return []

        raw_pages = []
        for i in range(1, len(parts), 2):
            page_num = int(parts[i])
            text = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if text:
                raw_pages.append({"page_number": page_num, "text": text})

        if not raw_pages:
            return []

        # Merge short pages into the previous chunk to reduce embedding calls
        merged = [raw_pages[0]]
        for page in raw_pages[1:]:
            prev = merged[-1]
            if (
                len(prev["text"]) < KnowledgeService._MERGE_THRESHOLD
                or len(page["text"]) < KnowledgeService._MIN_PAGE_CHARS
            ):
                merged[-1] = {
                    "page_number": prev["page_number"],
                    "text": prev["text"] + "\n\n" + page["text"],
                }
            else:
                merged.append(page)

        # Drop any final chunk that's too short
        return [p for p in merged if len(p["text"]) >= KnowledgeService._MIN_PAGE_CHARS]

    _EMBED_BATCH_SIZE = 20

    def _create_page_chunks(
        self,
        parent_id: str,
        pages: list[dict],
        parent_payload: dict,
        now: str,
    ) -> None:
        """Serialize chunk creation so concurrent entry writes don't flood the embedder."""
        with _CHUNK_LOCK:
            self._create_page_chunks_locked(parent_id, pages, parent_payload, now)

    def _create_page_chunks_locked(
        self,
        parent_id: str,
        pages: list[dict],
        parent_payload: dict,
        now: str,
    ) -> None:
        """Create individual Qdrant points for each page of a document."""
        total_pages = len(pages)
        title = parent_payload.get("title", "")
        upserted = 0

        logger.info(
            "Creating %d page chunks for parent=%s (batches of %d)", total_pages, parent_id, self._EMBED_BATCH_SIZE
        )

        for batch_start in range(0, len(pages), self._EMBED_BATCH_SIZE):
            batch_pages = pages[batch_start : batch_start + self._EMBED_BATCH_SIZE]
            batch_texts = [p["text"] for p in batch_pages]
            logger.info(
                "Embedding+upserting batch %d-%d of %d pages",
                batch_start + 1,
                batch_start + len(batch_texts),
                total_pages,
            )

            try:
                batch_embeddings = self._embed.embed_batch(batch_texts)
            except Exception as e:
                logger.error("Chunk embedding failed for batch %d: %s", batch_start, e)
                continue

            points = []
            for i, page in enumerate(batch_pages):
                chunk_hash = self._compute_content_hash(f"{parent_id}:page:{page['page_number']}")
                chunk_id = self._vector.generate_point_id(chunk_hash)

                payload = {
                    "text": page["text"],
                    "title": f"{title} (Page {page['page_number']})",
                    "content": page["text"],
                    "content_type": parent_payload.get("content_type", ""),
                    "language": parent_payload.get("language"),
                    "tags": parent_payload.get("tags", []),
                    "source_url": parent_payload.get("source_url"),
                    "project": parent_payload.get("project", "Uncategorized"),
                    "parent_id": parent_id,
                    "page_number": page["page_number"],
                    "total_pages": total_pages,
                    "is_chunk": True,
                    "content_hash": chunk_hash,
                    "created_at": now,
                    "updated_at": now,
                    "indexed_at": now,
                }

                points.append(PointStruct(id=chunk_id, vector=batch_embeddings[i], payload=payload))

            self._vector.upsert_batch(points)
            upserted += len(points)

        logger.info("Finished: upserted %d page chunks for parent=%s", upserted, parent_id)

    @staticmethod
    def _bm25_prefilter(chunks: list[dict], query: str, limit: int) -> list[dict]:
        """Score chunks by term frequency and return the top candidates."""
        terms = re.findall(r"\w{2,}", query.lower())
        if not terms:
            return chunks[:limit]

        scored = []
        for chunk in chunks:
            lower = chunk["text"].lower()
            score = sum(lower.count(t) for t in terms)
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]

    @staticmethod
    def _chunk_content(content: str, target_size: int = 500) -> list[dict]:
        """Split content into chunks on page markers or paragraph boundaries."""
        pages = re.split(r"---\s*Page\s+\d+\s*---", content)
        chunks = []
        idx = 0

        for page in pages:
            page = page.strip()
            if not page:
                continue
            paragraphs = re.split(r"\n{2,}", page)
            current = ""
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                if current and len(current) + len(para) > target_size:
                    chunks.append({"text": current.strip(), "index": idx})
                    idx += 1
                    current = para
                else:
                    current = f"{current}\n\n{para}" if current else para

            if current.strip():
                chunks.append({"text": current.strip(), "index": idx})
                idx += 1

        return chunks

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def rechunk_entry(self, point_id: str) -> dict | None:
        """Re-create page chunks for an existing entry."""
        entry = self._vector.get_by_id(point_id)
        if not entry:
            return None

        payload = dict(entry["payload"])
        content = payload.get("content", "")
        pages = self._split_pages(content)

        # Delete existing chunks
        self._vector.delete_by_parent(point_id)

        if len(pages) <= 1:
            payload.pop("has_chunks", None)
            payload.pop("chunk_count", None)
            self._vector.set_payload(point_id, payload)
            return {"status": "no_pages", "chunks_created": 0}

        now = datetime.now(timezone.utc).isoformat()
        payload["has_chunks"] = True
        payload["chunk_count"] = len(pages)
        self._vector.set_payload(point_id, payload)

        threading.Thread(
            target=self._create_page_chunks,
            args=(point_id, pages, payload, now),
            daemon=True,
        ).start()

        return {"status": "ok", "chunks_creating": len(pages), "entry_id": point_id}

    def filter_entries(
        self,
        limit: int = 50,
        content_type: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        parent_id: str | None = None,
    ) -> dict:
        """Return entries matching filters (no vector search)."""
        results = self._vector.scroll_by_filter(
            limit=limit,
            content_type=content_type,
            tags=tags,
            project=project,
            parent_id=parent_id,
        )
        return {
            "results": [self._payload_to_response(r["id"], r["payload"]) for r in results],
            "count": len(results),
        }

    def list_overview(self, limit: int = 2000) -> dict:
        """Slim listing for the browse view: metadata only, no content body."""
        results = self._vector.list_slim(limit=limit)
        return {
            "results": [
                {
                    "id": r["id"],
                    "title": r["payload"].get("title", ""),
                    "content_type": r["payload"].get("content_type", ""),
                    "language": r["payload"].get("language"),
                    "tags": r["payload"].get("tags", []),
                    "parent_id": r["payload"].get("parent_id"),
                    "created_at": r["payload"].get("created_at", ""),
                    "metadata": r["payload"].get("metadata"),
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
            "metadata": payload.get("metadata"),
            "parent_id": payload.get("parent_id"),
            "created_at": payload.get("created_at", ""),
            "updated_at": payload.get("updated_at", ""),
        }


knowledge_service = KnowledgeService()
