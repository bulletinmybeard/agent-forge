"""Article markdown extraction and heading-aware page clips."""

from agentforge.tools.page_text import clip_page, clip_tool_output, html_to_markdown
from agentforge.tools.search_providers.base import FetchResult
from agentforge.tools.web_render import _format_sidecar_response
from agentforge.tools.web_search import _format_fetch_result
from sidecar.app import _normalize_content, _normalize_layout_text

_ARTICLE = "October CVE-2026-1 is listed in the catalog. " * 12


def _page(article: str) -> str:
    return (
        "<html><body>"
        "<nav>Home About</nav>"
        "<header>Site banner</header>"
        f"<article><h1>KEV</h1><p>{article}</p></article>"
        "<footer>copyright line</footer>"
        "</body></html>"
    )


def test_html_to_markdown_keeps_article_and_drops_chrome():
    text = html_to_markdown(_page(_ARTICLE))

    assert "# KEV" in text
    assert "CVE-2026-1" in text
    assert "Home About" not in text
    assert "Site banner" not in text
    assert "copyright line" not in text


def test_short_article_falls_back_to_body_without_chrome():
    text = html_to_markdown(_page("short note"))

    assert "short note" in text
    assert "Home About" not in text
    assert "copyright line" not in text


def test_clip_page_stops_on_heading():
    text = "# Title\n\n" + ("b" * 200) + "\n\n## Later\n\n" + ("c" * 200)
    clipped, truncated = clip_page(text, 280)

    assert truncated
    assert "bbbb" in clipped
    assert "## Later" not in clipped
    assert "cccc" not in clipped


def test_clip_page_hard_cuts_a_short_budget():
    clipped, truncated = clip_page("abcdefghij", 4)

    assert truncated
    assert clipped == "abcd"


def test_clip_tool_output_page_vs_file_hint():
    page = clip_tool_output("web_fetch", "x" * 50, 10)
    other = clip_tool_output("shell", "x" * 50, 10)

    assert "truncated at 10 chars" in page
    assert "grep/tail" not in page
    assert "grep/tail" in other


def test_fetch_formatter_converts_html_and_does_not_ask_for_more():
    result = FetchResult(title="KEV", url="https://example.com/kev", content=_page(_ARTICLE))
    rendered = _format_fetch_result("https://example.com/kev", result, 400)

    assert "# KEV" in rendered
    assert "Home About" not in rendered
    assert "larger max_chars" not in rendered


def test_sidecar_formatter_uses_article_html():
    html = "<article><h1>KEV</h1><p>" + ("alpha " * 40) + "</p></article>"
    rendered = _format_sidecar_response(
        "https://example.com/kev",
        {"html": html, "content": "nav footer flattened"},
        80,
    )

    assert "KEV" in rendered
    assert "flattened" not in rendered
    assert "larger max_chars" not in rendered


def test_layout_text_keeps_newlines_monitor_text_does_not():
    raw = "Hello\n\nWorld   wide"
    assert "\n" in _normalize_layout_text(raw)
    assert "\n" not in _normalize_content(raw)
    assert _normalize_content(raw) == "Hello World wide"
