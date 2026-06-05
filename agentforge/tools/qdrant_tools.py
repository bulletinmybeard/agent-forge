"""Qdrant admin tools - inspection, search, and maintenance for the vector database.

Provides collection listing, stats, point sampling, filtered counts,
source-name discovery, **similarity search**, and **point deletion**.

The ``search`` action embeds a query and finds semantically similar points,
with optional mode/payload filtering.  The ``delete`` action removes points
by Qdrant filter (e.g., all points with ``mode=monitor``) or by explicit
point IDs from a prior search.

Uses the same ``QdrantClient`` connection settings as the main search
pipeline (``app.config.settings.qdrant``).

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.qdrant_tools import register_qdrant_tools

    registry = ToolRegistry()
    register_qdrant_tools(registry)
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from typing import TYPE_CHECKING

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singleton client
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    """Lazy-init a QdrantClient from QDRANT_HOST / QDRANT_PORT (env)."""
    global _client
    if _client is not None:
        return _client
    try:
        from qdrant_client import QdrantClient

        host = os.environ.get("QDRANT_HOST", "localhost")
        port = int(os.environ.get("QDRANT_PORT", "6333"))

        _client = QdrantClient(host=host, port=port, timeout=10)
        return _client
    except ImportError:
        raise RuntimeError("qdrant-client package is not installed")


def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

_VALID_ACTIONS = ("collections", "info", "sample", "count", "sources", "search", "delete")


def _action_collections() -> str:
    """List all collections with point count and config."""
    client = _get_client()
    collections = client.get_collections().collections

    if not collections:
        return "No collections found in Qdrant."

    lines = [f"Qdrant Collections ({len(collections)})\n"]

    for coll in sorted(collections, key=lambda c: c.name):
        try:
            info = client.get_collection(coll.name)
            pts = info.points_count or 0
            vec_cfg = info.config.params.vectors
            # vec_cfg can be a dict (named vectors) or a VectorParams object
            if hasattr(vec_cfg, "size"):
                dim = vec_cfg.size
                dist = vec_cfg.distance.name if vec_cfg.distance else "?"
            elif isinstance(vec_cfg, dict):
                first = next(iter(vec_cfg.values()), None)
                dim = first.size if first and hasattr(first, "size") else "?"
                dist = first.distance.name if first and hasattr(first, "distance") else "?"
            else:
                dim = "?"
                dist = "?"

            status = info.status.name if info.status else "unknown"
            segments = info.segments_count or 0
            on_disk = "yes" if getattr(info.config.params, "on_disk", None) else "no"

            lines.append(f"{coll.name}")
            lines.append(f"  Points: {pts:,} | Vector: {dim}-dim {dist.lower()} | Segments: {segments}")
            lines.append(f"  Status: {status} | On-disk: {on_disk}")
            lines.append("")
        except Exception as exc:
            lines.append(f"{coll.name}")
            lines.append(f"  (error reading collection: {exc})")
            lines.append("")

    return "\n".join(lines).rstrip()


def _action_info(collection: str) -> str:
    """Detailed stats for one collection."""
    client = _get_client()
    info = client.get_collection(collection)

    pts = info.points_count or 0
    indexed = info.indexed_vectors_count or 0
    segments = info.segments_count or 0
    status = info.status.name if info.status else "unknown"

    vec_cfg = info.config.params.vectors
    if hasattr(vec_cfg, "size"):
        dim = vec_cfg.size
        dist = vec_cfg.distance.name if vec_cfg.distance else "?"
    elif isinstance(vec_cfg, dict):
        first = next(iter(vec_cfg.values()), None)
        dim = first.size if first and hasattr(first, "size") else "?"
        dist = first.distance.name if first and hasattr(first, "distance") else "?"
    else:
        dim = "?"
        dist = "?"

    on_disk = "yes" if getattr(info.config.params, "on_disk", None) else "no"

    opt = info.config.optimizer_config
    opt_status = "idle"
    if opt:
        opt_status = f"indexing_threshold={getattr(opt, 'indexing_threshold', '?')}"

    lines = [
        f"Collection: {collection}\n",
        f"Points:           {pts:,}",
        f"Indexed vectors:  {indexed:,}",
        f"Segments:         {segments}",
        f"Status:           {status}",
        "",
        f"Vector config:    {dim}-dim, {dist.lower()} distance",
        f"On-disk payload:  {on_disk}",
        f"Optimizer:        {opt_status}",
    ]
    return "\n".join(lines)


def _action_sample(collection: str, limit: int) -> str:
    """Retrieve N random points with payloads (no vectors)."""
    client = _get_client()
    limit = max(1, min(limit, 20))

    info = client.get_collection(collection)
    total = info.points_count or 0

    records, _offset = client.scroll(
        collection_name=collection,
        limit=limit,
        with_vectors=False,
        with_payload=True,
    )

    if not records:
        return f"Collection '{collection}' is empty (0 points)."

    lines = [f"Sample Points - {collection} ({len(records)} of {total:,})\n"]

    for i, pt in enumerate(records, 1):
        pid = str(pt.id)
        if len(pid) > 16:
            pid = pid[:16] + "..."
        lines.append(f"Point {i} (id: {pid})")

        payload = pt.payload or {}
        for key in ("source_name", "source_type", "chunk_type", "title", "api_name", "document_name"):
            if key in payload:
                lines.append(f"  {key}: {payload[key]}")

        content = payload.get("content", payload.get("text", ""))
        if content:
            preview = content[:200].replace("\n", " ")
            if len(content) > 200:
                preview += "..."
            lines.append(f"  content: [{len(content)} chars] {preview}")

        # Show any other payload keys not already shown
        shown_keys = {
            "source_name",
            "source_type",
            "chunk_type",
            "title",
            "api_name",
            "document_name",
            "content",
            "text",
        }
        extra = {k: v for k, v in payload.items() if k not in shown_keys}
        if extra:
            for k, v in sorted(extra.items()):
                val_str = str(v)
                if len(val_str) > 80:
                    val_str = val_str[:80] + "..."
                lines.append(f"  {k}: {val_str}")

        lines.append("")

    return "\n".join(lines).rstrip()


def _action_count(collection: str, filter_json: str) -> str:
    """Count points, optionally filtered."""
    client = _get_client()

    count_filter = None
    filter_desc = "(no filter)"

    if filter_json:
        try:
            from qdrant_client.models import Filter as QFilter

            raw = json.loads(filter_json)
            count_filter = QFilter(**raw)
            filter_desc = filter_json
        except json.JSONDecodeError as exc:
            return f"Error: invalid filter JSON - {exc}"
        except Exception as exc:
            return f"Error: invalid Qdrant filter - {exc}"

    result = client.count(collection_name=collection, count_filter=count_filter, exact=True)

    lines = [
        f"Count - {collection}",
        f"Filter: {filter_desc}",
        f"Result: {result.count:,} points",
    ]
    return "\n".join(lines)


def _action_sources(collection: str) -> str:
    """Discover all source_name values with counts."""
    client = _get_client()

    info = client.get_collection(collection)
    total = info.points_count or 0

    if total == 0:
        return f"Collection '{collection}' is empty (0 points)."

    # For large collections, sample instead of full scan
    is_estimated = False
    sample_size = total

    if total > 50_000:
        is_estimated = True
        sample_size = 5_000

    source_counter: Counter = Counter()
    type_counter: Counter = Counter()
    source_type_map: dict[str, str] = {}

    offset = None
    collected = 0
    batch_size = 256

    while collected < sample_size:
        remaining = sample_size - collected
        fetch = min(batch_size, remaining)

        records, offset = client.scroll(
            collection_name=collection,
            limit=fetch,
            offset=offset,
            with_vectors=False,
            with_payload=True,
        )

        if not records:
            break

        for pt in records:
            payload = pt.payload or {}
            src = payload.get("source_name", "(unknown)")
            stype = payload.get("source_type", "(unknown)")
            source_counter[src] += 1
            type_counter[stype] += 1
            if src not in source_type_map:
                source_type_map[src] = stype

        collected += len(records)

        if offset is None:
            break

    # Build output
    est_label = " (estimated)" if is_estimated else ""
    lines = [f"Indexed Sources - {collection} ({total:,} points{est_label})\n"]

    # Header
    lines.append(f"{'source_type':<15} {'source_name':<25} {'points':>8}")
    lines.append(f"{'-' * 15} {'-' * 25} {'-' * 8}")

    # Sort by source_type then source_name
    for src, cnt in sorted(source_counter.items(), key=lambda x: (source_type_map.get(x[0], ""), x[0])):
        stype = source_type_map.get(src, "?")
        if is_estimated:
            estimated_count = int((cnt / sample_size) * total)
            lines.append(f"{stype:<15} {src:<25} ~{estimated_count:>7,}")
        else:
            lines.append(f"{stype:<15} {src:<25} {cnt:>8,}")

    lines.append("")
    lines.append(f"Total: {len(source_counter)} sources across {len(type_counter)} types")

    return "\n".join(lines)


def _action_search(collection: str, query: str, filter_json: str, limit: int) -> str:
    """Similarity search - find points by semantic query, optionally filtered by mode."""
    client = _get_client()
    limit = max(1, min(limit, 20))

    # Build optional filter
    search_filter = None
    if filter_json:
        try:
            from qdrant_client.models import Filter as QFilter

            raw = json.loads(filter_json)
            search_filter = QFilter(**raw)
        except (json.JSONDecodeError, Exception) as exc:
            return f"Error: invalid filter JSON - {exc}"

    # Embed the query
    try:
        from app.services.embedding_service import embedding_service

        vector = embedding_service.embed(query[:800])
    except Exception as exc:
        return f"Error: embedding failed - {exc}"

    results = client.query_points(
        collection_name=collection,
        query=vector,
        limit=limit,
        query_filter=search_filter,
        with_payload=True,
    )

    if not results.points:
        return f"No matching points found in '{collection}' for query: {query[:80]}"

    lines = [f"Search Results - {collection} ({len(results.points)} hits)\n"]
    lines.append(f"Query: {query[:120]}")
    if filter_json:
        lines.append(f"Filter: {filter_json}")
    lines.append("")

    for i, hit in enumerate(results.points, 1):
        payload = hit.payload or {}
        pid = str(hit.id)
        score = f"{hit.score:.4f}" if hit.score is not None else "?"

        lines.append(f"Hit {i} (id: {pid}, score: {score})")

        # Show key payload fields
        for key in ("session_id", "mode", "model", "timestamp", "source_name", "source_type", "chunk_type", "title"):
            if key in payload:
                val = str(payload[key])
                if len(val) > 80:
                    val = val[:80] + "..."
                lines.append(f"  {key}: {val}")

        # Show query/response for conversation_memory
        for key in ("query", "response"):
            if key in payload:
                val = str(payload[key])
                preview = val[:200].replace("\n", " ")
                if len(val) > 200:
                    preview += "..."
                lines.append(f"  {key}: [{len(val)} chars] {preview}")

        # Show content/text for knowledge collections
        content = payload.get("content", payload.get("text", ""))
        if content and "query" not in payload:
            preview = content[:200].replace("\n", " ")
            if len(content) > 200:
                preview += "..."
            lines.append(f"  content: [{len(content)} chars] {preview}")

        lines.append("")

    return "\n".join(lines).rstrip()


def _action_delete(collection: str, filter_json: str, point_ids: str) -> str:
    """Delete points by filter (e.g., mode) or by explicit point IDs.

    Exactly one of filter_json or point_ids must be provided.
    filter_json: Qdrant filter as JSON, e.g., {"must": [{"key": "mode", "match": {"value": "monitor"}}]}
    point_ids: comma-separated point IDs to delete
    """
    client = _get_client()

    if filter_json and point_ids:
        return "Error: provide either filter_json or point_ids, not both."
    if not filter_json and not point_ids:
        return "Error: provide filter_json (e.g., mode filter) or point_ids (comma-separated IDs) to delete."

    # Count before deletion for reporting
    try:
        info_before = client.get_collection(collection)
        count_before = info_before.points_count or 0
    except Exception:
        count_before = -1

    if filter_json:
        try:
            from qdrant_client.models import Filter as QFilter

            raw = json.loads(filter_json)
            qfilter = QFilter(**raw)
        except (json.JSONDecodeError, Exception) as exc:
            return f"Error: invalid filter JSON - {exc}"

        # Count matching points first
        try:
            match_count = client.count(
                collection_name=collection,
                count_filter=qfilter,
                exact=True,
            ).count
        except Exception:
            match_count = "?"

        client.delete(collection_name=collection, points_selector=qfilter)

        # Count after
        try:
            info_after = client.get_collection(collection)
            count_after = info_after.points_count or 0
        except Exception:
            count_after = -1

        lines = [
            f"Deleted points from '{collection}'",
            f"Filter: {filter_json}",
            f"Matched: {match_count} points",
            f"Before: {count_before:,} -> After: {count_after:,}",
        ]
        return "\n".join(lines)

    else:
        # Delete by explicit point IDs
        ids = [pid.strip() for pid in point_ids.split(",") if pid.strip()]
        if not ids:
            return "Error: no valid point IDs provided."

        from qdrant_client.models import PointIdsList

        client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=ids),
        )

        try:
            info_after = client.get_collection(collection)
            count_after = info_after.points_count or 0
        except Exception:
            count_after = -1

        lines = [
            f"Deleted {len(ids)} point(s) from '{collection}'",
            f"IDs: {', '.join(ids[:10])}{'...' if len(ids) > 10 else ''}",
            f"Before: {count_before:,} -> After: {count_after:,}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


@tool
def qdrant_admin(
    action: str,
    collection: str = "",
    query: str = "",
    filter_json: str = "",
    point_ids: str = "",
    limit: int = 5,
) -> str:
    """Inspect and manage the Qdrant vector database.

    action: one of: collections, info, sample, count, sources, search, delete
    collection: collection name (required for most actions)
    query: search text for similarity search (required for 'search' action)
    filter_json: optional Qdrant filter as JSON string (for count, search, delete)
      - Mode filter example: {"must": [{"key": "mode", "match": {"value": "monitor"}}]}
    point_ids: comma-separated point IDs to delete (for 'delete' action, alternative to filter_json)
    limit: number of points to return for sample/search actions (default 5, max 20)
    """
    action = action.strip().lower()

    if action not in _VALID_ACTIONS:
        return f'Error: unknown action "{action}". Valid: {", ".join(_VALID_ACTIONS)}'

    # Validate collection requirement
    needs_collection = {"info", "sample", "count", "sources", "search", "delete"}
    if action in needs_collection and not collection:
        # Try to auto-detect if there's only one collection (or use default)
        try:
            client = _get_client()
            colls = [c.name for c in client.get_collections().collections]
            if len(colls) == 1:
                collection = colls[0]
            elif not collection:
                return (
                    f'Error: "collection" is required for action "{action}". '
                    f"Available: {', '.join(sorted(colls)) or '(none)'}"
                )
        except Exception as exc:
            return f"Error: cannot connect to Qdrant - {exc}"

    try:
        if action == "collections":
            return _action_collections()
        elif action == "info":
            return _action_info(collection)
        elif action == "sample":
            return _action_sample(collection, limit)
        elif action == "count":
            return _action_count(collection, filter_json)
        elif action == "sources":
            return _action_sources(collection)
        elif action == "search":
            if not query:
                return 'Error: "query" is required for action "search".'
            return _action_search(collection, query, filter_json, limit)
        elif action == "delete":
            return _action_delete(collection, filter_json, point_ids)
        else:
            return f'Error: unknown action "{action}"'
    except Exception as exc:
        exc_str = str(exc).lower()
        if "not found" in exc_str or "doesn't exist" in exc_str:
            # Collection not found - list available ones
            try:
                client = _get_client()
                colls = [c.name for c in client.get_collections().collections]
                return f'Error: collection "{collection}" not found. Available: {", ".join(sorted(colls)) or "(none)"}'
            except Exception:
                return f'Error: collection "{collection}" not found.'
        if "connect" in exc_str or "refused" in exc_str or "timeout" in exc_str:
            return f"Error: cannot connect to Qdrant - {exc}"
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_qdrant_tools(registry: ToolRegistry) -> int:
    """Register Qdrant inspection tools. Returns count."""
    registry.register_category_hint(
        "Qdrant",
        "Qdrant tools inspect and manage the vector database used by AgentForge search. "
        "Supports read-only inspection (collections, info, sample, count, sources), "
        "similarity search, and point deletion by filter or ID. "
        "Use 'sources' action to discover available #hashtag filters.",
    )
    tools = [qdrant_admin]
    for func in tools:
        registry.register(func, category="Qdrant")
    return len(tools)
