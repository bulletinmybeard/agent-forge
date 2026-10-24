"""web_fetch_scrapling — URL checks, formatting, and Fetcher call shape."""

from __future__ import annotations

from pathlib import Path

import yaml

from agentforge.tools import web_scrapling
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.web_scrapling import register_web_scrapling_tools, web_fetch_scrapling

ROOT = Path(__file__).resolve().parents[1]


class _Title:
    def get(self):
        return "Example"


class _Page:
    def __init__(self, **kwargs):
        self.status = kwargs.get("status", 200)
        self.headers = kwargs.get("headers", {"content-type": "text/html; charset=utf-8"})
        self.url = kwargs.get("url", "https://example.com/final")
        self.body = kwargs.get("body", b"<html></html>")
        self.markdown_kwargs = None
        self._markdown = kwargs.get("markdown", "# Hello")
        self._text = kwargs.get("text", "plain text")
        self._json = kwargs.get("json_payload", None)

    def css(self, selector):
        assert selector == "title::text"
        return _Title()

    def markdown(self, **kwargs):
        self.markdown_kwargs = kwargs
        return self._markdown

    def get_all_text(self, **kwargs):
        return self._text

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


def _install_fetcher(monkeypatch, page: _Page | Exception):
    calls: list[tuple[str, dict]] = []

    class _FakeFetcher:
        @staticmethod
        def get(url, **kwargs):
            calls.append((url, kwargs))
            if isinstance(page, Exception):
                raise page
            return page

    monkeypatch.setattr(web_scrapling, "_Fetcher", _FakeFetcher)
    monkeypatch.setattr(web_scrapling, "_validate_url", lambda url: None)
    return calls


def test_empty_url():
    assert web_fetch_scrapling("  ") == "Error: No URL provided."


def test_rejects_non_http_scheme():
    result = web_fetch_scrapling("file:///etc/passwd")
    assert result.startswith("Error: unsupported URL scheme")


def test_rejects_loopback():
    result = web_fetch_scrapling("http://127.0.0.1/secret")
    assert "SSRF guard" in result


def test_rejects_link_local_metadata():
    result = web_fetch_scrapling("http://169.254.169.254/latest/meta-data")
    assert "link-local" in result


def test_missing_install(monkeypatch):
    monkeypatch.setattr(web_scrapling, "_Fetcher", None)
    monkeypatch.setattr(web_scrapling, "_validate_url", lambda url: None)
    result = web_fetch_scrapling("https://example.com")
    assert "scrapling is not installed" in result


def test_fetch_kwargs_and_markdown(monkeypatch):
    page = _Page()
    calls = _install_fetcher(monkeypatch, page)

    result = web_fetch_scrapling("example.com/docs", selector="article", max_chars=50)

    assert calls[0][0] == "https://example.com/docs"
    kwargs = calls[0][1]
    assert kwargs["follow_redirects"] == "safe"
    assert kwargs["impersonate"] == "chrome"
    assert kwargs["stealthy_headers"] is True
    assert kwargs["timeout"] == web_scrapling._FETCH_TIMEOUT
    assert kwargs["retries"] == web_scrapling._FETCH_RETRIES
    assert page.markdown_kwargs == {"css_selector": "article"}
    assert result.startswith("# Example\nHTTP 200\nSource: https://example.com/final")
    assert "Selector: article" in result
    assert "# Hello" in result


def test_body_markdown_when_no_selector(monkeypatch):
    page = _Page(markdown="Body copy")
    _install_fetcher(monkeypatch, page)

    result = web_fetch_scrapling("https://example.com")

    assert page.markdown_kwargs == {"main_content_only": True}
    assert "Body copy" in result
    assert "Selector:" not in result


def test_truncates(monkeypatch):
    page = _Page(markdown="abcdefghij")
    _install_fetcher(monkeypatch, page)

    result = web_fetch_scrapling("https://example.com", max_chars="4")

    assert "abcd" in result
    assert "truncated at 4 chars" in result


def test_json_body_skips_markdown(monkeypatch):
    page = _Page(
        headers={"content-type": "application/json"},
        body=b'{"ok": true}',
        json_payload={"ok": True},
        markdown="should not be used",
    )
    _install_fetcher(monkeypatch, page)

    result = web_fetch_scrapling("https://example.com/api")

    assert '"ok": true' in result
    assert page.markdown_kwargs is None


def test_binary_is_not_inlined(monkeypatch):
    page = _Page(headers={"content-type": "image/png"}, body=b"\x89PNG", markdown="nope")
    _install_fetcher(monkeypatch, page)

    result = web_fetch_scrapling("https://example.com/a.png")

    assert "not returned" in result
    assert "nope" not in result
    assert page.markdown_kwargs is None


def test_fetch_error(monkeypatch):
    _install_fetcher(monkeypatch, RuntimeError("connection refused"))

    result = web_fetch_scrapling("https://example.com")

    assert result == "Fetch error: connection refused"


def test_falls_back_to_text_when_markdown_fails(monkeypatch):
    page = _Page(markdown="")
    # replaces the method; get_all_text still exists
    setattr(page, "markdown", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no markdownify")))
    _install_fetcher(monkeypatch, page)

    result = web_fetch_scrapling("https://example.com")

    assert "plain text" in result


def test_register_and_remote_route():
    registry = ToolRegistry()
    assert register_web_scrapling_tools(registry) == 1
    assert "web_fetch_scrapling" in registry.list_tools()

    with open(ROOT / "tool_routing.yaml") as fh:
        cfg = yaml.safe_load(fh)
    remote = []
    for rule in cfg.get("rules") or []:
        if rule.get("role") == "remote":
            remote.extend(rule.get("tools") or [])
    assert "web_fetch_scrapling" in remote
