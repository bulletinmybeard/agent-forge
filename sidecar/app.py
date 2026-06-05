"""Scraper Sidecar — browser extraction API for AgentForge monitor.

A lightweight FastAPI service that provides hardened browser automation with anti-bot evasion techniques ported from Price Scout's AsyncBaseProvider (18 evasion techniques, Firefox headed mode, Xvfb virtual display).

AgentForge's monitor system calls this sidecar for extraction instead of its own basic Playwright/curl_cffi code, giving it the ability to bypass Akamai Bot Manager, Cloudflare, and other bot detection systems.

Selectors can be CSS *or* XPath — auto-detected at runtime. Anything starting with ``/`` or ``(`` is treated as XPath; everything else is CSS.

Endpoints:
    POST /extract            — Extract page content (text, screenshot)
    POST /extract-structured — Extract multiple named CSS/XPath selector values
    POST /unsubscribe/click  — Click-through an unsubscribe landing page
    GET  /health             — Readiness check
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("sidecar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(
    title="AgentForge Scraper Sidecar",
    description="Hardened browser extraction API for AgentForge monitor — "
    "anti-bot evasion techniques ported from Price Scout",
    version="0.1.0",
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ExtractRequest(BaseModel):
    """Request body for /extract."""

    url: str
    mode: str = Field(default="rendered", description="Extraction mode: rendered, text, screenshot")
    css_selector: str | None = Field(default=None, description="Optional CSS or XPath selector to target a subtree")
    timeout: int = Field(default=30, description="Navigation timeout in seconds")
    screenshot: bool = Field(default=False, description="Also capture a PNG screenshot")


class ExtractResponse(BaseModel):
    """Response body for /extract."""

    url: str
    content: str | None = None
    content_hash: str | None = None
    word_count: int = 0
    extraction_mode: str = "rendered"
    screenshot_b64: str | None = Field(default=None, description="Base64-encoded PNG screenshot (if requested)")
    error: str | None = None
    duration_s: float = 0.0


class StructuredExtractRequest(BaseModel):
    """Request body for /extract-structured."""

    url: str
    selectors: dict[str, str | list[str]] = Field(
        ...,
        description=(
            "Named CSS or XPath selectors. Each value can be a single string "
            "or a list of strings (tried in order, first match wins). "
            'Example: {"price": ["//div[contains(text(), \'€\')]", ".price-tag"], '
            '"status": "//dt[normalize-space()=\'Status\']/following-sibling::dd[1]"}'
        ),
    )
    timeout: int = Field(default=30, description="Navigation timeout in seconds")
    screenshot: bool = Field(default=False, description="Also capture a PNG screenshot")


class StructuredExtractResponse(BaseModel):
    """Response body for /extract-structured."""

    url: str
    fields: dict[str, str | None] = Field(default_factory=dict)
    screenshot_b64: str | None = None
    error: str | None = None
    duration_s: float = 0.0


class UnsubClickRequest(BaseModel):
    """Request body for /unsubscribe/click.

    ``pre_click_text_match`` — if provided, the page body must contain this
    string before the click fires (defends against stale / hijacked URLs).
    ``avoid_button_texts`` — button labels to explicitly skip (e.g., the
    "Manage permissions" trap on the Microsoft unsubscribe page).
    ``success_patterns`` — regex list matched against the post-click H1 to
    decide whether the unsubscribe succeeded. Falls back to a multi-locale
    default when omitted.
    """

    url: str
    pre_click_text_match: str | None = None
    avoid_button_texts: list[str] = Field(default_factory=list)
    success_patterns: list[str] | None = None
    timeout: int = Field(default=30, description="Navigation timeout in seconds")


class UnsubClickResponse(BaseModel):
    """Response body for /unsubscribe/click."""

    url: str
    final_url: str | None = None
    clicked_selector: str | None = None
    status: str = "failed"  # clicked | not_found | pre_check_failed | failed
    success_detected: bool = False
    success_signal: str | None = None  # heading_match | url_change | button_gone
    final_heading: str | None = None
    screenshot_b64: str | None = None
    error: str | None = None
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Configuration (via env vars — set in Docker)
# ---------------------------------------------------------------------------

_HEADLESS: bool = os.environ.get("SIDECAR_HEADLESS", "false").lower() in ("true", "1")
_BROWSER_TYPE: str = os.environ.get("SIDECAR_BROWSER", "firefox")

# Shared-secret auth. When SIDECAR_AUTH_TOKEN is set, every extraction endpoint
# requires a matching X-Sidecar-Token header; /health stays open. Unset = open
# (warned at startup) — fine only because the service should now be bound to an
# internal network / localhost, not 0.0.0.0 on the host.
_AUTH_TOKEN: str = os.environ.get("SIDECAR_AUTH_TOKEN", "").strip()

# SSRF guard. The sidecar exists to fetch PUBLIC web pages, so by default it
# refuses URLs that resolve to internal/link-local/loopback/private space
# (cloud metadata, redis, qdrant, admin panels). Opt back in for internal
# extraction with SIDECAR_ALLOW_PRIVATE_URLS=1.
_ALLOW_PRIVATE_URLS: bool = os.environ.get("SIDECAR_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes")

if not _AUTH_TOKEN:
    logger.warning(
        "SIDECAR_AUTH_TOKEN not set — extraction endpoints are UNAUTHENTICATED. Set it (and have "
        "callers send X-Sidecar-Token) and keep the service off any public/host-exposed port."
    )


async def _require_token(x_sidecar_token: str | None = Header(default=None)) -> None:
    """Reject extraction requests without the shared secret (when one is set)."""
    if not _AUTH_TOKEN:
        return
    if not x_sidecar_token or not hmac.compare_digest(x_sidecar_token, _AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Sidecar-Token")


def _validate_public_url(url: str) -> str | None:
    """Return an error string if ``url`` is unsafe to fetch, else ``None``.

    http/https only; blocks hosts resolving to internal address space unless
    SIDECAR_ALLOW_PRIVATE_URLS is set (link-local/metadata stays blocked).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return f"invalid URL: {url}"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported URL scheme '{parsed.scheme}' (only http/https allowed)"
    host = parsed.hostname
    if not host:
        return f"URL has no host: {url}"

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"could not resolve host: {host}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_link_local:
            return f"refusing to fetch link-local address {ip} (SSRF guard)"
        if not _ALLOW_PRIVATE_URLS and (
            ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return f"refusing to fetch internal address {ip} (SSRF guard)"
    return None


# ---------------------------------------------------------------------------
# Selector type detection
# ---------------------------------------------------------------------------


def _is_xpath(selector: str) -> bool:
    """Return True if *selector* looks like an XPath expression."""
    s = selector.lstrip()
    return s.startswith("/") or s.startswith("(")


def _pw_selector(selector: str) -> str:
    """Convert a selector to Playwright's format.

    Playwright treats selectors starting with ``xpath=`` as XPath.
    CSS selectors are used as-is.
    """
    return f"xpath={selector}" if _is_xpath(selector) else selector


# ---------------------------------------------------------------------------
# Anti-bot evasion — ported from Price Scout's AsyncBaseProvider
# ---------------------------------------------------------------------------

# Bot wall detection phrases
_BOT_WALL_PHRASES = [
    "je bent bijna op de pagina",
    "even geduld",
    "controleren of u een mens bent",  # "Checking if you're human"
    "checking if you are human",
    "checking your browser",
    "please verify you are a human",
    "just a moment",
    "attention required",
    "access denied",
    "enable javascript and cookies",
]

# Cookie consent selectors — common "Accept All" buttons on Dutch sites
_COOKIE_ACCEPT_SELECTORS = [
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

# Stealth init script — 18 anti-fingerprinting evasions
# Based on Price Scout's AsyncBaseProvider stealth techniques
_STEALTH_SCRIPT = """
    // 1. Hide webdriver property
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Mock languages (Dutch locale)
    Object.defineProperty(navigator, 'languages', {
        get: () => ['nl-NL', 'nl', 'en-US', 'en']
    });

    // 3. Mock hardware concurrency
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

    // 4. Mock device memory
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // 5. Mock platform
    Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });

    // 6. Mock vendor (empty for Firefox)
    Object.defineProperty(navigator, 'vendor', { get: () => '' });

    // 7. Mock maxTouchPoints
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

    // 8-11. Mock screen properties
    Object.defineProperty(screen, 'width', { get: () => 1920 });
    Object.defineProperty(screen, 'height', { get: () => 1080 });
    Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
    Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
    Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

    // 12. Mock outer window dimensions
    window.outerWidth = 1920;
    window.outerHeight = 1080;
    window.screenX = 0;
    window.screenY = 0;

    // 13. Mock battery API
    navigator.getBattery = () => Promise.resolve({
        charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1
    });

    // 14. Override permissions API
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );

    // 15. Mock connection API
    Object.defineProperty(navigator, 'connection', {
        get: () => ({
            effectiveType: '4g', rtt: 100, downlink: 10, saveData: false
        })
    });

    // 16. Mock notification permission
    Object.defineProperty(Notification, 'permission', { get: () => 'default' });

    // 17. Consistent timezone offset (CET = UTC+1)
    const origDate = Date;
    const tzOff = -60;
    Date = class extends origDate { getTimezoneOffset() { return tzOff; } };
    Date.prototype = origDate.prototype;

    // 18. Override toString to hide proxy nature
    const origToString = Function.prototype.toString;
    Function.prototype.toString = function() {
        if (this === navigator.permissions.query) {
            return 'function query() { [native code] }';
        }
        return origToString.call(this);
    };

    // WebGL vendor/renderer spoofing
    const getParamOrig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return getParamOrig.call(this, param);
    };
"""


def _get_user_agent() -> str:
    """Return a realistic Firefox UA string matching the headed browser."""
    return "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"


def _get_base_url(url: str) -> str | None:
    """Extract scheme + host for warm-up navigation."""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


async def _is_bot_wall(page) -> bool:
    """Detect CAPTCHA / bot-verification wall by scanning body text."""
    try:
        text = await page.inner_text("body")
        text_lower = text.lower()[:3000]
        for phrase in _BOT_WALL_PHRASES:
            if phrase in text_lower:
                return True
    except Exception:
        pass
    return False


async def _dismiss_cookies(page) -> None:
    """Try to click a cookie consent 'Accept' button if visible."""
    for selector in _COOKIE_ACCEPT_SELECTORS:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=600):
                await btn.click(timeout=1500)
                logger.debug("Dismissed cookie banner: %s", selector)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Core extraction — hardened Playwright browser stack
# ---------------------------------------------------------------------------


async def _extract_with_browser(
    url: str,
    css_selector: str | None = None,
    timeout_s: int = 30,
    capture_screenshot: bool = False,
    structured_selectors: dict[str, str | list[str]] | None = None,
) -> dict[str, Any]:
    """Core extraction using hardened Playwright with anti-bot evasion.

    Launches Firefox in headed mode (Xvfb provides the virtual display),
    applies all 18 stealth evasions, performs warm-up navigation, then
    extracts content with optional CSS targeting and screenshot capture.
    """
    from playwright.async_api import async_playwright

    result: dict[str, Any] = {"url": url}
    timeout_ms = timeout_s * 1000

    try:
        async with async_playwright() as p:
            # Firefox in headed mode = best anti-bot (Xvfb provides display)
            if _BROWSER_TYPE == "firefox":
                browser = await p.firefox.launch(headless=_HEADLESS)
            elif _BROWSER_TYPE == "webkit":
                browser = await p.webkit.launch(headless=_HEADLESS)
            else:
                browser = await p.chromium.launch(
                    headless=_HEADLESS,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-infobars",
                        "--disable-extensions",
                        "--enable-features=NetworkService,NetworkServiceInProcess",
                        "--disable-features=VizDisplayCompositor",
                    ],
                )

            ua = _get_user_agent()
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="nl-NL",
                timezone_id="Europe/Amsterdam",
                user_agent=ua,
                extra_http_headers={
                    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
                    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept-Encoding": "gzip, deflate, br",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Cache-Control": "max-age=0",
                },
            )

            # Apply stealth evasions
            await context.add_init_script(_STEALTH_SCRIPT)

            page = await context.new_page()

            # ── Warm-up: visit domain root first (mimics real browsing) ──
            base_url = _get_base_url(url)
            if base_url:
                try:
                    logger.debug("Warm-up: visiting %s", base_url)
                    await page.goto(
                        base_url,
                        wait_until="domcontentloaded",
                        timeout=min(10_000, timeout_ms // 2),
                    )
                    # Brief homepage interaction (human-like)
                    await asyncio.sleep(0.8)
                    await page.evaluate("window.scrollBy(0, 150)")
                    await asyncio.sleep(0.4)
                    logger.debug("Warm-up complete")
                except Exception as exc:
                    logger.debug("Warm-up failed (non-critical): %s", exc)

            # Human-like pre-navigation delay
            await asyncio.sleep(0.3)

            # ── Navigate to the actual target ──
            # Set referer to look like we came from the homepage
            if base_url:
                await page.set_extra_http_headers({"Referer": base_url})

            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(2000)

            # SSRF re-check: navigation follows HTTP/JS redirects, which could
            # land on an internal address the initial guard never saw. Re-validate
            # the final URL before extracting anything.
            redir_err = _validate_public_url(page.url)
            if redir_err:
                raise ValueError(f"blocked post-redirect URL {page.url}: {redir_err}")

            # Random mouse movement + scroll (trigger event listeners)
            await page.mouse.move(200, 300)
            await asyncio.sleep(0.2)
            await page.evaluate("window.scrollBy(0, 100)")
            await asyncio.sleep(0.3)

            # Dismiss cookie banners (best-effort)
            await _dismiss_cookies(page)

            # ── Bot wall detection with retry ──
            if await _is_bot_wall(page):
                logger.warning("Bot wall detected on %s — waiting for auto-resolve", url)
                await page.wait_for_timeout(8000)

                if await _is_bot_wall(page):
                    logger.info("Still blocked — reloading %s", url)
                    await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.wait_for_timeout(5000)

                if await _is_bot_wall(page):
                    result["error"] = "Bot wall detected — could not bypass"
                    await browser.close()
                    return result

            # ── Screenshot (before extraction — visual record even if extraction fails) ──
            if capture_screenshot:
                png_bytes = await page.screenshot(full_page=True)
                result["screenshot_b64"] = base64.b64encode(png_bytes).decode("ascii")

            # ── Structured multi-selector extraction ──
            if structured_selectors:
                fields: dict[str, str | None] = {}
                for field_name, selector_or_list in structured_selectors.items():
                    # Normalize to a list of candidate selectors (first-match-wins).
                    candidates: list[str] = (
                        selector_or_list if isinstance(selector_or_list, list) else [selector_or_list]
                    )
                    value: str | None = None
                    for selector in candidates:
                        try:
                            # Support comma-separated fallback selectors within a
                            # single string.  For XPath the whole string is one
                            # expression (commas are part of XPath syntax), so
                            # don't split.
                            parts_selectors = (
                                [selector]
                                if _is_xpath(selector)
                                else [s.strip() for s in selector.split(",") if s.strip()]
                            )
                            for sel in parts_selectors:
                                pw_sel = _pw_selector(sel)
                                elements = await page.query_selector_all(pw_sel)
                                if elements:
                                    parts = []
                                    for el in elements:
                                        txt = (await el.inner_text()).strip()
                                        if txt:
                                            parts.append(txt)
                                    if parts:
                                        value = " | ".join(parts)
                                        break
                            if value is not None:
                                break
                        except Exception as exc:
                            logger.debug("Selector '%s' failed for '%s': %s", selector, field_name, exc)
                    fields[field_name] = value
                result["fields"] = fields

            # ── Content extraction (full page or CSS/XPath subtree) ──
            if css_selector:
                content_parts = []
                # XPath: use as single expression; CSS: split on comma for fallbacks
                sel_list = (
                    [css_selector]
                    if _is_xpath(css_selector)
                    else [s.strip() for s in css_selector.split(",") if s.strip()]
                )
                for sel in sel_list:
                    pw_sel = _pw_selector(sel)
                    elements = await page.query_selector_all(pw_sel)
                    for el in elements:
                        txt = (await el.inner_text()).strip()
                        if txt:
                            content_parts.append(txt)
                    if content_parts:
                        break
                content = "\n\n".join(content_parts) if content_parts else ""
            else:
                content = (await page.inner_text("body")).strip()

            content = _normalize_content(content)

            result["content"] = content
            result["content_hash"] = hashlib.sha256(content.encode()).hexdigest()
            result["word_count"] = len(content.split())

            await browser.close()

    except Exception as exc:
        result["error"] = str(exc)
        logger.exception("Browser extraction failed for %s", url)

    return result


# ---------------------------------------------------------------------------
# Content normalization
# ---------------------------------------------------------------------------

_DYNAMIC_PATTERNS = [
    re.compile(r"data-[\w-]+=\S+"),  # data-* attributes
    re.compile(r"[a-z]{1,3}_[A-Za-z0-9]{6,12}"),  # CSS module hashes
    re.compile(r"\b\d{10,13}\b"),  # Unix timestamps
    re.compile(r"cache[-_]?bust\S*", re.I),  # Cache busters
]


def _normalize_content(text: str) -> str:
    """Strip dynamic tokens to avoid false-positive diffs."""
    for pat in _DYNAMIC_PATTERNS:
        text = pat.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Readiness check — confirms the sidecar is running and Xvfb is available."""
    display = os.environ.get("DISPLAY", "")
    return {
        "status": "ok",
        "service": "agentforge-scraper-sidecar",
        "browser": _BROWSER_TYPE,
        "headless": _HEADLESS,
        "display": display,
    }


@app.post("/extract", response_model=ExtractResponse, dependencies=[Depends(_require_token)])
async def extract(req: ExtractRequest) -> ExtractResponse:
    """Extract page content using the hardened browser stack.

    Supports CSS and XPath selector targeting (auto-detected), optional
    screenshot capture, and full anti-bot evasion (Firefox headed + Xvfb +
    18 stealth techniques).
    """
    start = time.monotonic()

    err = _validate_public_url(req.url)
    if err:
        return ExtractResponse(url=req.url, extraction_mode=req.mode, error=f"Error: {err}", duration_s=0.0)

    raw = await _extract_with_browser(
        url=req.url,
        css_selector=req.css_selector,
        timeout_s=req.timeout,
        capture_screenshot=req.screenshot,
    )

    duration = time.monotonic() - start

    return ExtractResponse(
        url=raw["url"],
        content=raw.get("content"),
        content_hash=raw.get("content_hash"),
        word_count=raw.get("word_count", 0),
        extraction_mode=req.mode,
        screenshot_b64=raw.get("screenshot_b64"),
        error=raw.get("error"),
        duration_s=round(duration, 2),
    )


# ---------------------------------------------------------------------------
# Unsubscribe click-through — header-less senders (Option B for gmail_unsubscribe)
# ---------------------------------------------------------------------------

# Default multi-locale success patterns matched against the post-click H1.
_UNSUB_SUCCESS_PATTERNS_DEFAULT: list[str] = [
    r"unsubscrib",  # unsubscribe, unsubscribed, unsubscribing
    r"removed",
    r"success",
    r"no longer",  # "you will no longer receive"
    r"you have been",
    r"afgemeld",  # Dutch
    r"abgemeldet",  # German
    r"désinscrit",  # French
    r"cancelad",  # Spanish (cancelado/a)
]

# Selector candidates, tried in order. Most specific first.
_UNSUB_BUTTON_CANDIDATES: list[str] = [
    "button[aria-label='Unsubscribe' i]",
    "a[aria-label='Unsubscribe' i]",
    "main button:has-text('unsubscribe')",
    "main a:has-text('unsubscribe')",
    "button:has-text('unsubscribe')",
    "a:has-text('unsubscribe')",
    # Localised variants — broad, last-resort
    "button:has-text('afmelden')",
    "button:has-text('abmelden')",
    "button:has-text('se désinscrire')",
    "button:has-text('opt out')",
    "button:has-text('opt-out')",
]


async def _unsubscribe_click_browser(
    url: str,
    pre_click_text_match: str | None,
    avoid_button_texts: list[str],
    success_patterns: list[str],
    timeout_s: int,
) -> dict[str, Any]:
    """Navigate to ``url``, click the unsubscribe button, detect success.

    Uses the same stealth stack as ``/extract`` so we don't trip anti-bot
    checks on senders who protect their unsubscribe page.
    """
    from playwright.async_api import async_playwright

    avoid_lc = [t.lower() for t in (avoid_button_texts or [])]
    result: dict[str, Any] = {"url": url}
    timeout_ms = timeout_s * 1000

    try:
        async with async_playwright() as p:
            if _BROWSER_TYPE == "firefox":
                browser = await p.firefox.launch(headless=_HEADLESS)
            elif _BROWSER_TYPE == "webkit":
                browser = await p.webkit.launch(headless=_HEADLESS)
            else:
                browser = await p.chromium.launch(
                    headless=_HEADLESS,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )

            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="Europe/Amsterdam",
                user_agent=_get_user_agent(),
            )
            await context.add_init_script(_STEALTH_SCRIPT)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                result["status"] = "failed"
                result["error"] = f"navigation failed: {exc}"
                await browser.close()
                return result

            # SSRF re-check after redirects before interacting with the page.
            redir_err = _validate_public_url(page.url)
            if redir_err:
                result["status"] = "failed"
                result["error"] = f"blocked post-redirect URL {page.url}: {redir_err}"
                await browser.close()
                return result

            await page.wait_for_timeout(1500)
            await _dismiss_cookies(page)

            if await _is_bot_wall(page):
                result["status"] = "failed"
                result["error"] = "bot wall detected on unsubscribe page"
                try:
                    png = await page.screenshot(full_page=False)
                    result["screenshot_b64"] = base64.b64encode(png).decode("ascii")
                except Exception:
                    pass
                await browser.close()
                return result

            # Pre-click sanity check — refuse to click if the expected text
            # (usually the recipient email) isn't present on the page.
            if pre_click_text_match:
                try:
                    body_text = (await page.inner_text("body")).lower()
                except Exception:
                    body_text = ""
                if pre_click_text_match.lower() not in body_text:
                    result["status"] = "pre_check_failed"
                    result["error"] = f"expected text '{pre_click_text_match}' not found on page"
                    await browser.close()
                    return result

            initial_url = page.url

            clicked_selector: str | None = None
            for selector in _UNSUB_BUTTON_CANDIDATES:
                try:
                    locator = page.locator(selector).first
                    if not await locator.is_visible(timeout=500):
                        continue
                    try:
                        text = (await locator.text_content() or "").strip().lower()
                    except Exception:
                        text = ""
                    if avoid_lc and any(avoid in text for avoid in avoid_lc):
                        logger.debug(
                            "unsubscribe_click: skipping %s — avoid-match on text '%s'",
                            selector,
                            text[:60],
                        )
                        continue
                    await locator.click(timeout=3000)
                    clicked_selector = selector
                    break
                except Exception as exc:
                    logger.debug("unsubscribe_click: candidate %s failed: %s", selector, exc)
                    continue

            if clicked_selector is None:
                result["status"] = "not_found"
                result["error"] = "no unsubscribe button matched the selector candidates"
                try:
                    png = await page.screenshot(full_page=False)
                    result["screenshot_b64"] = base64.b64encode(png).decode("ascii")
                except Exception:
                    pass
                await browser.close()
                return result

            # Wait for the page to settle so success signals stabilise.
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                await page.wait_for_timeout(3000)

            final_url = page.url
            try:
                final_heading = (await page.text_content("h1")) or ""
            except Exception:
                final_heading = ""

            compiled = [re.compile(p, re.I) for p in success_patterns]
            success_signal: str | None = None
            if final_heading and any(p.search(final_heading) for p in compiled):
                success_signal = "heading_match"
            elif final_url != initial_url:
                success_signal = "url_change"
            else:
                try:
                    still_visible = await page.locator(clicked_selector).first.is_visible(timeout=500)
                    if not still_visible:
                        success_signal = "button_gone"
                except Exception:
                    pass

            result.update(
                {
                    "status": "clicked",
                    "clicked_selector": clicked_selector,
                    "final_url": final_url,
                    "final_heading": final_heading.strip() or None,
                    "success_detected": success_signal is not None,
                    "success_signal": success_signal,
                }
            )

            try:
                png = await page.screenshot(full_page=False)
                result["screenshot_b64"] = base64.b64encode(png).decode("ascii")
            except Exception:
                pass

            await browser.close()

    except Exception as exc:
        logger.exception("Unsubscribe click failed for %s", url)
        result["status"] = "failed"
        result["error"] = str(exc)

    return result


@app.post("/unsubscribe/click", response_model=UnsubClickResponse, dependencies=[Depends(_require_token)])
async def unsubscribe_click(req: UnsubClickRequest) -> UnsubClickResponse:
    """Navigate to an unsubscribe landing page, click the button, detect success.

    Strategy:
      1. Try a prioritised list of selectors (aria-label → text in <main> → text anywhere).
      2. Skip candidates whose text matches any ``avoid_button_texts`` — defends
         against traps like "Manage permissions" inline links.
      3. Click, wait for the page to settle, then classify success via heading
         match / URL change / button-gone heuristics.
      4. Always attempt a small screenshot so the caller can verify ambiguous cases.
    """
    start = time.monotonic()
    err = _validate_public_url(req.url)
    if err:
        return UnsubClickResponse(url=req.url, status="failed", error=f"Error: {err}", duration_s=0.0)
    patterns = req.success_patterns or _UNSUB_SUCCESS_PATTERNS_DEFAULT
    raw = await _unsubscribe_click_browser(
        url=req.url,
        pre_click_text_match=req.pre_click_text_match,
        avoid_button_texts=req.avoid_button_texts or [],
        success_patterns=patterns,
        timeout_s=req.timeout,
    )
    duration = time.monotonic() - start
    return UnsubClickResponse(
        url=raw.get("url", req.url),
        final_url=raw.get("final_url"),
        clicked_selector=raw.get("clicked_selector"),
        status=raw.get("status", "failed"),
        success_detected=bool(raw.get("success_detected")),
        success_signal=raw.get("success_signal"),
        final_heading=raw.get("final_heading"),
        screenshot_b64=raw.get("screenshot_b64"),
        error=raw.get("error"),
        duration_s=round(duration, 2),
    )


@app.post("/extract-structured", response_model=StructuredExtractResponse, dependencies=[Depends(_require_token)])
async def extract_structured(req: StructuredExtractRequest) -> StructuredExtractResponse:
    """Extract multiple named values from a page using CSS or XPath selectors.

    CSS selectors can include comma-separated fallbacks.  XPath selectors
    are used as-is (commas are valid XPath syntax).  Returns a dict mapping
    field names to their extracted text values.
    """
    start = time.monotonic()

    err = _validate_public_url(req.url)
    if err:
        return StructuredExtractResponse(url=req.url, error=f"Error: {err}", duration_s=0.0)

    raw = await _extract_with_browser(
        url=req.url,
        timeout_s=req.timeout,
        capture_screenshot=req.screenshot,
        structured_selectors=req.selectors,
    )

    duration = time.monotonic() - start

    return StructuredExtractResponse(
        url=raw["url"],
        fields=raw.get("fields", {}),
        screenshot_b64=raw.get("screenshot_b64"),
        error=raw.get("error"),
        duration_s=round(duration, 2),
    )
