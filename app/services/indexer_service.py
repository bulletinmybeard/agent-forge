import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client.models import PointStruct

from app.config import settings
from app.services.dedup_service import dedup_service
from app.services.embedding_service import embedding_service
from app.services.vector_service import vector_service

logger = logging.getLogger(__name__)


class IndexerService:
    """Reads chunk JSON files from disk and indexes them into Qdrant."""

    def __init__(self) -> None:
        configured = Path(settings.indexer.chunks_dir)
        if configured.exists():
            self._chunks_dir = configured
        else:
            # Fallback: try data/chunks relative to the project root
            # (covers local dev where config still has the Docker path /app/chunks)
            # Docker maps ./data/chunks → /app/chunks; locally the project root
            # is one level above agentforge/
            service_root = Path(__file__).resolve().parent.parent.parent
            project_root = service_root.parent
            local_candidate = project_root / "data" / "chunks"
            if local_candidate.exists():
                logger.info(
                    "Configured chunks_dir %s not found — using local fallback %s",
                    configured,
                    local_candidate,
                )
                self._chunks_dir = local_candidate
            else:
                self._chunks_dir = configured  # keep original; will warn later
        self._batch_size = settings.indexer.batch_size

    @staticmethod
    def _generate_point_id(chunk_id: str) -> str:
        """Generate a deterministic UUID from chunk_id for Qdrant point ID.

        Qdrant stores 32-char hex strings as UUIDs with dashes (8-4-4-4-12),
        so we format the MD5 hash accordingly to ensure retrieve() lookups match.
        """
        h = hashlib.md5(chunk_id.encode()).hexdigest()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    @staticmethod
    def _load_chunk_file(path: Path) -> dict | None:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to load chunk file %s: %s", path, e)
            return None

    @staticmethod
    def _discover_chunk_files(api_dir: Path) -> list[Path]:
        """Discover all JSON chunk files for a version directory.

        Handles both OpenAPI layout (endpoints/, schemas/) and SQL Schema layout
        (tables/, _relationships.json).
        """
        chunk_files: list[Path] = []

        # _summary.json at the version root
        summary_file = api_dir / "_summary.json"
        if summary_file.exists():
            chunk_files.append(summary_file)

        # _relationships.json (SQL Schema)
        relationships_file = api_dir / "_relationships.json"
        if relationships_file.exists():
            chunk_files.append(relationships_file)

        # Subdirectories: endpoints/, schemas/, tables/, commands/, classes/, functions/, modules/, sections/
        for subdir_name in (
            "endpoints",
            "schemas",
            "tables",
            "commands",
            "classes",
            "functions",
            "modules",
            "sections",
        ):
            subdir = api_dir / subdir_name
            if subdir.is_dir():
                chunk_files.extend(sorted(subdir.glob("*.json")))

        return chunk_files

    @staticmethod
    def _find_latest_version_dir(api_dir: Path) -> Path | None:
        """Find the latest version directory inside an API chunks dir."""
        version_dirs = [d for d in api_dir.iterdir() if d.is_dir() and d.name.startswith("v")]
        if not version_dirs:
            return None
        # Sort by directory name (semver-ish), pick last
        return sorted(version_dirs, key=lambda d: d.name)[-1]

    def discover_sources(self) -> list[dict]:
        """List all knowledge sources and their latest versions found in the chunks directory.

        Scans: chunks/{source_type}/{source_name}/v{version}/
        """
        if not self._chunks_dir.exists():
            logger.warning("Chunks directory not found: %s", self._chunks_dir)
            return []

        sources = []
        for type_dir in sorted(self._chunks_dir.iterdir()):
            if not type_dir.is_dir():
                continue
            source_type = type_dir.name
            for source_dir in sorted(type_dir.iterdir()):
                if not source_dir.is_dir():
                    continue
                latest = self._find_latest_version_dir(source_dir)
                if latest:
                    chunk_files = self._discover_chunk_files(latest)
                    sources.append(
                        {
                            "source_type": source_type,
                            "source_name": source_dir.name,
                            "api_name": source_dir.name,  # backward compat alias
                            "version": latest.name,
                            "path": str(latest),
                            "chunk_count": len(chunk_files),
                        }
                    )
        return sources

    def discover_apis(self) -> list[dict]:
        """Backward-compatible alias for discover_sources."""
        return self.discover_sources()

    @staticmethod
    def discover_documents() -> list[dict]:
        """List unique document_name values and their chunk counts.

        Queries Qdrant directly using the facet API — a single indexed lookup
        on the ``document_name`` payload field.  This replaces the old approach
        of reading every chunk JSON file from disk, which was O(n_chunks) and
        scaled poorly as the knowledge base grew.

        Returns a list of ``{"document_name": str, "chunk_count": int}`` dicts
        sorted by document_name.  Falls back to an empty list if Qdrant is
        unavailable (e.g., during offline data preparation).
        """
        try:
            results = vector_service.facet_documents()
            return sorted(results, key=lambda d: d["document_name"])
        except Exception as exc:
            logger.warning("discover_documents: Qdrant facet failed (%s), returning empty list", exc)
            return []

    def _resolve_source_dir(self, source_name: str, source_type: str | None = None) -> Path | None:
        """Resolve the source directory under the new layout.

        If source_type is given, looks at chunks/{source_type}/{source_name}/.
        If not given, scans all source_type directories for a match.
        """
        if source_type:
            candidate = self._chunks_dir / source_type / source_name
            return candidate if candidate.is_dir() else None

        # Auto-discover: scan all source_type dirs
        if not self._chunks_dir.exists():
            return None
        for type_dir in sorted(self._chunks_dir.iterdir()):
            if not type_dir.is_dir():
                continue
            candidate = type_dir / source_name
            if candidate.is_dir():
                return candidate
        return None

    def index_api(
        self,
        api_name: str,
        version: str | None = None,
        clean: bool = False,
        source_type: str | None = None,
        batch_size: int | None = None,
        embed_timeout: float | None = None,
    ) -> dict:
        """Index all chunks for a specific source into Qdrant."""
        # Resolve the source directory using the new layout
        source_dir = self._resolve_source_dir(api_name, source_type)
        if source_dir is None:
            return {"error": f"Source directory not found for '{api_name}' (source_type={source_type})", "indexed": 0}

        if version:
            version_dir = source_dir / version
            if not version_dir.is_dir():
                return {"error": f"Version directory not found: {version_dir}", "indexed": 0}
        else:
            version_dir = self._find_latest_version_dir(source_dir)
            if not version_dir:
                return {"error": f"No version directories found in {source_dir}", "indexed": 0}
            version = version_dir.name

        # Ensure collection exists
        vector_service.ensure_collection()

        # Optionally clean existing points for this API
        if clean:
            logger.info("Cleaning existing points for api_name='%s'", api_name)
            vector_service.delete_by_api(api_name)

        # Discover chunk files
        chunk_files = self._discover_chunk_files(version_dir)
        if not chunk_files:
            return {"api_name": api_name, "version": version, "indexed": 0, "skipped": 0, "unchanged": 0, "errors": 0}

        effective_batch_size = batch_size or self._batch_size

        logger.info(
            "Indexing %d chunks for %s %s (batch_size=%d%s)",
            len(chunk_files),
            api_name,
            version,
            effective_batch_size,
            f", embed_timeout={embed_timeout}s" if embed_timeout else "",
        )

        # ── Load all chunks and check which ones actually need indexing ──
        loaded_chunks: list[tuple[Path, dict]] = []
        errors = 0

        for chunk_path in chunk_files:
            chunk_data = self._load_chunk_file(chunk_path)
            if chunk_data is None:
                errors += 1
                continue
            text = chunk_data.get("text", "")
            if not text.strip():
                logger.warning("Empty text in chunk %s, skipping", chunk_path.name)
                continue
            loaded_chunks.append((chunk_path, chunk_data))

        # Build point_id → content_hash mapping for all loaded chunks
        id_to_chunk: dict[str, dict] = {}
        for _, chunk_data in loaded_chunks:
            chunk_id = chunk_data.get("chunk_id", "")
            point_id = self._generate_point_id(chunk_id)
            id_to_chunk[point_id] = chunk_data

        # Fetch existing content hashes from Qdrant in one call
        existing_hashes = vector_service.get_content_hashes(list(id_to_chunk.keys()))

        # Filter: only embed chunks whose content_hash differs or doesn't exist
        chunks_to_index: list[dict] = []
        unchanged = 0

        for point_id, chunk_data in id_to_chunk.items():
            new_hash = chunk_data.get("content_hash", "")
            existing_hash = existing_hashes.get(point_id, "")

            if new_hash and new_hash == existing_hash:
                unchanged += 1
                continue

            chunks_to_index.append(chunk_data)

        if unchanged:
            logger.info("Skipping %d unchanged chunks (content_hash match)", unchanged)

        if not chunks_to_index:
            stats = {
                "api_name": api_name,
                "version": version,
                "total_chunks": len(chunk_files),
                "indexed": 0,
                "unchanged": unchanged,
                "errors": errors,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            logger.info("Nothing to index (all chunks unchanged): %s", stats)
            return stats

        logger.info("Embedding %d new/changed chunks (skipped %d unchanged)", len(chunks_to_index), unchanged)

        # ── Batch embed, dedup, and upsert only the new/changed chunks ──
        indexed = 0
        deduped = 0
        batch_texts: list[str] = []
        batch_chunks: list[dict] = []

        for chunk_data in chunks_to_index:
            batch_texts.append(chunk_data.get("text", ""))
            batch_chunks.append(chunk_data)

            if len(batch_texts) >= effective_batch_size:
                result = self._process_batch(batch_texts, batch_chunks, embed_timeout=embed_timeout)
                indexed += result["indexed"]
                deduped += result.get("deduped", 0)
                errors += result["errors"]
                batch_texts = []
                batch_chunks = []

        # Process remaining
        if batch_texts:
            result = self._process_batch(batch_texts, batch_chunks, embed_timeout=embed_timeout)
            indexed += result["indexed"]
            deduped += result.get("deduped", 0)
            errors += result["errors"]

        stats = {
            "api_name": api_name,
            "version": version,
            "total_chunks": len(chunk_files),
            "indexed": indexed,
            "unchanged": unchanged,
            "deduped": deduped,
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Indexing complete: %s", stats)
        return stats

    def _process_batch(self, texts: list[str], chunks: list[dict], embed_timeout: float | None = None) -> dict:
        """Embed a batch of texts, run semantic dedup, and upsert into Qdrant."""
        indexed = 0
        deduped = 0
        errors = 0

        try:
            embeddings = embedding_service.embed_batch(texts, timeout=embed_timeout)
        except Exception as e:
            logger.error("Batch embedding failed: %s", e)
            return {"indexed": 0, "deduped": 0, "errors": len(texts)}

        # ── Semantic dedup: filter out near-duplicates of existing points ──
        point_ids = [self._generate_point_id(c.get("chunk_id", "")) for c in chunks]
        dedup_result = dedup_service.filter_duplicates(embeddings, chunks, point_ids)
        deduped = len(dedup_result.duplicates)

        if deduped:
            logger.info("Semantic dedup: skipping %d duplicate chunks in this batch", deduped)

        # Only upsert the kept chunks
        points: list[PointStruct] = []
        for idx in dedup_result.kept:
            chunk_data = chunks[idx]
            vector = embeddings[idx]
            try:
                chunk_id = chunk_data.get("chunk_id", "")
                point_id = self._generate_point_id(chunk_id)

                payload = chunk_data.get("payload", {})
                payload["text"] = chunk_data.get("text", "")
                # Persist content_hash so the incremental skip (get_content_hashes)
                # can match it next run — without this every chunk re-embeds every time.
                payload["content_hash"] = chunk_data.get("content_hash", "")
                payload["indexed_at"] = datetime.now(timezone.utc).isoformat()

                points.append(
                    PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload,
                    )
                )
                indexed += 1
            except Exception as e:
                logger.error("Failed to prepare point for chunk '%s': %s", chunk_data.get("chunk_id"), e)
                errors += 1

        if points:
            try:
                vector_service.upsert_batch(points)
            except Exception as e:
                logger.error("Batch upsert failed: %s", e)
                return {"indexed": 0, "deduped": deduped, "errors": len(points)}

        return {"indexed": indexed, "deduped": deduped, "errors": errors}

    def index_all(self, clean: bool = False) -> list[dict]:
        """Index all discovered sources."""
        sources = self.discover_sources()
        results = []
        for source_info in sources:
            stats = self.index_api(
                source_info["source_name"],
                source_info["version"],
                clean=clean,
                source_type=source_info["source_type"],
            )
            results.append(stats)
        return results


indexer_service = IndexerService()
