"""Memory inspection + cleanup API — powers the Settings UI.

Endpoints:

    GET    /api/memory/stats           → counts + collection status
    GET    /api/memory/facts           → all user_facts rows
    DELETE /api/memory/facts/{key}     → delete one fact
    DELETE /api/memory/facts           → clear all facts
    GET    /api/memory/exchanges       → paginated cross-session memories
    DELETE /api/memory/exchanges/{id}  → delete one exchange
    DELETE /api/memory/exchanges       → clear all exchanges

All mutating endpoints are idempotent. Reads return empty lists when the
backend is unavailable rather than erroring — the Settings UI should stay
usable even if Qdrant is down.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from .api import get_db
from .conversation_memory import get_conversation_memory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats")
def memory_stats() -> dict[str, Any]:
    """Return counts and backend availability for the Settings header."""
    db = get_db()
    try:
        facts = db.get_all_facts(min_confidence=0.0)
        facts_count = len(facts)
    except Exception as exc:
        logger.warning("memory_stats: facts count failed: %s", exc)
        facts_count = 0

    mem = get_conversation_memory()
    conv_stats: dict[str, Any] = {"status": "disabled"}
    if mem is not None:
        try:
            conv_stats = mem.get_stats()
        except Exception as exc:
            logger.warning("memory_stats: conv stats failed: %s", exc)
            conv_stats = {"status": "error"}

    return {
        "facts_count": facts_count,
        "conversation_memory": conv_stats,
    }


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@router.get("/facts")
def list_facts() -> list[dict[str, Any]]:
    """Return all known facts, newest first."""
    db = get_db()
    try:
        facts = db.get_all_facts(min_confidence=0.0)
    except Exception as exc:
        logger.warning("list_facts failed: %s", exc)
        return []

    return [
        {
            "key": f.key,
            "value": f.value,
            "fact_type": f.fact_type,
            "confidence": f.confidence,
            "source_session": f.source_session,
            "updated_at": f.updated_at.isoformat() if f.updated_at else None,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in facts
    ]


@router.delete("/facts/{key}")
def delete_fact(key: str) -> dict[str, Any]:
    """Delete one fact by key."""
    db = get_db()
    deleted = db.delete_fact(key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Fact {key!r} not found")
    return {"deleted": True, "key": key}


@router.delete("/facts")
def clear_facts() -> dict[str, Any]:
    """Delete every fact row."""
    db = get_db()
    removed = db.delete_all_facts()
    return {"deleted": removed}


# ---------------------------------------------------------------------------
# Conversation memory (Qdrant)
# ---------------------------------------------------------------------------

# Hard cap per page — the Settings UI has no real use case for more than
# this, and it keeps payload size + Qdrant scroll time reasonable.
_EXCHANGES_MAX_LIMIT = 200


@router.get("/exchanges")
def list_exchanges(
    limit: int = 50,
    offset: str | None = None,
) -> dict[str, Any]:
    """Return a page of stored exchanges.

    Uses Qdrant's ``scroll`` API which returns a ``next_page_offset``
    token. The UI echoes that back as ``offset=<token>`` for the next
    call. ``offset=None`` (omit the query param) yields the first page.
    """
    limit = max(1, min(int(limit), _EXCHANGES_MAX_LIMIT))
    mem = get_conversation_memory()
    if mem is None:
        return {"exchanges": [], "next_offset": None}

    try:
        client = mem._get_client()  # intentional internal access — no public accessor yet
        points, next_page = client.scroll(
            collection_name=mem._collection,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:
        logger.warning("list_exchanges failed: %s", exc)
        return {"exchanges": [], "next_offset": None}

    exchanges = []
    for point in points:
        p = point.payload or {}
        exchanges.append(
            {
                "id": str(point.id),
                "session_id": p.get("session_id", ""),
                "mode": p.get("mode", ""),
                "model": p.get("model", ""),
                "query": p.get("query", ""),
                "response_preview": (p.get("response", "") or "")[:400],
                "timestamp": p.get("timestamp", ""),
            }
        )

    return {
        "exchanges": exchanges,
        "next_offset": str(next_page) if next_page is not None else None,
    }


@router.delete("/exchanges/{point_id}")
def delete_exchange(point_id: str) -> dict[str, Any]:
    """Delete one stored exchange by point id."""
    mem = get_conversation_memory()
    if mem is None:
        raise HTTPException(status_code=503, detail="Conversation memory not available")
    try:
        client = mem._get_client()
        client.delete(
            collection_name=mem._collection,
            points_selector=[point_id],
        )
    except Exception as exc:
        logger.warning("delete_exchange %s failed: %s", point_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"deleted": True, "id": point_id}


@router.delete("/exchanges")
def clear_exchanges() -> dict[str, Any]:
    """Delete every stored exchange."""
    mem = get_conversation_memory()
    if mem is None:
        raise HTTPException(status_code=503, detail="Conversation memory not available")
    ok = mem.delete_all()
    return {"deleted": bool(ok)}


# ---------------------------------------------------------------------------
# DB schemas — sql_extract_schema cache management
# ---------------------------------------------------------------------------


@router.get("/schemas")
def list_schemas() -> dict[str, Any]:
    """Return the list of configured databases with cache status per DB.

    Each entry:
        {
          "database": str,            # logical name used in sql_extract_schema
          "display_name": str,        # human label (from config.yaml)
          "engine": str,              # "mysql" | "postgres"
          "cached": bool,             # whether a cached schema is in Redis
          "cached_at": str | null,    # ISO-8601 timestamp
          "table_count": int,
          "total_columns": int,
          "view_count": int,
        }
    Plus top-level ``cache_disabled`` so the UI can render the global toggle.
    """
    try:
        from agentforge.tools.sql_schema_tool import (
            get_cached_schema_metadata,
            is_cache_disabled,
        )
        from app.services.db_service import db_service
    except Exception as exc:
        logger.warning("list_schemas: import failed: %s", exc)
        raise HTTPException(status_code=503, detail="DB service not available")

    databases = []
    for key in db_service.available_databases:
        entry = db_service._configs.get(key)
        meta = get_cached_schema_metadata(key) or {}
        databases.append(
            {
                "database": key,
                "display_name": getattr(entry, "name", "") or key,
                "engine": getattr(entry, "engine", "") or "",
                "cached": bool(meta),
                "cached_at": meta.get("cached_at"),
                "table_count": meta.get("table_count", 0),
                "total_columns": meta.get("total_columns", 0),
                "view_count": meta.get("view_count", 0),
            }
        )

    return {
        "cache_disabled": is_cache_disabled(),
        "databases": databases,
    }


@router.post("/schemas/{database}/scan")
def scan_schema(database: str) -> dict[str, Any]:
    """Run sql_extract_schema for *database* with ``force_refresh=True``.

    The tool is routed to the Mac role in ``tool_routing.yaml`` because the
    configured databases only exist on the macOS host — the agentforge-web
    container itself has no route to `localhost` DBs. Dispatching through
    the SAQ tools queue routes the job to the matching worker and returns
    the result. The worker writes the full schema into the shared Redis
    cache as a side effect, so after dispatch we read metadata from Redis
    just like any other call.
    """
    try:
        from agentforge.tools.routing import get_role_for_tool
        from agentforge.tools.sql_schema_tool import get_cached_schema_metadata
        from web.server.queue.dispatch_compat import saq_dispatch_tool
    except Exception as exc:
        logger.warning("scan_schema: import failed: %s", exc)
        raise HTTPException(status_code=503, detail="DB service not available")

    target_role = get_role_for_tool("sql_extract_schema")
    try:
        result = saq_dispatch_tool(
            "sql_extract_schema",
            {"database": database, "force_refresh": True},
            target_role=target_role,
        )
    except Exception as exc:
        logger.warning("scan_schema: SAQ dispatch failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=(
                f"{target_role!r} tools worker unreachable — can't scan {database!r}. "
                f"Check the SAQ tools worker for that role is running. ({exc})"
            ),
        )

    if result.startswith("ERROR:"):
        raise HTTPException(status_code=400, detail=result[len("ERROR:") :].strip())

    meta = get_cached_schema_metadata(database) or {}
    return {
        "database": database,
        "scanned": True,
        "table_count": meta.get("table_count", 0),
        "total_columns": meta.get("total_columns", 0),
        "view_count": meta.get("view_count", 0),
        "cached_at": meta.get("cached_at"),
    }


@router.delete("/schemas/{database}")
def clear_schema(database: str) -> dict[str, Any]:
    """Remove one cached schema."""
    from agentforge.tools.sql_schema_tool import clear_cached_schema

    ok = clear_cached_schema(database)
    if not ok:
        raise HTTPException(status_code=503, detail="Redis not available")
    return {"database": database, "cleared": True}


@router.delete("/schemas")
def clear_all_schemas() -> dict[str, Any]:
    """Remove every cached schema (disable flag is preserved)."""
    from agentforge.tools.sql_schema_tool import clear_all_cached_schemas

    removed = clear_all_cached_schemas()
    return {"cleared": removed}


@router.put("/schemas/cache/disabled")
def set_schema_cache_disabled(body: dict[str, Any]) -> dict[str, Any]:
    """Toggle the global "Always fetch fresh" switch.

    Request body: ``{"disabled": true | false}``
    """
    from agentforge.tools.sql_schema_tool import is_cache_disabled, set_cache_disabled

    disabled = bool(body.get("disabled"))
    set_cache_disabled(disabled)
    return {"cache_disabled": is_cache_disabled()}
