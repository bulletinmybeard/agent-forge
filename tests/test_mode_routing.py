from web.server.mode_routing import strip_mode_prefix


def test_strip_start_of_query_aliases():
    assert strip_mode_prefix("@agent list files") == ("list files", "agent")
    assert strip_mode_prefix("@search latest news") == ("latest news", "web_search")
    assert strip_mode_prefix("@coding fix imports") == ("fix imports", "coding")
    assert strip_mode_prefix("@code refactor") == ("refactor", "coding")


def test_strip_rag_aliases_at_start():
    for prefix in ("@qdrant", "@docs", "@find"):
        cleaned, mode = strip_mode_prefix(f"{prefix} how does auth work?")
        assert mode == "search"
        assert cleaned == "how does auth work?"


def test_strip_rag_aliases_anywhere_in_query():
    cases = [
        ("find me @qdrant session compaction docs", "find me  session compaction docs"),
        ("before @docs after", "before  after"),
        ("use @find for this", "use  for this"),
        ("mixed @QDRANT case", "mixed  case"),
    ]
    for query, expected in cases:
        cleaned, mode = strip_mode_prefix(query)
        assert mode == "search", query
        assert cleaned == expected, query


def test_no_prefix_returns_original():
    assert strip_mode_prefix("plain question") == ("plain question", None)
    assert strip_mode_prefix("  spaced  ") == ("  spaced  ", None)
