"""Turn fetched HTML into article markdown and clip it on a boundary.

Search providers sometimes return a raw document. The rendered path can
return the whole body. Both get the same cleanup before a model sees them:
drop chrome, keep headings, and cut on a heading or blank line so a cap
does not end mid-sentence.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, Tag
from markdownify import markdownify

_PAGE_TOOLS = frozenset({"web_fetch", "web_fetch_rendered", "web_fetch_scrapling"})

# A short <article> is often a card, not the page. Below this, use <body>.
_MIN_MAIN_CHARS = 200
# Caps this small are probes. A boundary search would keep nothing useful.
_MIN_BREAK_BUDGET = 80

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "template")
_CHROME_TAGS = ("nav", "footer", "aside")
_BODY_CHROME_TAGS = ("nav", "footer", "aside", "header")
_CHROME_ROLES = {"navigation", "contentinfo", "banner", "complementary"}
_ZWC = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b")


def looks_like_html(text: str) -> bool:
    """True when *text* is a document rather than markdown that mentions a tag."""
    sample = text.lstrip()[:500].lower()
    if sample.startswith(("<!doctype", "<html", "<head", "<body")):
        return True
    return len(_TAG.findall(text[:4000])) >= 8


def html_to_markdown(html: str) -> str:
    """Article or main content as markdown. Nav, footer, and scripts are dropped.

    The longest of ``article``, ``main``, and ``[role=main]`` wins when it
    has at least ``_MIN_MAIN_CHARS`` of text. Otherwise the body is used
    and site chrome (including ``header``) is removed.
    """
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "lxml")
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    _drop_hidden(soup)

    root = _main_node(soup)
    if root is None:
        _drop_chrome(soup, _BODY_CHROME_TAGS)
        root = soup.body or soup
    else:
        _drop_chrome(root, _CHROME_TAGS)

    text = markdownify(str(root), heading_style="ATX", bullets="-")
    text = _ZWC.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clip_page(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut *text* to *max_chars*, preferring the last heading or blank line.

    The cut stays in the second half of the budget so a heading near the
    start does not throw the page away. With no boundary, the cut is the
    budget itself.
    """
    if max_chars < 1:
        max_chars = 1
    if len(text) <= max_chars:
        return text, False
    window = text[:max_chars]
    if max_chars < _MIN_BREAK_BUDGET:
        return window.rstrip(), True
    floor = max(max_chars // 2, _MIN_BREAK_BUDGET)
    cut = len(window)
    for marker in ("\n#", "\n\n"):
        idx = window.rfind(marker, floor)
        if idx != -1:
            cut = idx
            break
    else:
        nl = window.rfind("\n", floor)
        if nl != -1:
            cut = nl
    return window[:cut].rstrip(), True


def truncation_line(max_chars: int) -> str:
    """One line. Does not invite the model to ask for a larger cap."""
    return f"... (truncated at {max_chars:,} chars)"


def clip_tool_output(tool_name: str, output: str, limit: int) -> str:
    """Apply the agent-loop cap. Page tools clip on a heading; other tools keep the file hint."""
    if limit < 1 or len(output) <= limit:
        return output
    if tool_name in _PAGE_TOOLS:
        clipped, _ = clip_page(output, limit)
        return clipped + "\n\n" + truncation_line(limit)
    trimmed = len(output) - limit
    note = f"\n\n[... truncated {trimmed:,} characters — use grep/tail to narrow your search ...]"
    return output[:limit] + note


def _main_node(soup: BeautifulSoup) -> Tag | None:
    nodes = list(soup.find_all(["article", "main"]))
    nodes.extend(soup.find_all(None, {"role": "main"}))
    best = None
    best_len = 0
    for node in nodes:
        length = len(node.get_text(" ", strip=True))
        if length > best_len:
            best = node
            best_len = length
    if best is None or best_len < _MIN_MAIN_CHARS:
        return None
    return best


def _drop_chrome(root, tags: tuple[str, ...]) -> None:
    for tag in root.find_all(tags):
        tag.decompose()
    for tag in root.find_all(attrs={"role": True}):
        raw = tag.get("role")
        if isinstance(raw, list):
            parts = {str(part).lower() for part in raw}
        else:
            parts = set(str(raw).lower().split())
        if parts & _CHROME_ROLES:
            tag.decompose()


def _drop_hidden(soup: BeautifulSoup) -> None:
    hidden = []
    for tag in soup.find_all(True):
        if tag.has_attr("hidden"):
            hidden.append(tag)
            continue
        aria = str(tag.get("aria-hidden") or "").lower()
        if aria == "true":
            hidden.append(tag)
            continue
        style = str(tag.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            hidden.append(tag)
    for tag in hidden:
        tag.decompose()
