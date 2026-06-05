"""web_render — headless-browser page rendering via Playwright.

Provides ``web_fetch_rendered``: a JS-aware alternative to ``web_fetch`` that
launches a headless Firefox instance, waits for the page to fully render
(including SPA hydration), and returns the resolved title, text content,
detected technologies, and outbound links.

Use this instead of ``web_fetch`` whenever the target URL is a React / Vue /
Angular / Svelte SPA, a Docusaurus / Next.js / Gatsby / Nuxt site, or any
page that relies on client-side JS to build its visible content.

Requirements:
  - ``playwright`` Python package (``pip install playwright``)
  - Firefox browser (``playwright install firefox``)

Firefox is preferred over Chromium for its better TLS fingerprint, lower
bot-detection surface, and lighter dependency footprint in containers.

If Playwright or Firefox is not available the tool returns a helpful error
message rather than crashing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


def _default_max_chars() -> int:
    """Default cap on rendered text (chars). Env AGENTFORGE_WEB_RENDER_MAX_CHARS."""
    try:
        return int(os.environ.get("AGENTFORGE_WEB_RENDER_MAX_CHARS", "12000"))
    except ValueError:
        return 12000


_DEFAULT_WEB_RENDER_MAX_CHARS = _default_max_chars()


# ---------------------------------------------------------------------------
# Screenshot storage
# ---------------------------------------------------------------------------


def _screenshots_dir() -> Path:
    """Return (and create) the shared screenshots directory.

    The Docker bind mount maps ``./data`` (repo root) → ``/app/data/`` inside
    the container.  The local worker must write to the repo-root ``data/``
    directory so Docker exposes the file to the web server at ``/app/data/``.

    Detection: prefer ``/app/data`` when that bind mount exists (we're inside a
    container). The package is pip-installed under ``site-packages``, so
    ``__file__`` is NOT under ``/app`` even in the container — checking the data
    mount is what keeps screenshots where the ``/api/screenshots`` route serves
    them. Otherwise we're the native local worker and use the repo-root path.
    """
    this_file = Path(__file__).resolve()
    logger.debug("[web_render] _screenshots_dir: __file__ resolved to %s", this_file)

    if Path("/app/data").is_dir():
        # Inside a container (the ./data bind mount is present as /app/data).
        d = Path("/app/data/uploads/screenshots")
        d.mkdir(parents=True, exist_ok=True)
        return d

    # Local worker: repo root is 3 levels up (agentforge/tools/web_render.py)
    repo_root = this_file.parent.parent.parent
    repo_data_dir = repo_root / "data"
    logger.debug("[web_render] _screenshots_dir: repo_root=%s, data exists=%s", repo_root, repo_data_dir.exists())

    if repo_data_dir.exists():
        d = repo_data_dir / "uploads" / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        logger.info("[web_render] Screenshots dir (local → repo-root): %s", d)
        return d

    # Fallback
    d = this_file.parent.parent.parent / "data" / "uploads" / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    logger.info("[web_render] Screenshots dir (fallback): %s", d)
    return d


def _screenshot_filename(url: str) -> str:
    """Deterministic filename from URL — short hash + sanitised host."""
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_")
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    return f"{host}_{url_hash}.png"


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

# Block requests that resolve to internal/link-local space so the agent can't
# be steered (or prompt-injected) into hitting cloud metadata (169.254.169.254),
# loopback, or RFC1918 services (redis, qdrant, internal admin panels).
# LAN/localhost targets are legitimate for some setups, so private ranges can be
# opted back in — but link-local (metadata) stays blocked unconditionally.
_ALLOW_PRIVATE_URLS = os.environ.get("AGENTFORGE_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes")


def _validate_url(url: str) -> str | None:
    """Return an error string if ``url`` is unsafe to fetch, else ``None``.

    Enforces an http/https scheme allowlist and blocks hosts that resolve to
    link-local / loopback / private / reserved addresses (SSRF). Covers both the
    sidecar dispatch and the local Playwright render path.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return f"Error: invalid URL: {url}"

    if parsed.scheme not in ("http", "https"):
        return f"Error: unsupported URL scheme '{parsed.scheme}' (only http/https are allowed)"

    host = parsed.hostname
    if not host:
        return f"Error: URL has no host: {url}"

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Can't resolve — not an SSRF risk; let the renderer surface the lookup error.
        return None

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_link_local:
            return f"Error: refusing to fetch link-local address {ip} (SSRF guard)"
        if not _ALLOW_PRIVATE_URLS and (
            ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return (
                f"Error: refusing to fetch private/internal address {ip} (SSRF guard). "
                "Set AGENTFORGE_ALLOW_PRIVATE_URLS=1 to allow LAN/localhost targets."
            )
    return None


# ---------------------------------------------------------------------------
# SPA / analytics fingerprinting
# ---------------------------------------------------------------------------

# Maps a regex pattern (matched against script src attributes and inline
# script text) to a human-readable technology label.
_TECH_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Frameworks
    (re.compile(r"react(?:[-_.]dom)?(?:\.min)?\.js", re.I), "React"),
    (re.compile(r"react-dom", re.I), "React"),
    (re.compile(r"_next/static", re.I), "Next.js"),
    (re.compile(r"__NEXT_DATA__", re.I), "Next.js"),
    (re.compile(r"nuxt", re.I), "Nuxt"),
    (re.compile(r"gatsby", re.I), "Gatsby"),
    (re.compile(r"__docusaurus", re.I), "Docusaurus"),
    (re.compile(r"vue(?:\.min)?\.js", re.I), "Vue"),
    (re.compile(r"angular", re.I), "Angular"),
    (re.compile(r"svelte", re.I), "Svelte"),
    (re.compile(r"ember", re.I), "Ember"),
    # SPA marker IDs
    (re.compile(r'id=["\']__next["\']', re.I), "Next.js"),
    (re.compile(r'id=["\']root["\']', re.I), "React SPA"),
    (re.compile(r'id=["\']app["\']', re.I), "Vue/React SPA"),
    (re.compile(r'id=["\']gatsby-focus-wrapper["\']', re.I), "Gatsby"),
    # Analytics
    (re.compile(r"google-analytics\.com|gtag\.js|ga\.js", re.I), "Google Analytics"),
    (re.compile(r"googletagmanager\.com", re.I), "Google Tag Manager"),
    (re.compile(r"segment\.com|segment\.io", re.I), "Segment"),
    (re.compile(r"mixpanel", re.I), "Mixpanel"),
    (re.compile(r"amplitude", re.I), "Amplitude"),
    (re.compile(r"plausible\.io", re.I), "Plausible"),
    (re.compile(r"fathom", re.I), "Fathom Analytics"),
    (re.compile(r"hotjar", re.I), "Hotjar"),
    (re.compile(r"clarity\.ms", re.I), "Microsoft Clarity"),
    (re.compile(r"posthog", re.I), "PostHog"),
    # CDNs / misc
    (re.compile(r"cloudflare", re.I), "Cloudflare"),
    (re.compile(r"netlify", re.I), "Netlify"),
    (re.compile(r"vercel", re.I), "Vercel"),
]


def _detect_technologies(html: str, script_srcs: list[str]) -> list[str]:
    """Return a deduplicated list of detected technologies."""
    found: dict[str, bool] = {}
    combined = html + "\n" + "\n".join(script_srcs)
    for pattern, label in _TECH_PATTERNS:
        if pattern.search(combined):
            found[label] = True
    return list(found.keys())


# ---------------------------------------------------------------------------
# Core async rendering function
# ---------------------------------------------------------------------------


async def _render(url: str, wait_for: str, timeout_ms: int) -> dict:
    """Launch headless Firefox, navigate to *url*, and extract content."""
    from playwright.async_api import TimeoutError as PWTimeout
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0"),
                java_script_enabled=True,
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            try:
                await page.goto(url, wait_until=wait_for, timeout=timeout_ms)
            except PWTimeout:
                # Page loaded but timed out waiting for the requested state —
                # still extract whatever content we have.
                logger.warning(
                    "[web_render] Timed out waiting for '%s' on %s — extracting partial content", wait_for, url
                )

            # Give late-firing JS (lazy hydration, async analytics) a moment.
            await asyncio.sleep(1)

            # SSRF re-check: goto follows HTTP/JS redirects, which could land on
            # an internal address the initial guard never saw. Re-validate the
            # final URL before reading any content.
            redir_err = _validate_url(page.url)
            if redir_err:
                raise PermissionError(f"blocked post-redirect URL {page.url}: {redir_err}")

            title = await page.title()
            html = await page.content()

            # Visible text — strip and compress whitespace
            try:
                body_text = await page.inner_text("body")
            except Exception:
                body_text = ""
            body_text = re.sub(r"\n{3,}", "\n\n", body_text.strip())
            body_text = re.sub(r"[ \t]+", " ", body_text)

            # Script src attributes (external + inline snippets for fingerprinting)
            script_srcs: list[str] = await page.eval_on_selector_all(
                "script[src]",
                "els => els.map(e => e.getAttribute('src'))",
            )
            # First 500 chars of each inline script for tech detection
            inline_snippets: list[str] = await page.eval_on_selector_all(
                "script:not([src])",
                "els => els.map(e => e.textContent.slice(0, 500))",
            )

            # Collect visible links
            links: list[str] = await page.eval_on_selector_all(
                "a[href]",
                "els => [...new Set(els.map(e => e.href).filter(h => h.startsWith('http')))].slice(0, 30)",
            )

            # Meta tags (description, og:*, twitter:*)
            metas: list[dict] = await page.eval_on_selector_all(
                "meta[name], meta[property]",
                "els => els.map(e => ({key: e.name || e.getAttribute('property'), value: e.content || ''}))",
            )

            # Capture a full-page screenshot before closing the browser
            screenshot_path: str | None = None
            try:
                ss_dir = _screenshots_dir()
                ss_file = ss_dir / _screenshot_filename(url)
                await page.screenshot(path=str(ss_file), full_page=True)
                # Return the web-accessible relative path (served via /uploads/ mount)
                screenshot_path = f"/api/screenshots/{ss_file.name}"
                logger.info("[web_render] Screenshot saved to disk: %s (web path: %s)", ss_file, screenshot_path)
            except Exception as ss_exc:
                logger.warning("[web_render] Screenshot failed for %s: %s", url, ss_exc)
        finally:
            # Always close the browser — orphan Firefox processes leak RAM on the host.
            try:
                await browser.close()
            except Exception as close_exc:
                logger.warning("[web_render] browser.close() failed: %s", close_exc)

    # Fingerprint using full HTML + script srcs + inline snippets
    tech_corpus = html + "\n".join(script_srcs + inline_snippets)
    technologies = _detect_technologies(tech_corpus, [])

    return {
        "title": title,
        "text": body_text,
        "script_srcs": script_srcs,
        "technologies": technologies,
        "metas": metas,
        "links": links,
        "screenshot": screenshot_path,
    }


# ---------------------------------------------------------------------------
# Sidecar routing
# ---------------------------------------------------------------------------

# The sidecar container has Firefox + Xvfb + the full stealth stack
# already. Rather than install Firefox into every container that might call
# web_fetch_rendered (the worker, SAQ workers), we route through the sidecar's
# /extract endpoint when one is configured. Local Playwright stays as a
# fallback so standalone CLI usage / tests still work.


def _sidecar_url() -> str | None:
    """Resolve the sidecar base URL — SIDECAR_URL env first, then config.yaml."""
    env_url = os.environ.get("SIDECAR_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        import yaml  # noqa: PLC0415 — lazy, keeps this module import-cheap

        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        if not config_path.exists():
            return None
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh) or {}
        sidecar_cfg = (cfg.get("monitor") or {}).get("sidecar") or {}
        if sidecar_cfg.get("enabled") and sidecar_cfg.get("url"):
            return str(sidecar_cfg["url"]).rstrip("/")
    except Exception as exc:  # noqa: BLE001
        logger.debug("web_render: sidecar config lookup failed: %s", exc)
    return None


def _sidecar_headers() -> dict[str, str]:
    """JSON content-type plus the shared-secret token when SIDECAR_AUTH_TOKEN is set."""
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SIDECAR_AUTH_TOKEN", "").strip()
    if token:
        headers["X-Sidecar-Token"] = token
    return headers


def _fetch_rendered_via_sidecar(
    url: str,
    *,
    timeout_s: int,
    capture_screenshot: bool,
) -> dict | None:
    """POST to sidecar's /extract. Returns None when unreachable.

    Response contract mirrors the sidecar's ``ExtractResponse``:
        {url, content, content_hash, word_count, screenshot_b64, error, duration_s}
    """
    base = _sidecar_url()
    if not base:
        return None
    payload = {
        "url": url,
        "mode": "rendered",
        "timeout": max(5, int(timeout_s)),
        "screenshot": bool(capture_screenshot),
    }
    data = json.dumps(payload).encode()
    # The sidecar's own navigation can run up to `timeout` seconds; add a bit of
    # slack so our HTTP call doesn't give up before the browser does.
    http_timeout = max(30, int(timeout_s) + 15)
    try:
        req = Request(
            f"{base}/extract",
            data=data,
            headers=_sidecar_headers(),
            method="POST",
        )
        with urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode())
    except (HTTPError, URLError) as exc:
        logger.warning("web_render: sidecar /extract HTTP error: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("web_render: sidecar /extract error: %s", exc)
        return None


def _save_sidecar_screenshot(url: str, screenshot_b64: str) -> str | None:
    """Decode + persist a sidecar screenshot. Returns the web-accessible path."""
    if not screenshot_b64:
        return None
    try:
        ss_dir = _screenshots_dir()
        ss_file = ss_dir / _screenshot_filename(url)
        ss_file.write_bytes(base64.b64decode(screenshot_b64))
        return f"/api/screenshots/{ss_file.name}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("web_render: sidecar screenshot save failed: %s", exc)
        return None


def _format_sidecar_response(url: str, sidecar: dict, max_chars: int) -> str:
    """Shape the sidecar's /extract response to match the local renderer's output."""
    body = (sidecar.get("content") or "").strip()
    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = True

    parts: list[str] = [
        f"Source: {url}  (rendered via scraper sidecar — Firefox + stealth)",
        "",
        "## Page content",
        body or "(empty)",
    ]
    if truncated:
        parts.append(f"\n... (truncated at {max_chars:,} chars — use a larger max_chars if needed)")

    ss_path = _save_sidecar_screenshot(url, sidecar.get("screenshot_b64"))
    if ss_path:
        parts.append(f"<!-- SCREENSHOT:{ss_path} -->")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@tool(
    locality="remote",
    hint=(
        "Use this tool instead of web_fetch when the target URL is a Single Page "
        "Application (React, Vue, Angular, Svelte), a JS-rendered site (Next.js, "
        "Nuxt, Gatsby, Docusaurus, Astro), or any page whose content or analytics "
        "tags are injected after page load. "
        "web_fetch only fetches raw HTML and misses anything rendered by JavaScript. "
        "web_fetch_rendered launches a real headless browser, waits for JS to "
        "execute, and returns the fully rendered text along with detected technologies "
        "(frameworks, analytics) and links. "
        "wait_for options: 'load' (DOM ready), 'domcontentloaded' (faster, less JS), "
        "'networkidle' (all async requests settled — most thorough, use for SPAs). "
        "Use 'networkidle' when checking for analytics tags or lazy-loaded content."
    ),
)
def web_fetch_rendered(
    url: str,
    wait_for: str = "networkidle",
    timeout: int = 30,
    max_chars: int = _DEFAULT_WEB_RENDER_MAX_CHARS,
) -> str:
    """Fetch a URL using a headless Firefox browser and return the fully rendered content.

    Executes JavaScript, waits for async rendering to complete, then extracts
    the visible text, detected technologies (frameworks, analytics), meta tags,
    and outbound links.  Use this for any JS-heavy or SPA page where web_fetch
    would return an empty shell.

    url:       Full URL to fetch (must start with http:// or https://)
    wait_for:  When to consider the page ready:
                 'networkidle'      — wait until no network requests for 500ms (default, best for SPAs)
                 'load'             — wait for the load event
                 'domcontentloaded' — wait for DOMContentLoaded (fastest, less JS executed)
    timeout:   Max seconds to wait for the page (default 30s)
    max_chars: Max characters of body text to return (default 12 000)

    Examples:
      web_fetch_rendered('https://example.com/')
      web_fetch_rendered('https://react-app.example.com', wait_for='networkidle')
      web_fetch_rendered('https://docs.example.com', wait_for='load', timeout=15)
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # SSRF guard — validate once before both the sidecar dispatch and the
    # local Playwright fallback so neither path can hit internal targets.
    err = _validate_url(url)
    if err:
        return err

    # Preferred path: route through sidecar, which already has Firefox
    # + the stealth stack. Keeps us from having to install a browser in
    # every caller's container (the worker, SAQ workers, etc.).
    sidecar_response = _fetch_rendered_via_sidecar(
        url,
        timeout_s=max(5, timeout),
        capture_screenshot=True,
    )
    if sidecar_response is not None:
        error = sidecar_response.get("error")
        if error:
            # Sidecar reached us but the browser failed (bot wall, network,
            # etc.). Surface the exact reason so the caller can decide what
            # to do — no fallback here; if the sidecar couldn't do it,
            # local Playwright isn't going to either.
            return f"Error rendering page (via sidecar): {error}"
        logger.info(
            "[web_render] Rendered %s via sidecar — %d chars text",
            url[:60],
            len((sidecar_response.get("content") or "")),
        )
        return _format_sidecar_response(url, sidecar_response, max_chars)

    # Fallback: local Playwright. Useful for CLI / test contexts where the
    # sidecar isn't running. Real production paths go through the sidecar.
    if not _playwright_available():
        return (
            "Error: no sidecar configured and Playwright is not installed locally.\n"
            "Set SIDECAR_URL to the sidecar endpoint (production) or install:\n"
            "  pip install playwright && playwright install firefox"
        )

    valid_states = {"load", "domcontentloaded", "networkidle"}
    if wait_for not in valid_states:
        wait_for = "networkidle"

    timeout_ms = max(5, timeout) * 1000

    logger.info(
        "[web_render] Rendering %s via local Playwright (wait_for=%s, timeout=%ds)",
        url,
        wait_for,
        timeout,
    )

    try:
        result = asyncio.run(_render(url, wait_for, timeout_ms))
    except Exception as exc:
        logger.error("[web_render] Failed to render %s: %s", url, exc)
        return f"Error rendering page: {exc}"

    # --- Build output ---
    parts: list[str] = []

    if result["title"]:
        parts.append(f"# {result['title']}")
    parts.append(f"Source: {url}  (rendered with headless Firefox, wait_for={wait_for})\n")

    # Technologies detected
    if result["technologies"]:
        parts.append("## Detected technologies")
        for tech in result["technologies"]:
            parts.append(f"  • {tech}")
        parts.append("")

    # Relevant meta tags
    relevant_metas = [
        m
        for m in result["metas"]
        if m["key"]
        and m["value"]
        and any(k in m["key"].lower() for k in ("description", "og:", "twitter:", "keywords"))
    ]
    if relevant_metas:
        parts.append("## Meta tags")
        for m in relevant_metas[:8]:
            parts.append(f"  {m['key']}: {m['value'][:120]}")
        parts.append("")

    # Script sources (useful for manual inspection)
    if result["script_srcs"]:
        parts.append("## Script sources")
        for src in result["script_srcs"][:15]:
            parts.append(f"  {src}")
        if len(result["script_srcs"]) > 15:
            parts.append(f"  ... and {len(result['script_srcs']) - 15} more")
        parts.append("")

    # Body text
    text = result["text"]
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    parts.append("## Page content")
    parts.append(text)
    if truncated:
        parts.append(f"\n... (truncated at {max_chars:,} chars — use a larger max_chars if needed)")

    # Links
    if result["links"]:
        parts.append(f"\n## Links ({len(result['links'])} found)")
        for link in result["links"][:10]:
            parts.append(f"  {link}")
        if len(result["links"]) > 10:
            parts.append(f"  ... and {len(result['links']) - 10} more")

    # Embed screenshot path as a hidden marker — the LLM won't include HTML
    # comments in its response, so _inject_screenshots picks it up reliably
    # without the LLM mangling the URL.
    if result.get("screenshot"):
        parts.append(f"<!-- SCREENSHOT:{result['screenshot']} -->")

    output = "\n".join(parts)
    logger.info(
        "[web_render] Rendered %s — %d chars text, techs: %s",
        url[:60],
        len(result["text"]),
        result["technologies"],
    )
    return output


# ---------------------------------------------------------------------------
# Standalone screenshot tool
# ---------------------------------------------------------------------------


async def _screengrab(url: str, wait_for: str, timeout_ms: int, output_path: str | None) -> dict:
    """Launch headless Firefox, navigate to *url*, and capture a screenshot."""
    from playwright.async_api import TimeoutError as PWTimeout
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0"),
                java_script_enabled=True,
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            try:
                await page.goto(url, wait_until=wait_for, timeout=timeout_ms)
            except PWTimeout:
                logger.warning("[web_screengrab] Timed out waiting for '%s' on %s — capturing anyway", wait_for, url)

            await asyncio.sleep(1)

            # SSRF re-check after redirects (see _render).
            redir_err = _validate_url(page.url)
            if redir_err:
                raise PermissionError(f"blocked post-redirect URL {page.url}: {redir_err}")

            title = await page.title()

            # Determine output file
            if output_path:
                out = Path(output_path).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
            else:
                out = _screenshots_dir() / _screenshot_filename(url)

            await page.screenshot(path=str(out), full_page=True)
        finally:
            try:
                await browser.close()
            except Exception as close_exc:
                logger.warning("[web_screengrab] browser.close() failed: %s", close_exc)

    return {
        "title": title,
        "path": str(out),
        "web_path": f"/api/screenshots/{out.name}" if not output_path else None,
    }


@tool(
    locality="local",
    hint=(
        "Use this tool to capture a full-page screenshot of any website. "
        "It launches a headless Firefox browser, renders the page (including JS), "
        "and saves a PNG screenshot. "
        "Optionally save to a specific path (e.g., ~/Downloads/site.png). "
        "If no output_path is given, the screenshot is stored in the server's "
        "uploads directory and returned as a linked thumbnail. "
        "Use this when the user explicitly asks for a screenshot/screengrab of a URL, "
        "or when you need a visual capture of a web page. "
        "Runs on the local worker so screenshots can be saved to local paths "
        "like ~/Downloads and are also available to the web server via the "
        "shared data/ bind mount."
    ),
)
def web_screengrab(
    url: str,
    output_path: str = "",
    wait_for: str = "networkidle",
    timeout: int = 30,
    **kwargs,
) -> str:
    """Capture a full-page screenshot of a URL using headless Firefox.

    url:         Full URL to screenshot (must start with http:// or https://)
    output_path: Optional file path to save the PNG (e.g., ~/Downloads/site.png).
                 If omitted, saved to server uploads directory.
    wait_for:    When to consider the page ready:
                   'networkidle'      — wait until no network requests for 500ms (default)
                   'load'             — wait for the load event
                   'domcontentloaded' — wait for DOMContentLoaded (fastest)
    timeout:     Max seconds to wait for the page (default 30s)

    Examples:
      web_screengrab('https://example.com/')
      web_screengrab('https://example.com', output_path='~/Downloads/example.png')
      web_screengrab('https://react-app.example.com', wait_for='networkidle')
    """
    # Handle parameter aliases and LLM arg-packing quirks.
    # Some models pass output_path nested inside a "kwargs" JSON string:
    #   web_screengrab(url="...", kwargs='{"output_path": "/Users/.../file.png"}')
    # Unpack that so the parameter actually reaches _screengrab.
    if not output_path and "kwargs" in kwargs:
        raw = kwargs["kwargs"]
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                import json as _json

                packed = _json.loads(raw)
                output_path = packed.get("output_path", "") or packed.get("save_to", "") or packed.get("path", "")
            except (ValueError, TypeError):
                pass
    output_path = output_path or kwargs.get("save_to", "") or kwargs.get("path", "") or kwargs.get("output_path", "")

    if not _playwright_available():
        return "Error: Playwright is not installed. Run:\n  pip install playwright && playwright install firefox"

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # SSRF guard before navigating the headless browser.
    err = _validate_url(url)
    if err:
        return err

    valid_states = {"load", "domcontentloaded", "networkidle"}
    if wait_for not in valid_states:
        wait_for = "networkidle"

    timeout_ms = max(5, timeout) * 1000

    logger.info("[web_screengrab] Capturing %s (wait_for=%s, timeout=%ds)", url, wait_for, timeout)

    try:
        result = asyncio.run(_screengrab(url, wait_for, timeout_ms, output_path or None))
    except Exception as exc:
        logger.error("[web_screengrab] Failed to capture %s: %s", url, exc)
        return f"Error capturing screenshot: {exc}"

    parts: list[str] = []
    parts.append(f"Screenshot captured: **{result['title'] or url}**")
    parts.append(f"Saved to: `{result['path']}`")

    # If saved to server uploads, include a viewable thumbnail
    if result.get("web_path"):
        parts.append(f"\n[![Screenshot]({result['web_path']})]({result['web_path']})")

    # If user requested a custom path, also copy to uploads for the thumbnail
    if output_path and not result.get("web_path"):
        try:
            ss_dir = _screenshots_dir()
            ss_file = ss_dir / _screenshot_filename(url)
            shutil.copy2(result["path"], str(ss_file))
            web_path = f"/api/screenshots/{ss_file.name}"
            parts.append(f"\n[![Screenshot]({web_path})]({web_path})")
        except Exception:
            pass  # thumbnail is nice-to-have

    output = "\n".join(parts)
    logger.info("[web_screengrab] Screenshot saved: %s", result["path"])
    return output


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_web_render_tools(registry: "ToolRegistry") -> int:
    """Register headless rendering tools with the given registry."""
    registry.register_category_hint(
        "Web Rendering",
        "Headless-browser page rendering for SPAs and JS-heavy sites. "
        "Use web_fetch_rendered when web_fetch returns empty or incomplete content "
        "because the page relies on JavaScript to build its content. "
        "Use web_screengrab to capture a screenshot of any URL on demand.",
    )
    registry.register(web_fetch_rendered, category="Web Rendering")
    registry.register(web_screengrab, category="Web Rendering")
    return 2
