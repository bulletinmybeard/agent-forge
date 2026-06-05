"""Intent-aware re-ranking for search results.

Uses a strategy registry so each source_type can plug in its own
re-ranking logic. The dispatcher detects the dominant source_type
in the result set and delegates to the matching strategy.
"""

import logging
import re
from collections import Counter
from typing import Callable

logger = logging.getLogger(__name__)


# ── Type alias for rerank strategies ─────────────────────────────────────────

RerankStrategy = Callable[[list[dict], str, int], tuple[list[dict], dict]]


# ── OpenAPI intent detection ─────────────────────────────────────────────────

# Maps detected intent to the HTTP methods and action_types that match it.
# Order matters: first match wins during detection.
#
# Intent detection is OPT-IN: it only triggers when the query contains an
# explicitly method-specific verb (e.g., "delete", "create", "update",
# "fetch", "download"). General discovery words like "find", "show",
# "what", "which" do NOT trigger any intent, so all HTTP methods are
# treated equally for broad queries.
INTENT_PATTERNS: list[tuple[str, re.Pattern, list[str], list[str]]] = [
    # (intent_name, regex_pattern, preferred_methods, preferred_action_types)
    (
        "delete",
        re.compile(r"\b(delete|remove|destroy|drop|cancel|revoke)\b", re.IGNORECASE),
        ["DELETE"],
        ["delete"],
    ),
    (
        "create",
        re.compile(
            r"\b(create|insert|register|submit|upload|post)\b",
            re.IGNORECASE,
        ),
        ["POST"],
        ["create"],
    ),
    (
        "update",
        re.compile(
            r"\b(update|edit|modify|patch|replace|rename)\b",
            re.IGNORECASE,
        ),
        ["PUT", "PATCH"],
        ["update"],
    ),
    (
        "retrieve",
        re.compile(
            r"\b(fetch|retrieve|download|GET)\b",
            re.IGNORECASE,
        ),
        ["GET"],
        ["retrieve", "list"],
    ),
]

# Boost multiplier for matching action_type
ACTION_MATCH_BOOST = 1.08  # +8% for matching action_type

# Penalty multiplier for non-matching HTTP methods.
# Soft-filter: instead of dropping results outright, demote them so they
# still appear if the query is a multi-step workflow needing several methods.
METHOD_MISMATCH_PENALTY = 0.70  # −30% for wrong HTTP method


def detect_intent(query: str) -> tuple[str | None, list[str], list[str]]:
    """Detect the user's intent from a natural language query."""
    for intent_name, pattern, methods, actions in INTENT_PATTERNS:
        if pattern.search(query):
            logger.debug("Detected intent '%s' in query: %s", intent_name, query)
            return intent_name, methods, actions

    return None, [], []


# ── OpenAPI rerank strategy ──────────────────────────────────────────────────


def rerank_openapi(
    results: list[dict],
    query: str,
    limit: int,
) -> tuple[list[dict], dict]:
    """Re-rank OpenAPI search results based on detected intent.

    Demotes non-matching HTTP methods and boosts matching action_types.
    """
    intent, preferred_methods, preferred_actions = detect_intent(query)

    meta = {
        "intent": intent,
        "preferred_methods": preferred_methods,
        "preferred_actions": preferred_actions,
        "reranked": intent is not None,
    }

    if intent is None:
        return results[:limit], meta

    reranked = []
    demoted = 0

    for result in results:
        payload = result.get("payload", {})
        original_score = result.get("score", 0.0)
        adjusted_score = original_score

        method = payload.get("method", "")
        action_type = payload.get("action_type", "")

        # Soft-filter: demote results whose HTTP method doesn't match
        # the detected intent instead of dropping them.  This keeps
        # workflow queries (create → monitor → download) intact while
        # still ranking the intent-matching methods higher.
        if preferred_methods and method and method not in preferred_methods:
            adjusted_score *= METHOD_MISMATCH_PENALTY
            demoted += 1

        # Action type boost (no penalty — method filter already handles mismatches)
        if action_type:
            if action_type in preferred_actions:
                adjusted_score *= ACTION_MATCH_BOOST

        reranked.append(
            {
                **result,
                "score": round(adjusted_score, 7),
                "original_score": round(original_score, 7),
            }
        )

    if demoted:
        logger.debug(
            "Intent '%s' demoted %d non-%s result(s) (×%.2f)",
            intent,
            demoted,
            "/".join(preferred_methods),
            METHOD_MISMATCH_PENALTY,
        )

    meta["demoted_by_method"] = demoted

    # Sort by adjusted score descending
    reranked.sort(key=lambda r: r["score"], reverse=True)

    return reranked[:limit], meta


# ── SQL Schema rerank strategy ───────────────────────────────────────────────

# Queries that indicate the user wants a database-level overview rather
# than details about a specific table.
_OVERVIEW_PATTERN = re.compile(
    r"\b(how many|count|list all|all tables|overview|summary|structure|schema overview|what tables|which tables|describe database|database info)\b",
    re.IGNORECASE,
)

# Boost applied to database_summary / relationship_map when the query is an
# overview question.  +25% is enough to push them above individual tables.
SUMMARY_BOOST = 1.25


def rerank_sql_schema(
    results: list[dict],
    query: str,
    limit: int,
) -> tuple[list[dict], dict]:
    """Re-rank SQL schema results.

    For overview queries (e.g., "how many tables"), boost database_summary
    and relationship_map chunks so they appear at the top where the LLM
    refiner can see them.
    """
    is_overview = bool(_OVERVIEW_PATTERN.search(query))

    meta = {
        "intent": "overview" if is_overview else None,
        "reranked": is_overview,
    }

    if not is_overview:
        sorted_results = sorted(results, key=lambda r: r.get("score", 0), reverse=True)
        return sorted_results[:limit], meta

    reranked = []
    for result in results:
        payload = result.get("payload", {})
        chunk_type = payload.get("chunk_type", "")
        original_score = result.get("score", 0.0)
        adjusted_score = original_score

        if chunk_type in ("database_summary", "relationship_map"):
            adjusted_score *= SUMMARY_BOOST

        reranked.append(
            {
                **result,
                "score": round(adjusted_score, 7),
                "original_score": round(original_score, 7),
            }
        )

    reranked.sort(key=lambda r: r["score"], reverse=True)

    meta["boosted_summary"] = True
    return reranked[:limit], meta


def rerank_default(
    results: list[dict],
    query: str,
    limit: int,
) -> tuple[list[dict], dict]:
    """Default re-ranking: sort by score, no intent filtering."""
    sorted_results = sorted(results, key=lambda r: r.get("score", 0), reverse=True)
    meta = {
        "intent": None,
        "reranked": False,
    }
    return sorted_results[:limit], meta


RERANK_STRATEGIES: dict[str, RerankStrategy] = {
    "openapi": rerank_openapi,
    "sql-schema": rerank_sql_schema,
}


def _detect_dominant_source_type(results: list[dict]) -> str | None:
    """Detect the most common source_type in the result set."""
    types = [r.get("payload", {}).get("source_type") for r in results]
    types = [t for t in types if t]
    if not types:
        return None
    counter = Counter(types)
    return counter.most_common(1)[0][0]


def rerank_results(
    results: list[dict],
    query: str,
    limit: int,
) -> tuple[list[dict], dict]:
    """Re-rank search results using the strategy matching the dominant source_type."""
    source_type = _detect_dominant_source_type(results)
    strategy = RERANK_STRATEGIES.get(source_type, rerank_default) if source_type else rerank_default

    reranked, meta = strategy(results, query, limit)
    meta["source_type"] = source_type
    return reranked, meta
