"""Semantic deduplication service for AgentForge.

Uses Qdrant vector similarity to detect near-duplicate chunks before indexing
and to surface documentation drift (docs that no longer match their code).

Integration point:
    indexer_service._process_batch() calls dedup_service.filter_duplicates()
    after embedding, before upserting.  Chunks whose embedding is ≥ threshold
    similar to an *existing* (different) point are flagged and skipped.
"""

import logging
from dataclasses import dataclass, field

from app.config import settings
from app.services.vector_service import vector_service

logger = logging.getLogger(__name__)


@dataclass
class DuplicateMatch:
    """A pair of semantically similar chunks."""

    new_chunk_id: str
    new_point_id: str
    existing_point_id: str
    existing_chunk_id: str
    score: float
    existing_source_type: str = ""
    existing_chunk_type: str = ""
    existing_text_preview: str = ""


@dataclass
class DedupResult:
    """Outcome of a dedup pass over a batch of embeddings."""

    kept: list[int] = field(default_factory=list)  # indices into the original batch
    duplicates: list[DuplicateMatch] = field(default_factory=list)  # skipped entries


@dataclass
class DriftMatch:
    """A doc chunk whose nearest code chunk has drifted (low similarity)."""

    doc_chunk_id: str
    doc_text_preview: str
    nearest_code_chunk_id: str
    nearest_code_text_preview: str
    score: float
    source_name: str = ""


class DedupService:
    """Semantic deduplication against existing Qdrant points."""

    @property
    def enabled(self) -> bool:
        return settings.dedup.enabled

    @property
    def threshold(self) -> float:
        return settings.dedup.similarity_threshold

    @property
    def drift_threshold(self) -> float:
        return settings.dedup.drift_threshold

    def filter_duplicates(
        self,
        embeddings: list[list[float]],
        chunks: list[dict],
        point_ids: list[str],
    ) -> DedupResult:
        """Check each embedding against existing Qdrant points."""
        result = DedupResult()

        if not self.enabled or not embeddings:
            result.kept = list(range(len(embeddings)))
            return result

        for idx, (vector, chunk, pid) in enumerate(zip(embeddings, chunks, point_ids)):
            dup = self._find_nearest_duplicate(vector, pid)
            if dup:
                chunk_id = chunk.get("chunk_id", "?")
                match = DuplicateMatch(
                    new_chunk_id=chunk_id,
                    new_point_id=pid,
                    existing_point_id=dup["id"],
                    existing_chunk_id=dup["payload"].get("chunk_id", dup["payload"].get("text", "")[:60]),
                    score=dup["score"],
                    existing_source_type=dup["payload"].get("source_type", ""),
                    existing_chunk_type=dup["payload"].get("chunk_type", ""),
                    existing_text_preview=dup["payload"].get("text", "")[:120],
                )
                result.duplicates.append(match)
                logger.info(
                    "Dedup: skipping '%s' — %.1f%% similar to existing '%s'",
                    chunk_id,
                    match.score * 100,
                    match.existing_chunk_id,
                )
            else:
                result.kept.append(idx)

        return result

    def _find_nearest_duplicate(self, vector: list[float], exclude_point_id: str) -> dict | None:
        """Search Qdrant for the closest existing point above the threshold.

        Excludes the point itself (for re-index scenarios) by filtering out the matching point_id from results.
        """
        try:
            hits = vector_service.search(
                query_vector=vector,
                limit=3,  # small limit — we only need the top match
                score_threshold=self.threshold,
            )
        except Exception as e:
            logger.warning("Dedup search failed (will keep chunk): %s", e)
            return None

        for hit in hits:
            if str(hit["id"]) != exclude_point_id:
                return hit

        return None

    def find_duplicates(
        self,
        source_name: str | None = None,
        source_type: str | None = None,
        limit: int = 500,
        threshold: float | None = None,
    ) -> list[DuplicateMatch]:
        """Scan existing points and report semantic duplicates.

        Fetches points from Qdrant, embeds their text, and searches for
        near-neighbours.  Useful for auditing an already-indexed collection.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        eff_threshold = threshold or self.threshold
        client = vector_service._get_client()
        collection = settings.qdrant.collection_name

        # Build scroll filter
        conditions = []
        if source_name:
            conditions.append(FieldCondition(key="source_name", match=MatchValue(value=source_name)))
        if source_type:
            conditions.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))
        scroll_filter = Filter(must=conditions) if conditions else None

        # Scroll through points
        points, _ = client.scroll(
            collection_name=collection,
            scroll_filter=scroll_filter,
            limit=limit,
            with_vectors=True,
            with_payload=True,
        )

        if not points:
            return []

        logger.info("Dedup scan: checking %d points for duplicates (threshold=%.3f)", len(points), eff_threshold)

        seen_pairs: set[tuple[str, str]] = set()
        duplicates: list[DuplicateMatch] = []

        for point in points:
            pid = str(point.id)
            vector = point.vector
            if not vector:
                continue

            hits = vector_service.search(
                query_vector=vector,
                limit=5,
                score_threshold=eff_threshold,
            )

            for hit in hits:
                hit_id = str(hit["id"])
                if hit_id == pid:
                    continue

                # Deduplicate pairs (A,B) == (B,A)
                pair_key = tuple(sorted([pid, hit_id]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                duplicates.append(
                    DuplicateMatch(
                        new_chunk_id=point.payload.get("chunk_id", pid) if point.payload else pid,
                        new_point_id=pid,
                        existing_point_id=hit_id,
                        existing_chunk_id=hit["payload"].get("chunk_id", hit_id),
                        score=hit["score"],
                        existing_source_type=hit["payload"].get("source_type", ""),
                        existing_chunk_type=hit["payload"].get("chunk_type", ""),
                        existing_text_preview=hit["payload"].get("text", "")[:120],
                    )
                )

        duplicates.sort(key=lambda d: d.score, reverse=True)
        logger.info("Dedup scan complete: found %d duplicate pairs", len(duplicates))
        return duplicates

    # ── Drift detection: docs vs code similarity check ───────────────────

    def detect_drift(
        self,
        source_name: str | None = None,
        limit: int = 200,
        threshold: float | None = None,
    ) -> list[DriftMatch]:
        """Find documentation chunks that have drifted from their nearest code.

        Searches each doc chunk against code chunks — low similarity indicates
        the documentation may be stale or describing something that's changed.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        eff_threshold = threshold or self.drift_threshold
        client = vector_service._get_client()
        collection = settings.qdrant.collection_name

        # Fetch doc chunks
        conditions = [FieldCondition(key="source_type", match=MatchValue(value="docs"))]
        if source_name:
            conditions.append(FieldCondition(key="source_name", match=MatchValue(value=source_name)))

        doc_points, _ = client.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=conditions),
            limit=limit,
            with_vectors=True,
            with_payload=True,
        )

        if not doc_points:
            # Also check document type (markdown docs)
            conditions[0] = FieldCondition(key="source_type", match=MatchValue(value="document"))
            doc_points, _ = client.scroll(
                collection_name=collection,
                scroll_filter=Filter(must=conditions),
                limit=limit,
                with_vectors=True,
                with_payload=True,
            )

        if not doc_points:
            logger.info("Drift check: no doc/document chunks found")
            return []

        logger.info(
            "Drift check: comparing %d doc chunks against code (threshold=%.3f)", len(doc_points), eff_threshold
        )

        # For each doc chunk, find nearest code chunk
        code_filter = Filter(
            must=[
                FieldCondition(key="source_type", match=MatchValue(value="code")),
            ]
        )

        drift_matches: list[DriftMatch] = []

        for point in doc_points:
            vector = point.vector
            if not vector:
                continue

            hits = client.query_points(
                collection_name=collection,
                query=vector,
                limit=1,
                query_filter=code_filter,
                with_payload=True,
            )

            if not hits.points:
                continue

            best = hits.points[0]
            if best.score < eff_threshold:
                payload = point.payload or {}
                best_payload = dict(best.payload) if best.payload else {}

                drift_matches.append(
                    DriftMatch(
                        doc_chunk_id=payload.get("chunk_id", str(point.id)),
                        doc_text_preview=payload.get("text", "")[:120],
                        nearest_code_chunk_id=best_payload.get("chunk_id", str(best.id)),
                        nearest_code_text_preview=best_payload.get("text", "")[:120],
                        score=best.score,
                        source_name=payload.get("source_name", ""),
                    )
                )

        drift_matches.sort(key=lambda d: d.score)
        logger.info("Drift check complete: %d chunks below threshold", len(drift_matches))
        return drift_matches


dedup_service = DedupService()
