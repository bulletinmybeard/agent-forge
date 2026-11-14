"""Tavily extract request shape: timeout, and query chunks when a focus is set."""

from agentforge.tools.search_providers.base import FetchResult
from agentforge.tools.search_providers.tavily import _EXTRACT_URL, TavilyFetchProvider
from agentforge.tools.web_search import web_fetch


def _capture(monkeypatch):
    calls: dict = {}

    def fake_post(url, body, api_key, timeout=30):
        calls["url"] = url
        calls["body"] = body
        calls["api_key"] = api_key
        calls["timeout"] = timeout
        return {"results": [{"url": body["urls"][0], "raw_content": "# Hello\n\nBody"}]}

    monkeypatch.setattr("agentforge.tools.search_providers.tavily._get_api_key", lambda: "tvly-test")
    monkeypatch.setattr("agentforge.tools.search_providers.tavily._post", fake_post)
    return calls


def test_extract_sets_timeout_and_keeps_the_full_page(monkeypatch):
    calls = _capture(monkeypatch)

    result = TavilyFetchProvider().fetch("https://example.com/a")

    assert calls["url"] == _EXTRACT_URL
    assert calls["body"] == {
        "urls": ["https://example.com/a"],
        "extract_depth": "basic",
        "format": "markdown",
        "timeout": 30.0,
    }
    assert calls["timeout"] == 35.0
    assert result.content.startswith("# Hello")


def test_extract_query_requests_five_chunks(monkeypatch):
    calls = _capture(monkeypatch)

    TavilyFetchProvider().fetch("https://example.com/a", query="  node kev  ")

    assert calls["body"]["query"] == "node kev"
    assert calls["body"]["chunks_per_source"] == 5
    assert calls["body"]["timeout"] == 30.0


def test_web_fetch_forwards_a_focus_and_drops_a_blank_one(monkeypatch):
    seen: list[str] = []

    def fake_fetch(url, query=""):
        seen.append(query)
        return FetchResult(title="", url=url, content="ok", provider="tavily")

    monkeypatch.setattr("agentforge.tools.web_search.fetch_with_fallback", fake_fetch)

    assert "ok" in web_fetch("https://example.com/a", query="kev")
    assert "ok" in web_fetch("https://example.com/a", query="   ")
    assert seen == ["kev", ""]
