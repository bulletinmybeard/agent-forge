"""Shared @-prefix constants for RAG search mode routing.

Used by ``web.server.mode_routing`` (WS/REST strip) and
``agentforge.intent_classifier`` (fallback classification) so both agree on
which aliases map to the ``search`` mode and which can appear anywhere in a
prompt.
"""

from __future__ import annotations

# Canonical: @qdrant. @docs and @find are backward-compatible aliases.
RAG_SEARCH_ALIASES = frozenset({"@qdrant", "@docs", "@find"})
RAG_SEARCH_MODE = "search"
