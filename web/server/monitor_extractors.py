"""monitor_extractors — content extraction and normalization for @monitor.

Three extraction modes:
  - ``text``      : Static fetch via Ollama Cloud → clean text (fast, no JS)
  - ``markdown``  : Static fetch → markdown output (good for docs)
  - ``rendered``  : Playwright headless browser → visible text (SPAs, React, Vue)

Each mode supports optional CSS *or XPath* selector targeting — extract only
a subtree of the page.  CSS example: ``.pricing-table``, XPath example:
``//dt[normalize-space()='Price']/following-sibling::dd[1]``.

Selector type is auto-detected: anything starting with ``/`` or ``(`` is
treated as XPath; everything else is CSS.

The normalization pipeline strips dynamic attributes before hashing so that
React class-name churn, data-* tracking attrs, cache busters, and timestamps
don't cause false-positive change detections.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selector type detection
# ---------------------------------------------------------------------------


def is_xpath(selector: str) -> bool:
    """Return True if *selector* looks like an XPath expression.

    Heuristic: XPath selectors start with ``/``, ``//``, or ``(`` (grouped).
    CSS selectors never start with these characters.
    """
    s = selector.lstrip()
    return s.startswith("/") or s.startswith("(")


# ---------------------------------------------------------------------------
# Stealth browser helper — anti-bot evasion for Playwright
# ---------------------------------------------------------------------------

# Cookie consent selectors — common "Accept All" buttons on Dutch sites.
# Tried in order; first match wins.
_COOKIE_ACCEPT_SELECTORS = [
    # Generic CMP / consent managers
    "button#onetrust-accept-btn-handler",  # OneTrust
    "button[data-testid='uc-accept-all-button']",  # Usercentrics
    "button.accept-all",
    "button[id*='accept']",
    "button[class*='accept']",
    "button:has-text('Accepteer')",  # Dutch: "Accept"
    "button:has-text('Alles accepteren')",  # Dutch: "Accept all"
    "button:has-text('Alle cookies accepteren')",  # Dutch variant
    "button:has-text('Accept all')",
    "button:has-text('Accept')",
    "button:has-text('Akkoord')",  # Dutch: "Agree"
    "button:has-text('Ik ga akkoord')",  # Dutch: "I agree"
]


def _launch_stealth_page(playwright, url: str, *, viewport: dict | None = None, timeout: int | None = None):
    """Launch a stealth headless Chromium page using ``playwright-stealth``.

    Uses the ``playwright-stealth`` library for comprehensive fingerprint
    masking (WebGL, plugins, permissions, user-agent, Chrome runtime, etc.)
    plus anti-automation Chromium flags and a realistic Dutch locale context.

    After navigation, attempts to dismiss cookie consent banners that can
    obscure the real page content (common on Dutch retail / real-estate sites).

    ``timeout`` is the page-load timeout in ms; when None it falls back to
    ``monitor.page_timeout_ms`` in config.yaml (default 30000).

    Returns (browser, page) — caller must close the browser when done.
    Raises on failure so the caller can handle it.
    """
    if timeout is None:
        # Lazy import — monitor_service imports this module (avoid a cycle).
        from .monitor_service import _monitor_config

        timeout = int(_monitor_config().get("page_timeout_ms", 30_000))

    try:
        from playwright_stealth import Stealth

        stealth = Stealth(
            navigator_languages_override=("nl-NL", "nl"),
            navigator_platform_override="MacIntel",
            navigator_vendor_override="Google Inc.",
        )
    except ImportError:
        stealth = None
        logger.debug("playwright-stealth not installed — falling back to basic launch")

    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-default-apps",
            "--disable-hang-monitor",
        ],
    )

    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport=viewport or {"width": 1440, "height": 900},
        locale="nl-NL",
        timezone_id="Europe/Amsterdam",
        java_script_enabled=True,
        color_scheme="light",
    )

    # Apply playwright-stealth fingerprint evasions to the context
    if stealth is not None:
        stealth.apply_stealth_sync(context)

    page = context.new_page()

    # Navigate — domcontentloaded is safer than networkidle for analytics-heavy sites
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)

    # Some bot-challenge pages auto-resolve after running JS for a few seconds.
    # Wait briefly, then check if we're on a challenge page.
    page.wait_for_timeout(3000)

    if _is_bot_wall(page):
        logger.info("Bot challenge detected on %s — waiting for auto-resolve…", url)
        # Many JS challenges resolve within 5-10s of loading
        page.wait_for_timeout(8000)

        # If still blocked, try a full page reload (some challenges set a cookie
        # on first load and let you through on reload)
        if _is_bot_wall(page):
            logger.info("Still blocked after wait — reloading %s", url)
            page.reload(wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(5000)

    # Attempt to dismiss cookie consent overlays
    _dismiss_cookie_banner(page)

    # Final wait for SPA hydration / lazy-loaded elements
    page.wait_for_timeout(2000)

    return browser, page


def _dismiss_cookie_banner(page) -> None:
    """Try to click a cookie consent 'Accept' button if one is visible."""
    try:
        for selector in _COOKIE_ACCEPT_SELECTORS:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=800):
                    btn.click(timeout=2000)
                    logger.debug("Dismissed cookie banner via: %s", selector)
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue
    except Exception:
        pass  # Cookie dismissal is best-effort


# Phrases that indicate a CAPTCHA / bot-verification wall instead of real content.
_BOT_WALL_PHRASES = [
    "je bent bijna op de pagina",  # Funda.nl verification page
    "even geduld",  # "Please wait" (Dutch)
    "controleren of u een mens bent",  # "Checking if you're human"
    "checking if you are human",
    "checking your browser",
    "please verify you are a human",
    "just a moment",
    "attention required",
    "enable javascript and cookies",
    "ray id",  # Cloudflare challenge fingerprint
    "challenge-platform",  # Cloudflare challenge
    "captcha",
]


def _is_bot_wall(page) -> bool:
    """Detect if the current page is a CAPTCHA / bot-verification wall.

    Checks visible text for known verification-page phrases.
    Returns True if the page appears to be a challenge, not real content.
    """
    try:
        body_text = page.inner_text("body").lower()
        for phrase in _BOT_WALL_PHRASES:
            if phrase in body_text:
                logger.info("Bot wall detected — page contains phrase: %r", phrase)
                return True
    except Exception:
        pass
    return False


def _is_bot_wall_html(html: str) -> bool:
    """Check raw HTML string for bot-wall indicators (no Playwright needed)."""
    lower = html.lower()
    for phrase in _BOT_WALL_PHRASES:
        if phrase in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Stealth HTTP fetch — curl_cffi with browser TLS impersonation
# ---------------------------------------------------------------------------


def _stealth_http_fetch(url: str) -> str | None:
    """Fetch a URL using ``curl_cffi`` which impersonates Chrome's TLS fingerprint.

    Many bot-detection systems (DataDome, Akamai, Cloudflare) fingerprint the
    TLS ClientHello and HTTP/2 settings.  Playwright's Chromium uses a
    recognisable headless fingerprint that gets blocked.  ``curl_cffi`` links
    against a patched curl that reproduces a real Chrome TLS handshake.

    Returns the page HTML as a string, or None on failure.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.debug("curl_cffi not installed — stealth HTTP fetch unavailable")
        return None

    try:
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            headers={
                "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Stealth HTTP fetch returned status %d for %s", resp.status_code, url)
            return None

        html = resp.text
        if _is_bot_wall_html(html):
            logger.warning("Stealth HTTP fetch hit bot wall on %s", url)
            return None

        logger.info("Stealth HTTP fetch succeeded for %s (%d bytes)", url, len(html))
        return html

    except Exception as exc:
        logger.warning("Stealth HTTP fetch failed for %s: %s", url, exc)
        return None


def _extract_text_from_html(html: str, css_selector: str | None = None) -> str:
    """Extract visible text from raw HTML, optionally targeting a CSS or XPath selector."""
    # If a selector is provided, try the unified _extract_by_selector helper
    if css_selector:
        selected = _extract_by_selector(html, css_selector)
        if selected:
            return selected

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Fallback: strip tags with regex (rough but functional)
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()

    return soup.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# Normalization — strip dynamic noise before hashing
# ---------------------------------------------------------------------------

# Patterns that commonly change between page loads without real content changes
_DYNAMIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    # CSS-module / styled-components / Tailwind JIT class names  (e.g., css-1a2b3c, sc-dkzDqf)
    (re.compile(r'\bclass="[^"]*"'), ""),
    (re.compile(r"\bclass='[^']*'"), ""),
    # data-* attributes (tracking, React internals, state)
    (re.compile(r'\bdata-[\w-]+="[^"]*"'), ""),
    (re.compile(r"\bdata-[\w-]+'[^']*'"), ""),
    # style attributes with dynamic values
    (re.compile(r'\bstyle="[^"]*"'), ""),
    # id attributes that look auto-generated (contain hashes or long numbers)
    (re.compile(r'\bid="[a-zA-Z]*[0-9a-f]{6,}"'), ""),
    # Cache-buster query strings (?v=abc123, ?h=deadbeef)
    (re.compile(r"\?(?:v|h|hash|_)=[a-zA-Z0-9._-]+"), ""),
    # ISO timestamps and unix timestamps
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "[TIMESTAMP]"),
    (re.compile(r"\b1[6-9]\d{8,9}\b"), "[UNIX_TS]"),
    # UUIDs
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "[UUID]"),
    # Nonce values (CSP nonces)
    (re.compile(r'\bnonce="[^"]*"'), ""),
]

# Whitespace normalizer — collapse runs of whitespace into single space
_WS_COLLAPSE = re.compile(r"\s+")


def normalize_content(raw: str) -> str:
    """Strip dynamic attributes and noise, collapse whitespace.

    Returns a cleaned string suitable for hashing and diffing.
    """
    text = raw
    for pattern, replacement in _DYNAMIC_PATTERNS:
        text = pattern.sub(replacement, text)
    # Collapse whitespace
    text = _WS_COLLAPSE.sub(" ", text).strip()
    return text


def hash_content(content: str) -> str:
    """SHA-256 hash of normalized content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Selector extraction (BeautifulSoup for CSS, lxml for XPath)
# ---------------------------------------------------------------------------


def _extract_by_xpath(html: str, xpath: str) -> str | None:
    """Extract text from HTML elements matching an XPath expression.

    Uses ``lxml.html`` which provides full XPath 1.0 support.
    Returns None if no matches found or lxml isn't available.
    """
    try:
        from lxml import html as lxml_html
    except ImportError:
        logger.warning("lxml not installed — XPath selector ignored")
        return None

    try:
        tree = lxml_html.fromstring(html)
    except Exception as exc:
        logger.debug("lxml HTML parse failed: %s", exc)
        return None

    # Best-effort removal of script/style noise before XPath evaluation
    try:
        from lxml.html.clean import Cleaner  # type: ignore[import-untyped]

        cleaner = Cleaner(scripts=True, style=True, remove_tags=["noscript", "svg"])
        tree = cleaner.clean_html(tree)
    except ImportError:
        try:
            from lxml_html_clean import Cleaner  # lxml >= 5.2 moved Cleaner here

            cleaner = Cleaner(scripts=True, style=True, remove_tags=["noscript", "svg"])
            tree = cleaner.clean_html(tree)
        except ImportError:
            pass  # No cleaner available — proceed with raw tree
    except Exception:
        pass  # Cleaning failed — proceed with raw tree

    try:
        nodes = tree.xpath(xpath)
    except Exception as exc:
        logger.warning("XPath evaluation failed for '%s': %s", xpath, exc)
        return None

    if not nodes:
        return None

    parts = []
    for node in nodes:
        if isinstance(node, str):
            text = node.strip()
        elif hasattr(node, "text_content"):
            text = node.text_content().strip()
        else:
            text = str(node).strip()
        if text:
            parts.append(text)

    return "\n\n".join(parts) if parts else None


def _extract_by_selector(html: str, selector: str) -> str | None:
    """Extract text from HTML elements matching a CSS or XPath selector.

    Auto-detects selector type via ``is_xpath()``.
    Returns None if no matches found or required library isn't available.
    """
    if is_xpath(selector):
        return _extract_by_xpath(html, selector)

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 not installed — CSS selector ignored")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Remove script, style, noscript, svg — no content value
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()

    elements = soup.select(selector)
    if not elements:
        return None

    # Extract visible text from each matched element
    parts = []
    for el in elements:
        text = el.get_text(separator="\n", strip=True)
        if text:
            parts.append(text)

    return "\n\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def extract_text(url: str, css_selector: str | None = None) -> dict[str, Any]:
    """Fetch page via Ollama Cloud ``web_fetch`` and return clean text.

    Fast, no JS rendering.  Good for static sites, blogs, documentation.
    """
    from agentforge.tools.web_search import is_web_search_available, web_fetch

    if not is_web_search_available():
        return {"error": "Web fetch unavailable — no Ollama Cloud API key configured"}

    raw = web_fetch(url)

    if raw.startswith("Error") or raw.startswith("Fetch error") or raw.startswith("No content"):
        return {"error": raw}

    # If CSS selector is specified, try to extract via selector
    # web_fetch returns markdown-ish text, not raw HTML, so selector
    # only works if we also have the HTML.  For text mode with selector,
    # we do a separate raw HTML fetch.
    content = raw
    if css_selector:
        html = _fetch_raw_html(url)
        if html:
            selected = _extract_by_selector(html, css_selector)
            if selected:
                content = selected
            else:
                logger.warning("Selector '%s' matched nothing on %s — using full page", css_selector, url)

    normalized = normalize_content(content)
    return {
        "content": normalized,
        "content_hash": hash_content(normalized),
        "word_count": len(normalized.split()),
        "extraction_mode": "text",
        "url": url,
    }


def extract_markdown(url: str, css_selector: str | None = None) -> dict[str, Any]:
    """Fetch page as markdown via Ollama Cloud.

    Same as text mode but preserves markdown structure (headings, lists, links).
    """
    from agentforge.tools.web_search import is_web_search_available, web_fetch

    if not is_web_search_available():
        return {"error": "Web fetch unavailable — no Ollama Cloud API key configured"}

    raw = web_fetch(url)

    if raw.startswith("Error") or raw.startswith("Fetch error") or raw.startswith("No content"):
        return {"error": raw}

    content = raw
    if css_selector:
        html = _fetch_raw_html(url)
        if html:
            selected = _extract_by_selector(html, css_selector)
            if selected:
                content = selected

    normalized = normalize_content(content)
    return {
        "content": normalized,
        "content_hash": hash_content(normalized),
        "word_count": len(normalized.split()),
        "extraction_mode": "markdown",
        "url": url,
    }


def extract_rendered(
    url: str,
    css_selector: str | None = None,
    original_prompt: str | None = None,
) -> dict[str, Any]:
    """Fetch page via Playwright (headless Chromium) — handles SPAs.

    Waits for the page to fully render (React hydration, API calls, etc.)
    before extracting visible text content.

    If a CSS selector is provided but matches nothing, automatically falls
    back to vision extraction (screenshot + LLM) when ``original_prompt``
    is available, before falling back to full-page text.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "error": (
                "Playwright not installed. Install with:\n  pip install playwright && playwright install chromium"
            )
        }

    selector_missed = False
    bot_blocked = False
    content = ""
    try:
        with sync_playwright() as p:
            browser, page = _launch_stealth_page(p, url)

            # Check if we're stuck on a bot-verification page
            if _is_bot_wall(page):
                logger.warning("Bot wall persists on %s after stealth launch", url)
                bot_blocked = True
                browser.close()
            else:
                if css_selector:
                    # Extract only the targeted elements (CSS or XPath)
                    pw_sel = f"xpath={css_selector}" if is_xpath(css_selector) else css_selector
                    elements = page.query_selector_all(pw_sel)
                    if elements:
                        parts = [el.inner_text() for el in elements if el.inner_text().strip()]
                        content = "\n\n".join(parts)
                    else:
                        logger.warning("Selector '%s' matched nothing on %s", css_selector, url)
                        selector_missed = True
                        content = page.inner_text("body")
                else:
                    content = page.inner_text("body")

                browser.close()

    except Exception as exc:
        return {"error": f"Playwright error: {exc}"}

    if bot_blocked:
        # Playwright is blocked — try stealth HTTP fetch (curl_cffi) which
        # impersonates Chrome's TLS fingerprint and bypasses WAF detection.
        logger.info("Attempting stealth HTTP fallback for %s", url)
        html = _stealth_http_fetch(url)
        if html:
            content = _extract_text_from_html(html, css_selector)
            if content and content.strip():
                normalized = normalize_content(content)
                return {
                    "content": normalized,
                    "content_hash": hash_content(normalized),
                    "word_count": len(normalized.split()),
                    "extraction_mode": "rendered_stealth_http",
                    "url": url,
                }
        return {"error": f"Bot verification wall detected on {url} — both Playwright and stealth HTTP blocked"}

    # If the CSS selector matched nothing and we have the original prompt,
    # try vision extraction (screenshot → LLM) before falling back to full-page text.
    if selector_missed and original_prompt:
        logger.info("Attempting vision fallback for %s (CSS selector missed)", url)
        vision_result = vision_fallback(url, original_prompt=original_prompt)
        if vision_result:
            return vision_result
        logger.info("Vision fallback also failed — using full-page text for %s", url)

    if not content or not content.strip():
        return {"error": f"No visible content on {url} after rendering"}

    normalized = normalize_content(content)
    return {
        "content": normalized,
        "content_hash": hash_content(normalized),
        "word_count": len(normalized.split()),
        "extraction_mode": "rendered",
        "url": url,
    }


# ---------------------------------------------------------------------------
# Audit screenshot — capture page state for every monitor check
# ---------------------------------------------------------------------------


def capture_check_screenshot(url: str, job_id: str, check_id: int, upload_dir: str) -> str | None:
    """Take a full-page screenshot and save it to the uploads directory.

    Always captures the page as-is — even if it's a bot wall / CAPTCHA page.
    This gives a visual audit trail for every monitor check.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug("Playwright not installed — cannot capture audit screenshot")
        return None

    # Ensure the screenshot subdirectory exists
    screenshot_dir = Path(upload_dir) / "monitor" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Short job_id prefix (first 8 chars) + check_id for uniqueness
    filename = f"{job_id[:8]}_{check_id}.png"
    filepath = screenshot_dir / filename
    rel_path = f"monitor/screenshots/{filename}"

    try:
        with sync_playwright() as p:
            browser, page = _launch_stealth_page(p, url)
            page.screenshot(path=str(filepath), full_page=False, type="png")
            browser.close()

        logger.info("Audit screenshot saved: %s (%d bytes)", rel_path, filepath.stat().st_size)
        return rel_path

    except Exception as exc:
        logger.warning("Audit screenshot failed for %s: %s", url, exc)
        return None


def save_screenshot_b64(
    screenshot_b64: str,
    job_id: str,
    check_id: int,
    upload_dir: str,
) -> str | None:
    """Save a base64-encoded PNG screenshot to the uploads directory.

    Used when the sidecar returns a screenshot — avoids launching a
    separate Playwright session just for the screenshot.
    """
    try:
        screenshot_dir = Path(upload_dir) / "monitor" / "screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{job_id[:8]}_{check_id}.png"
        filepath = screenshot_dir / filename
        rel_path = f"monitor/screenshots/{filename}"

        png_bytes = base64.b64decode(screenshot_b64)
        filepath.write_bytes(png_bytes)

        logger.info("Sidecar screenshot saved: %s (%d bytes)", rel_path, len(png_bytes))
        return rel_path

    except Exception as exc:
        logger.warning("Failed to save sidecar screenshot: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Vision extraction — screenshot → LLM vision model
# ---------------------------------------------------------------------------


def _take_screenshot(url: str) -> bytes | None:
    """Render a page with Playwright and return a PNG screenshot as bytes."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed — cannot take screenshot")
        return None

    try:
        with sync_playwright() as p:
            browser, page = _launch_stealth_page(p, url)

            # Don't screenshot a CAPTCHA page — it'll confuse the vision LLM
            if _is_bot_wall(page):
                logger.warning("Bot wall detected during screenshot of %s — aborting", url)
                browser.close()
                return None

            screenshot = page.screenshot(full_page=False, type="png")
            browser.close()
            return screenshot

    except Exception as exc:
        logger.warning("Screenshot failed for %s: %s", url, exc)
        return None


def _vision_extract(screenshot: bytes, prompt: str, url: str) -> str | None:
    """Send a screenshot to the vision-capable LLM and extract structured content.

    Uses the cloud-heavy profile (mistral-large) which supports vision.
    Returns the LLM's text response, or None on failure.
    """
    try:
        from app.config import settings as af_settings

        from .ws_endpoint import _ollama_client_for_profile

        # Use cloud-heavy profile for vision (mistral-large supports images)
        answer_role = af_settings.ollama.get_role("answer_generation")
        model = answer_role.profile.model
        client = _ollama_client_for_profile(answer_role.profile)

        # Ollama expects images as base64 strings in the images field
        img_b64 = base64.b64encode(screenshot).decode("ascii")

        system_prompt = (
            "You are a precise content extraction assistant. You receive a screenshot "
            "of a web page and a description of what the user wants to monitor. "
            "Extract ONLY the specific content described — a price, a title, a status, "
            "an offer, or whatever the user specified. Return the extracted value as "
            "plain text, nothing else. No explanations, no markdown, no labels. "
            "If you see a price, include the currency symbol. "
            "If the content is not visible in the screenshot, respond with: [NOT FOUND]"
        )

        user_msg = (
            f"The user is monitoring this page: {url}\n"
            f"Their request: {prompt}\n\n"
            f"Look at the screenshot and extract the specific content they want to track."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg, "images": [img_b64]},
        ]

        response = client.chat(
            model=model,
            messages=messages,
            options={"temperature": 0.1, "num_predict": 512},
        )

        text = response["message"]["content"].strip()
        if text and text != "[NOT FOUND]":
            return text

        logger.warning("Vision extraction returned [NOT FOUND] for %s", url)
        return None

    except Exception as exc:
        logger.warning("Vision extraction failed for %s: %s", url, exc)
        return None


def extract_vision(
    url: str,
    css_selector: str | None = None,
    original_prompt: str | None = None,
) -> dict[str, Any]:
    """Extract content via screenshot + vision model.

    Takes a Playwright screenshot, sends it to the vision-capable LLM with the
    user's original monitoring prompt for context.  The model extracts the
    specific value (price, title, status, etc.) as plain text.

    This is the most reliable extraction mode for dynamic sites — it sees the
    page exactly as a human would, regardless of DOM structure.
    """
    prompt = original_prompt or "Extract the main content or key value being monitored on this page"

    screenshot = _take_screenshot(url)
    if not screenshot:
        # Playwright blocked (bot wall) — try stealth HTTP fetch + text extraction
        logger.info("Screenshot failed — attempting stealth HTTP fallback for %s", url)
        html = _stealth_http_fetch(url)
        if html:
            content = _extract_text_from_html(html, css_selector)
            if content and content.strip():
                normalized = normalize_content(content)
                return {
                    "content": normalized,
                    "content_hash": hash_content(normalized),
                    "word_count": len(normalized.split()),
                    "extraction_mode": "vision_stealth_http",
                    "url": url,
                }
        return {"error": f"Failed to take screenshot of {url} and stealth HTTP also failed"}

    extracted = _vision_extract(screenshot, prompt, url)
    if not extracted:
        return {"error": f"Vision model could not extract content from {url}"}

    normalized = normalize_content(extracted)
    return {
        "content": normalized,
        "content_hash": hash_content(normalized),
        "word_count": len(normalized.split()),
        "extraction_mode": "vision",
        "url": url,
    }


def vision_fallback(
    url: str,
    original_prompt: str | None = None,
) -> dict[str, Any] | None:
    """Attempt vision-based extraction as a fallback.

    Called when the primary extraction mode (rendered + CSS selector) fails.
    Returns the extraction result dict, or None if vision also fails.
    """
    prompt = original_prompt or "Extract the main content or value being monitored on this page"

    screenshot = _take_screenshot(url)
    if not screenshot:
        return None

    extracted = _vision_extract(screenshot, prompt, url)
    if not extracted:
        return None

    normalized = normalize_content(extracted)
    return {
        "content": normalized,
        "content_hash": hash_content(normalized),
        "word_count": len(normalized.split()),
        "extraction_mode": "vision_fallback",
        "url": url,
    }


# ---------------------------------------------------------------------------
# Structured multi-selector extraction
# ---------------------------------------------------------------------------


def extract_structured(
    url: str,
    structured_selectors: dict[str, str | list[str]],
    mode: str = "rendered",
    original_prompt: str | None = None,
) -> dict[str, str | None]:
    """Extract multiple named values from a page using CSS or XPath selectors.

    Tries the scraper sidecar first when configured, then falls back to
    local Playwright/static extraction.  Selector type is auto-detected
    per field — you can mix CSS and XPath in the same dict.

    Each selector value can be either a single string or a **list of strings**.
    When a list is provided, selectors are tried in order and the first one
    that produces a non-empty result wins (first-match-wins).
    """
    # Try sidecar first for rendered/vision modes
    _auto_configure_sidecar()
    if mode in ("rendered", "vision") and _SIDECAR_URL:
        sidecar_result = _extract_structured_via_sidecar(url, structured_selectors)
        if sidecar_result is not None:
            return sidecar_result
        logger.debug("Sidecar structured extraction unavailable — falling back to local")

    results: dict[str, str | None] = {}

    if mode in ("rendered", "vision"):
        # Use Playwright for JS-rendered pages
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not installed — falling back to static fetch for structured extraction")
            return _extract_structured_static(url, structured_selectors)

        try:
            with sync_playwright() as p:
                browser, page = _launch_stealth_page(p, url)

                if _is_bot_wall(page):
                    logger.warning("Bot wall on %s — trying stealth HTTP for structured extraction", url)
                    browser.close()
                    return _extract_structured_stealth_http(url, structured_selectors)

                for field_name, selector_or_list in structured_selectors.items():
                    selectors = selector_or_list if isinstance(selector_or_list, list) else [selector_or_list]
                    value: str | None = None
                    for selector in selectors:
                        try:
                            pw_sel = f"xpath={selector}" if is_xpath(selector) else selector
                            elements = page.query_selector_all(pw_sel)
                            if elements:
                                parts = [el.inner_text().strip() for el in elements if el.inner_text().strip()]
                                if parts:
                                    value = " | ".join(parts)
                                    break
                        except Exception as exc:
                            logger.debug("Selector '%s' failed for field '%s': %s", selector, field_name, exc)
                    results[field_name] = value

                browser.close()

        except Exception as exc:
            logger.warning("Playwright structured extraction failed: %s — trying stealth HTTP", exc)
            return _extract_structured_stealth_http(url, structured_selectors)

    else:
        # Static fetch for text/markdown modes
        return _extract_structured_static(url, structured_selectors)

    return results


def _extract_structured_static(url: str, structured_selectors: dict[str, str | list[str]]) -> dict[str, str | None]:
    """Extract structured fields from raw HTML (static fetch, no JS)."""
    html = _fetch_raw_html(url)
    if not html:
        return {k: None for k in structured_selectors}
    return _extract_structured_from_html(html, structured_selectors)


def _extract_structured_stealth_http(
    url: str, structured_selectors: dict[str, str | list[str]]
) -> dict[str, str | None]:
    """Extract structured fields via curl_cffi stealth HTTP fetch."""
    html = _stealth_http_fetch(url)
    if not html:
        return {k: None for k in structured_selectors}
    return _extract_structured_from_html(html, structured_selectors)


def _extract_structured_from_html(html: str, structured_selectors: dict[str, str | list[str]]) -> dict[str, str | None]:
    """Extract multiple named fields from raw HTML using CSS (BeautifulSoup) or XPath (lxml).

    Each field's selector can be a single string or a list of strings
    (first-match-wins).  Selector type is auto-detected independently,
    so you can mix CSS and XPath in the same dict.
    """
    results: dict[str, str | None] = {}
    for field_name, selector_or_list in structured_selectors.items():
        selectors = selector_or_list if isinstance(selector_or_list, list) else [selector_or_list]
        value: str | None = None
        for selector in selectors:
            try:
                extracted = _extract_by_selector(html, selector)
                if extracted:
                    value = extracted
                    break
            except Exception as exc:
                logger.debug("Selector '%s' failed for field '%s': %s", selector, field_name, exc)
        results[field_name] = value

    return results


def compute_structured_diff(
    prev_structured: dict[str, str | None] | None,
    curr_structured: dict[str, str | None] | None,
) -> dict[str, dict[str, str | None]] | None:
    """Compare two structured content dicts and return per-field changes.

    Returns a dict of changed fields::

        {"price": {"old": "€ 719,-", "new": "€ 699,-"}, ...}

    Returns None if nothing changed or inputs are missing.
    """
    if not prev_structured or not curr_structured:
        return None

    changes: dict[str, dict[str, str | None]] = {}

    # Check all fields in both old and new
    all_fields = set(prev_structured.keys()) | set(curr_structured.keys())
    for field in all_fields:
        old_val = prev_structured.get(field)
        new_val = curr_structured.get(field)
        if old_val != new_val:
            changes[field] = {"old": old_val, "new": new_val}

    return changes if changes else None


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

_EXTRACTORS = {
    "text": extract_text,
    "markdown": extract_markdown,
    "rendered": extract_rendered,
}


def extract(
    url: str,
    mode: str = "text",
    css_selector: str | None = None,
    original_prompt: str | None = None,
    screenshot: bool = False,
) -> dict[str, Any]:
    """Extract and normalize page content.

    Tries the scraper sidecar first (for rendered/vision modes) when
    configured and available, then falls back to local extraction.
    """
    # For rendered/vision modes, try the scraper sidecar first.
    # The sidecar has a hardened Firefox stack with 18 anti-bot evasion
    # techniques — much better for protected sites (Akamai, Cloudflare).
    _auto_configure_sidecar()
    if mode in ("rendered", "vision") and _SIDECAR_URL:
        sidecar_result = _extract_via_sidecar(
            url,
            css_selector=css_selector,
            mode=mode,
            screenshot=screenshot,
        )
        if sidecar_result:
            return sidecar_result
        logger.debug("Sidecar unavailable or failed — continuing with local extraction")

    # Vision mode — always use screenshot + LLM
    if mode == "vision":
        return extract_vision(url, css_selector=css_selector, original_prompt=original_prompt)

    fn = _EXTRACTORS.get(mode)
    if not fn:
        return {"error": f"Unknown extraction mode: {mode!r} (valid: text, markdown, rendered, vision)"}

    # rendered mode accepts original_prompt for vision fallback
    if mode == "rendered":
        return fn(url, css_selector=css_selector, original_prompt=original_prompt)
    return fn(url, css_selector)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_raw_html(url: str) -> str | None:
    """Quick raw HTML fetch for CSS selector support in text/markdown modes."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgentForge-Monitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Raw HTML fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Scraper Sidecar integration
# ---------------------------------------------------------------------------

_SIDECAR_URL: str | None = None
_SIDECAR_TIMEOUT: int = 60
_SIDECAR_CONFIGURED: bool = False


def _auto_configure_sidecar() -> None:
    """Auto-configure the sidecar from env var or config.yaml.

    This is called lazily on first use so that both the agentforge-web process
    (configured via ``init_monitor`` → ``configure_sidecar``) and the
    native worker (separate process, never calls ``init_monitor``)
    pick up the sidecar URL.

    Resolution order:
      1. ``SIDECAR_URL`` env var  — lets the host worker override to
         ``http://localhost:8300`` while Docker containers use the
         Docker network name.
      2. ``config.yaml`` → ``monitor.sidecar.url`` — the canonical config.
    """
    global _SIDECAR_URL, _SIDECAR_TIMEOUT, _SIDECAR_CONFIGURED  # noqa: PLW0603
    if _SIDECAR_CONFIGURED:
        return
    _SIDECAR_CONFIGURED = True

    # 1. Env var takes priority (host worker sets this)
    env_url = os.environ.get("SIDECAR_URL")
    if env_url:
        _SIDECAR_URL = env_url.rstrip("/")
        _SIDECAR_TIMEOUT = int(os.environ.get("SIDECAR_TIMEOUT", "60"))
        logger.info("Sidecar configured from env: %s (timeout=%ds)", _SIDECAR_URL, _SIDECAR_TIMEOUT)
        return

    # 2. Fall back to config.yaml
    try:
        import yaml

        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        sidecar_cfg = cfg.get("monitor", {}).get("sidecar", {})
        if sidecar_cfg.get("enabled") and sidecar_cfg.get("url"):
            url = sidecar_cfg["url"].rstrip("/")

            # If running outside Docker (e.g., native worker on macOS),
            # the Docker service name (agentforge-sidecar) won't resolve.
            # Rewrite to localhost so the worker reaches the sidecar via
            # the exposed port mapping.
            if not _is_inside_docker():
                import re

                url = re.sub(r"://[^:/]+", "://localhost", url)

            _SIDECAR_URL = url
            _SIDECAR_TIMEOUT = sidecar_cfg.get("timeout", 60)
            logger.info("Sidecar configured from config.yaml: %s (timeout=%ds)", _SIDECAR_URL, _SIDECAR_TIMEOUT)
    except Exception as exc:
        logger.debug("Could not load sidecar config from config.yaml: %s", exc)


def _is_inside_docker() -> bool:
    """Detect if the current process is running inside a Docker container."""
    if os.environ.get("IN_DOCKER") == "1":
        return True
    return Path("/.dockerenv").exists()


def configure_sidecar(url: str | None, timeout: int = 60) -> None:
    """Explicitly configure the sidecar endpoint.

    Called by ``init_monitor()`` in the agentforge-web process.  Also accepts
    the ``SIDECAR_URL`` env var as an override so that the native
    worker (which never calls ``init_monitor``) can reach the sidecar at
    ``localhost:8300`` while Docker containers use the Docker network name.
    """
    global _SIDECAR_URL, _SIDECAR_TIMEOUT, _SIDECAR_CONFIGURED  # noqa: PLW0603
    _SIDECAR_CONFIGURED = True

    # Env var overrides the config.yaml value
    env_url = os.environ.get("SIDECAR_URL")
    if env_url:
        _SIDECAR_URL = env_url.rstrip("/")
    else:
        _SIDECAR_URL = url.rstrip("/") if url else None

    _SIDECAR_TIMEOUT = int(os.environ.get("SIDECAR_TIMEOUT", str(timeout)))

    if _SIDECAR_URL:
        logger.info("Sidecar configured: %s (timeout=%ds)", _SIDECAR_URL, _SIDECAR_TIMEOUT)


def _sidecar_auth_headers() -> dict[str, str]:
    """Shared-secret header for sidecar POSTs, when SIDECAR_AUTH_TOKEN is set."""
    token = os.environ.get("SIDECAR_AUTH_TOKEN", "").strip()
    return {"X-Sidecar-Token": token} if token else {}


def _is_sidecar_available() -> bool:
    """Check if the sidecar is configured and reachable."""
    _auto_configure_sidecar()
    if not _SIDECAR_URL:
        return False
    try:
        import httpx

        resp = httpx.get(f"{_SIDECAR_URL}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _extract_via_sidecar(
    url: str,
    css_selector: str | None = None,
    mode: str = "rendered",
    screenshot: bool = False,
    timeout: int | None = None,
) -> dict[str, Any] | None:
    """Try to extract content via the scraper sidecar HTTP API.

    Returns an extraction result dict on success, or None on failure
    (so the caller falls back to local extraction).
    """
    _auto_configure_sidecar()
    if not _SIDECAR_URL:
        return None

    import httpx

    try:
        resp = httpx.post(
            f"{_SIDECAR_URL}/extract",
            json={
                "url": url,
                "mode": mode,
                "css_selector": css_selector,
                "timeout": timeout or _SIDECAR_TIMEOUT,
                "screenshot": screenshot,
            },
            headers=_sidecar_auth_headers(),
            timeout=max(timeout or _SIDECAR_TIMEOUT, 30) + 10,  # HTTP timeout > extraction timeout
        )
        if resp.status_code != 200:
            logger.warning("Sidecar /extract returned %d: %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()

        # If the sidecar itself reports an error (bot wall, timeout, etc.),
        # return None so we fall back to local extraction.
        if data.get("error"):
            logger.info("Sidecar extraction error for %s: %s — falling back to local", url, data["error"])
            return None

        # Convert sidecar response to our internal format
        result: dict[str, Any] = {
            "content": data.get("content", ""),
            "content_hash": data.get("content_hash", ""),
            "word_count": data.get("word_count", 0),
            "extraction_mode": data.get("extraction_mode", mode),
            "url": url,
        }

        # Pass through base64 screenshot if captured
        if data.get("screenshot_b64"):
            result["screenshot_b64"] = data["screenshot_b64"]

        logger.info("Sidecar extraction succeeded for %s (%d words)", url, result["word_count"])
        return result

    except Exception as exc:
        logger.info("Sidecar unavailable for %s: %s — falling back to local", url, exc)
        return None


def _extract_structured_via_sidecar(
    url: str,
    structured_selectors: dict[str, str | list[str]],
    timeout: int | None = None,
) -> dict[str, str | None] | None:
    """Try structured extraction via the scraper sidecar.

    Returns a dict of field values on success, or None on failure.
    """
    _auto_configure_sidecar()
    if not _SIDECAR_URL:
        return None

    import httpx

    try:
        resp = httpx.post(
            f"{_SIDECAR_URL}/extract-structured",
            json={
                "url": url,
                "selectors": structured_selectors,
                "timeout": timeout or _SIDECAR_TIMEOUT,
                "screenshot": False,
            },
            headers=_sidecar_auth_headers(),
            timeout=max(timeout or _SIDECAR_TIMEOUT, 30) + 10,
        )
        if resp.status_code != 200:
            logger.warning("Sidecar /extract-structured returned %d", resp.status_code)
            return None

        data = resp.json()
        if data.get("error"):
            logger.info("Sidecar structured extraction error: %s — falling back", data["error"])
            return None

        fields = data.get("fields", {})

        # If every field came back None the selectors probably didn't match
        # (e.g., sidecar hasn't been rebuilt with XPath support yet).  Treat
        # this as a failure so the caller falls back to local extraction.
        if fields and all(v is None for v in fields.values()):
            logger.info("Sidecar structured extraction returned all-null fields — falling back to local extraction")
            return None

        logger.info("Sidecar structured extraction succeeded: %s", list(fields.keys()))
        return fields

    except Exception as exc:
        logger.info("Sidecar structured unavailable: %s — falling back", exc)
        return None
