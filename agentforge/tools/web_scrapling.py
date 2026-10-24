"""HTTP page fetch via Scrapling's Fetcher.

``web_fetch`` reads a URL through a search provider. ``web_fetch_rendered``
runs a headless browser. This tool GETs the page itself (curl_cffi, Chrome
TLS impersonation) and returns cleaned markdown. It does not execute
JavaScript and it does not launch a browser.

Requires the ``scrapling[rag]`` extra (Fetcher + markdown conversion).
A missing install returns an error string instead of breaking registration.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from chalkbox.logging.bridge import get_logger

from .registry import tool
from .web_render import _validate_url

if TYPE_CHECKING:
    from .registry import ToolRegistry

# Bound once so the missing-install fallback does not redeclare the name as a
# different type (the import itself declares the class).
_Fetcher: Any | None = None
try:
    from scrapling.fetchers import Fetcher as _FetcherClass

    _Fetcher = _FetcherClass
except ImportError:  # scrapling[rag] not installed
    pass

logger = get_logger(__name__)

_DEFAULT_MAX_CHARS = 8000
_MAX_CHARS_CAP = 64_000
_FETCH_TIMEOUT = 20
_FETCH_RETRIES = 2

_BINARY_TYPES = (
    "image/",
    "audio/",
    "video/",
    "application/pdf",
    "application/octet-stream",
    "application/zip",
    "application/gzip",
)


@tool(
    locality="remote",
    hint=(
        "Fetch one http(s) URL with Scrapling and return cleaned markdown. "
        "The request uses browser TLS impersonation and does not execute JavaScript. "
        "web_fetch goes through the configured search provider; web_fetch_rendered "
        "is the headless browser for SPAs. Use this when you want the page HTML "
        "turned into markdown on the worker. "
        "selector: optional CSS selector; only matching elements are returned. "
        "max_chars: cap on returned text (default 8000, max 64000). "
        "Example: web_fetch_scrapling(url='https://example.com/docs', selector='article')"
    ),
)
def web_fetch_scrapling(url: str, selector: str = "", max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Fetch a URL with Scrapling and return cleaned markdown.

    When to use: a specific http(s) page whose content is in the HTML response,
        including sites that block plain HTTP clients but do not require JavaScript.
    When NOT to use: search queries (web_search), or SPAs whose content is built
        by JavaScript (web_fetch_rendered).

    url: full URL. A missing scheme is treated as https.
    selector: CSS selector. Empty returns the body as markdown.
    max_chars: maximum characters of page text to return.
    """
    if not isinstance(url, str) or not url.strip():
        return "Error: No URL provided."

    url = _normalize_url(url.strip())
    err = _validate_url(url)
    if err:
        return err

    limit = _clamp_max_chars(max_chars)
    selector = "" if selector is None else str(selector).strip()

    if _Fetcher is None:
        return (
            "Error: scrapling is not installed. "
            "Install the rag extra (Fetcher + markdown): pip install 'scrapling[rag]'"
        )

    logger.debug("[web_fetch_scrapling] Fetching: %s", url[:120])
    try:
        page = _Fetcher.get(
            url,
            timeout=_FETCH_TIMEOUT,
            retries=_FETCH_RETRIES,
            follow_redirects="safe",
            impersonate="chrome",
            stealthy_headers=True,
        )
    except Exception as exc:
        logger.debug("[web_fetch_scrapling] fetch failed: %s", exc)
        return f"Fetch error: {exc}"

    return _format_page(url, page, selector, limit)


def register_web_scrapling_tools(registry: ToolRegistry) -> int:
    """Register the Scrapling fetch tool. Always registered; errors at call time."""
    registry.register_category_hint(
        "Web Fetch",
        "Local HTTP fetch via Scrapling. web_fetch_scrapling GETs a URL with browser "
        "TLS impersonation and returns markdown. It does not run JavaScript. "
        "For SPAs use web_fetch_rendered. web_fetch still goes through the search provider.",
    )
    registry.register(web_fetch_scrapling, category="Web Fetch")
    return 1


def _normalize_url(url: str) -> str:
    """Add https only when the caller did not already name a scheme."""
    if "://" in url:
        return url
    return f"https://{url}"


def _clamp_max_chars(max_chars: Any) -> int:
    if isinstance(max_chars, str):
        try:
            max_chars = int(max_chars)
        except ValueError:
            max_chars = _DEFAULT_MAX_CHARS
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        max_chars = _DEFAULT_MAX_CHARS
    return max(1, min(max_chars, _MAX_CHARS_CAP))


def _header(page: Any, name: str) -> str:
    headers = getattr(page, "headers", None)
    if not headers:
        return ""
    try:
        value = headers.get(name) or headers.get(name.title()) or ""
    except Exception:
        return ""
    return str(value)


def _content_type(page: Any) -> str:
    return _header(page, "content-type").split(";", 1)[0].strip().lower()


def _is_binary(content_type: str) -> bool:
    return any(content_type.startswith(prefix) for prefix in _BINARY_TYPES)


def _format_page(url: str, page: Any, selector: str, max_chars: int) -> str:
    status = getattr(page, "status", None)
    source = str(getattr(page, "url", None) or url)
    content_type = _content_type(page)

    lines: list[str] = []
    title = _title(page)
    if title:
        lines.append(f"# {title}")
    if status is not None:
        lines.append(f"HTTP {status}")
    lines.append(f"Source: {source}")
    if selector:
        lines.append(f"Selector: {selector}")
    if content_type:
        lines.append(f"Content-Type: {content_type}")

    if _is_binary(content_type):
        body = getattr(page, "body", b"") or b""
        size = len(body) if isinstance(body, (bytes, bytearray)) else 0
        lines.append(f"\nBody: {size} bytes (not returned)")
        return "\n".join(lines)

    text = _extract_text(page, selector, content_type)
    if not text:
        lines.append("\nNo content returned.")
        return "\n".join(lines)

    clipped, truncated = _clip(text, max_chars)
    lines.append("")
    lines.append(clipped)
    if truncated:
        lines.append(f"\n... (truncated at {max_chars:,} chars)")
    return "\n".join(lines)


def _title(page: Any) -> str:
    css = getattr(page, "css", None)
    if not callable(css):
        return ""
    try:
        found = css("title::text").get()
    except Exception:
        return ""
    return str(found or "").strip()


def _extract_text(page: Any, selector: str, content_type: str) -> str:
    if not selector and ("json" in content_type or _looks_like_json(page)):
        rendered = _json_text(page)
        if rendered:
            return rendered

    markdown = getattr(page, "markdown", None)
    if callable(markdown):
        kwargs: dict[str, Any] = {"css_selector": selector} if selector else {"main_content_only": True}
        try:
            text = str(markdown(**kwargs) or "").strip()
        except Exception:
            logger.debug("[web_fetch_scrapling] markdown() failed", exc_info=True)
            text = ""
        if text:
            return text

    if selector:
        return _selector_text(page, selector)

    getter = getattr(page, "get_all_text", None)
    if callable(getter):
        try:
            return str(getter(separator="\n", strip=True, ignore_tags=("script", "style", "noscript")) or "").strip()
        except Exception:
            logger.debug("[web_fetch_scrapling] get_all_text() failed", exc_info=True)
    return ""


def _looks_like_json(page: Any) -> bool:
    body = getattr(page, "body", None)
    if isinstance(body, bytes):
        sample = body.lstrip()[:1]
        return sample in (b"{", b"[")
    if isinstance(body, str):
        sample = body.lstrip()[:1]
        return sample in ("{", "[")
    return False


def _json_text(page: Any) -> str:
    parser = getattr(page, "json", None)
    if not callable(parser):
        return ""
    try:
        payload = parser()
    except Exception:
        return ""
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    except TypeError:
        return str(payload)


def _selector_text(page: Any, selector: str) -> str:
    css = getattr(page, "css", None)
    if not callable(css):
        return ""
    try:
        nodes = css(selector)
    except Exception:
        logger.debug("[web_fetch_scrapling] css() failed", exc_info=True)
        return ""
    parts: list[str] = []
    try:
        iterator = list(nodes)
    except TypeError:
        iterator = []
    for node in iterator:
        piece = ""
        getter = getattr(node, "get_all_text", None)
        if callable(getter):
            try:
                piece = str(getter(separator="\n", strip=True) or "").strip()
            except Exception:
                piece = ""
        if not piece:
            raw = getattr(node, "get", None)
            if callable(raw):
                try:
                    piece = str(raw() or "").strip()
                except Exception:
                    piece = ""
        if piece:
            parts.append(piece)
    return "\n".join(parts)


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


__all__ = [
    "register_web_scrapling_tools",
    "web_fetch_scrapling",
]
