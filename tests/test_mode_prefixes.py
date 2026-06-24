from agentforge.intent_classifier import _ANYWHERE_PREFIXES, _PREFIX_MAP
from agentforge.mode_prefixes import RAG_SEARCH_ALIASES, RAG_SEARCH_MODE
from web.server.mode_routing import ANYWHERE_ALIASES, SEARCH_ALIASES


def test_rag_aliases_shared_across_routing_and_classifier():
    assert RAG_SEARCH_ALIASES == frozenset({"@qdrant", "@docs", "@find"})
    assert _ANYWHERE_PREFIXES == RAG_SEARCH_ALIASES
    assert ANYWHERE_ALIASES == RAG_SEARCH_ALIASES
    assert SEARCH_ALIASES == RAG_SEARCH_ALIASES
    for alias in RAG_SEARCH_ALIASES:
        assert _PREFIX_MAP[alias] == RAG_SEARCH_MODE
