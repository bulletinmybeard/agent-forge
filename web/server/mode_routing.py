"""Mode prefix detection for chat and REST query paths.

Parses @-aliases at the start of a prompt (or anywhere for RAG aliases) and
returns the cleaned query plus the forced mode name.
"""

from __future__ import annotations

STICKY_MODES = frozenset(("web_search", "logs", "sql", "scheduler", "monitor", "research", "coding"))

CHAT_ALIASES = {"@chat"}
AGENT_ALIASES = {"@agent"}
# RAG over indexed data (@qdrant is canonical). @docs/@find are backward-compatible aliases.
RAG_SEARCH_ALIASES = frozenset({"@qdrant", "@docs", "@find"})
SEARCH_ALIASES = RAG_SEARCH_ALIASES
WEB_SEARCH_ALIASES = {"@search"}
LOGS_ALIASES = {"@logs"}
DISCOVER_ALIASES = {"@discover"}
SQL_ALIASES = {"@sql"}
PIPELINE_ALIASES = {"@pipeline"}
SCHEDULER_ALIASES = {"@scheduler"}
MONITOR_ALIASES = {"@monitor"}
REVIEW_ALIASES = {"@review"}
RESEARCH_ALIASES = {"@research"}
CODING_ALIASES = {"@coding", "@code"}
CONNECTOR_ALIASES = {"@conn", "@connector"}
# RAG aliases that can appear anywhere in the query (not just at the start)
ANYWHERE_ALIASES = RAG_SEARCH_ALIASES

_PREFIX_GROUPS: list[tuple[set[str], str]] = [
    (CHAT_ALIASES, "chat"),
    (SQL_ALIASES, "sql"),
    (AGENT_ALIASES, "agent"),
    (WEB_SEARCH_ALIASES, "web_search"),
    (LOGS_ALIASES, "logs"),
    (SEARCH_ALIASES, "search"),
    (DISCOVER_ALIASES, "discover"),
    (PIPELINE_ALIASES, "pipeline"),
    (REVIEW_ALIASES, "review"),
    (RESEARCH_ALIASES, "research"),
    (SCHEDULER_ALIASES, "scheduler"),
    (MONITOR_ALIASES, "monitor"),
    (CODING_ALIASES, "coding"),
]


def strip_mode_prefix(query: str) -> tuple[str, str | None]:
    """Detect mode aliases in the query and strip them.

    Start-of-query aliases (@agent, @search, @logs, etc.) are checked first.
    Anywhere aliases (@qdrant / @docs / @find) can appear at any position.

    Returns (cleaned_query, forced_mode).
    forced_mode is "chat", "agent", "search", "web_search", "logs", "discover",
    "review", or None.
    """
    stripped = query.lstrip()
    lower = stripped.lower()

    for aliases, mode in _PREFIX_GROUPS:
        for alias in aliases:
            if lower.startswith(alias):
                rest = stripped[len(alias) :].lstrip()
                return rest, mode

    for alias in sorted(ANYWHERE_ALIASES, key=len, reverse=True):
        if alias in lower:
            idx = lower.index(alias)
            rest = (stripped[:idx] + stripped[idx + len(alias) :]).strip()
            return rest, "search"

    return query, None
