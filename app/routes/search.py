import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.services.code_context_service import enrich_results as enrich_code_context
from app.services.embedding_service import embedding_service
from app.services.memory_service import ChatMessage, memory_manager
from app.services.query_refiner import query_refiner
from app.services.rerank_service import rerank_results
from app.services.response_refiner import response_refiner
from app.services.vector_service import vector_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

OVERFETCH_FACTOR = settings.search.overfetch_factor
SCORE_FLOOR = settings.search.score_floor

# Maximum number of extra table chunks to fetch via relationship expansion.
# Keeps context budget bounded while covering the most common JOINs.
MAX_EXPANSION_CHUNKS = 10


def _expand_sql_relationships(results: list[dict]) -> list[dict]:
    """Expand sql-schema results by fetching related table chunks.

    Scans each table result's ``foreign_key_tables`` and ``referenced_by_tables``
    payload fields and fetches any table chunks that aren't already in the
    result set.  This gives the LLM complete column information for JOINs.

    Only activates when the dominant source_type is "sql-schema".
    Returns the original results with expanded chunks appended.
    """
    # Only expand for sql-schema results
    source_types = {r.get("payload", {}).get("source_type") for r in results}
    if "sql-schema" not in source_types:
        return results

    # Collect table names already in the result set
    existing_tables: dict[tuple[str, str], bool] = {}  # (source_name, table_name) → True
    for r in results:
        payload = r.get("payload", {})
        if payload.get("chunk_type") == "table":
            key = (payload.get("source_name", ""), payload.get("table_name", ""))
            existing_tables[key] = True

    # Collect related tables that are NOT in the result set
    needed_chunk_ids: list[str] = []
    seen: set[str] = set()

    for r in results:
        payload = r.get("payload", {})
        if payload.get("source_type") != "sql-schema" or payload.get("chunk_type") != "table":
            continue

        source_name = payload.get("source_name", "")
        if not source_name:
            continue

        # Tables this table references (FK targets)
        for ref_table in payload.get("foreign_key_tables", []):
            if (source_name, ref_table) not in existing_tables:
                chunk_id = f"{source_name}:table:{ref_table}"
                if chunk_id not in seen:
                    needed_chunk_ids.append(chunk_id)
                    seen.add(chunk_id)

        # Tables that reference this table (reverse FKs)
        for ref_table in payload.get("referenced_by_tables", []):
            if (source_name, ref_table) not in existing_tables:
                chunk_id = f"{source_name}:table:{ref_table}"
                if chunk_id not in seen:
                    needed_chunk_ids.append(chunk_id)
                    seen.add(chunk_id)

    if not needed_chunk_ids:
        return results

    # Cap the expansion to avoid flooding the context
    if len(needed_chunk_ids) > MAX_EXPANSION_CHUNKS:
        logger.info(
            "Relationship expansion: capping %d needed chunks to %d",
            len(needed_chunk_ids),
            MAX_EXPANSION_CHUNKS,
        )
        needed_chunk_ids = needed_chunk_ids[:MAX_EXPANSION_CHUNKS]

    expanded = vector_service.fetch_by_chunk_ids(needed_chunk_ids)
    if expanded:
        logger.info(
            "Relationship expansion: fetched %d related table chunks "
            "(from %d search results with %d total relationships)",
            len(expanded),
            len(results),
            len(needed_chunk_ids),
        )

    return results + expanded


class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    score_threshold: float | None = None
    score_floor: float | None = None  # override SCORE_FLOOR per request
    source_type: str | None = None
    source_name: str | None = None
    source_names: list[str] | None = None  # multiple sources → OR filter
    api_name: str | None = None
    chunk_type: str | None = None
    domain_group: str | None = None
    document_name: str | None = None
    include_examples: bool | None = None  # override refinement.output_examples per request
    brief: bool = False  # concise mode — direct answers, no extras
    session_id: str | None = None  # conversation session for memory continuity


@router.post("")
async def search_knowledge(req: SearchRequest) -> dict:
    """Search the knowledge base with a natural language query (raw Qdrant scores)."""
    query_vector = await embedding_service.aembed(req.query)

    results = vector_service.search(
        query_vector=query_vector,
        limit=req.limit,
        score_threshold=req.score_threshold,
        source_type=req.source_type,
        source_name=req.source_name,
        api_name=req.api_name,
        chunk_type=req.chunk_type,
        domain_group=req.domain_group,
    )

    return {
        "query": req.query,
        "results": results,
        "count": len(results),
    }


async def _smart_search_pipeline(req: SearchRequest) -> tuple[list[dict], dict]:
    """Shared search pipeline: input refinement → embed → score floor → re-rank."""
    # Optionally refine query via LLM
    refined_query = req.query
    was_refined = False

    if settings.refinement.input_enabled:
        refined_query, was_refined = await query_refiner.refine(
            req.query,
            source_type=req.source_type,
            source_name=req.source_name,
        )

    # Embed (refined or original) and search Qdrant
    query_vector = await embedding_service.aembed(refined_query)
    overfetch_limit = req.limit * OVERFETCH_FACTOR

    raw_results = vector_service.search(
        query_vector=query_vector,
        limit=overfetch_limit,
        score_threshold=req.score_threshold,
        source_type=req.source_type,
        source_name=req.source_name,
        source_names=req.source_names,
        api_name=req.api_name,
        chunk_type=req.chunk_type,
        domain_group=req.domain_group,
        document_name=req.document_name,
    )

    # Apply score floor
    floor = req.score_floor if req.score_floor is not None else SCORE_FLOOR
    floored_results = [r for r in raw_results if r.get("score", 0) >= floor]
    dropped_count = len(raw_results) - len(floored_results)

    if dropped_count:
        logger.debug("Score floor %.2f dropped %d results", floor, dropped_count)

    # Re-rank by intent (uses original query for intent detection)
    reranked, meta = rerank_results(floored_results, req.query, req.limit)

    meta["refined_query"] = refined_query if was_refined else None
    meta["was_refined"] = was_refined
    meta["score_floor"] = floor
    meta["dropped_by_floor"] = dropped_count

    return reranked, meta


@router.post("/smart")
async def smart_search(req: SearchRequest) -> dict:
    """Intent-aware search with optional LLM query refinement, re-ranking,
    and score floor. Returns raw result objects."""
    reranked, meta = await _smart_search_pipeline(req)

    return {
        "query": req.query,
        "results": reranked,
        "count": len(reranked),
        "intent": meta,
    }


@router.post("/answer")
async def answer_search(req: SearchRequest) -> dict:
    """Full RAG pipeline: input refine → embed → search → output refine.

    Returns a conversational answer generated by the LLM, with the
    search results included for reference.
    """
    reranked, meta = await _smart_search_pipeline(req)

    # Score-gate — check if the best result is relevant enough
    # for RAG context.  If not, fall back to general-knowledge mode.
    #
    # Skip the gate when:
    #  • the caller explicitly set score_floor (e.g., --no-floor → 0.0), OR
    #  • the request targets a specific source (source_name, api_name, or
    #    source_type) — the user's intent is clear, so always use RAG.
    best_score = max((r.get("score", 0) for r in reranked), default=0)
    user_bypassed_floor = req.score_floor is not None
    has_source_filter = any([req.source_name, req.source_names, req.api_name, req.source_type, req.document_name])
    skip_gate = user_bypassed_floor or has_source_filter
    general_knowledge = not skip_gate and best_score < settings.search.relevance_threshold

    if general_knowledge:
        logger.info(
            "Score-gate: best_score=%.3f < threshold=%.2f → general-knowledge mode",
            best_score,
            settings.search.relevance_threshold,
        )
    elif skip_gate and best_score < settings.search.relevance_threshold:
        reason = "source filter" if has_source_filter else "user bypassed floor"
        logger.info(
            "Score-gate: best_score=%.3f < threshold=%.2f but %s active → keeping RAG context",
            best_score,
            settings.search.relevance_threshold,
            reason,
        )

    meta["best_score"] = round(best_score, 4)
    meta["relevance_threshold"] = settings.search.relevance_threshold
    meta["general_knowledge"] = general_knowledge

    # Retrieve conversation history for this session (if any).
    conversation_history: list[dict[str, str]] | None = None
    if req.session_id and settings.memory.enabled:
        conversation_history = memory_manager.get_context_window(req.session_id) or None

    # Generate conversational answer from results.
    # Cap the number of results sent to the LLM to avoid exceeding the model's context window.
    # The full result set is still returned in the response for the UI source listing.
    refiner_results = reranked[: response_refiner.refiner_max_results]

    # Expand sql-schema results with related table chunks so the LLM has
    # complete column/FK context for writing JOINs.
    if not general_knowledge:
        refiner_results = _expand_sql_relationships(refiner_results)

    # Enrich code-type results with source snippets + usages.
    # Only runs when code_context.enabled is true and source_roots are configured.
    # Skip when in general-knowledge mode (results are irrelevant).
    if not general_knowledge:
        refiner_results = await enrich_code_context(refiner_results)

    answer = await response_refiner.refine(
        req.query,
        refiner_results,
        include_examples=req.include_examples,
        brief=req.brief,
        conversation_history=conversation_history,
        general_knowledge=general_knowledge,
    )

    # Store this turn in memory for future follow-ups.
    if req.session_id and settings.memory.enabled:
        # Store the user query with metadata about which filters were active
        user_meta: dict = {}
        if req.source_name:
            user_meta["source_name"] = req.source_name
        if req.source_type:
            user_meta["source_type"] = req.source_type
        refined_q = meta.get("refined_query")
        if refined_q:
            user_meta["refined_query"] = refined_q

        memory_manager.add_message(
            req.session_id,
            ChatMessage(role="user", content=req.query, metadata=user_meta),
        )
        memory_manager.add_message(
            req.session_id,
            ChatMessage(role="assistant", content=answer),
        )

    return {
        "query": req.query,
        "answer": answer,
        "results": reranked,
        "count": len(reranked),
        "intent": meta,
    }
