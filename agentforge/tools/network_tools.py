"""Network tools — download files and fetch URLs.

Uses ``curl`` under the hood (available on macOS and Linux by default).
Provides file downloads with progress/size reporting and URL fetching
for inspecting headers, response codes, and content.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.network_tools import register_network_tools

    registry = ToolRegistry()
    register_network_tools(registry)
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(argv: list[str], timeout: int = 120) -> str:
    """Run a command (argv list, no shell) and return stdout (or stderr on failure).

    Always invoked with an explicit argument vector and ``shell=False`` so that
    URLs/paths can never be interpreted as shell syntax (command injection).
    """
    try:
        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            stderr = result.stderr.strip()
            if output:
                output += f"\nSTDERR: {stderr}"
            else:
                output = f"Error: {stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except FileNotFoundError:
        return "Error: curl command not found. Is cURL installed?"
    except Exception as exc:
        return f"Error: {exc}"


# SSRF guard. Block requests that resolve to internal/link-local space so the
# agent can't be steered (or prompt-injected) into hitting cloud metadata
# (169.254.169.254), loopback, or RFC1918 services. LAN/localhost targets are
# legitimate for some setups, so private ranges can be opted back in — but
# link-local (metadata) stays blocked unconditionally.
_ALLOW_PRIVATE_URLS = os.environ.get("AGENTFORGE_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes")


def _validate_url(url: str) -> str | None:
    """Return an error string if ``url`` is unsafe to fetch, else ``None``.

    Enforces an http/https scheme allowlist and blocks hosts that resolve to
    link-local / loopback / private / reserved addresses (SSRF). This checks one
    URL; redirect chains are validated separately by ``_resolve_redirects`` before
    the fetch, so a 302 to an internal address is also blocked.
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
        # Can't resolve — not an SSRF risk; let curl surface the lookup error.
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


def _resolve_redirects(url: str, max_hops: int = 10) -> str | None:
    """Walk the redirect chain one hop at a time, validating each against the
    SSRF guard. Returns an error string if any hop is unsafe (or the chain is too
    long), else ``None``.

    ``curl -L`` follows 3xx redirects itself, so validating only the initial URL
    leaves a hole: a public host can 302 to ``http://169.254.169.254/...`` or an
    RFC1918 address. This pre-walks the chain with cheap HEAD requests and blocks
    it before the real fetch follows the same (now-validated) hops. Best-effort:
    if a host rejects HEAD we stop walking and let the initial-URL guard stand.
    """
    current = url
    seen: set[str] = set()
    for _ in range(max_hops):
        err = _validate_url(current)
        if err:
            return err
        if current in seen:
            return f"Error: redirect loop detected at {current}"
        seen.add(current)
        out = _run(
            [
                "curl",
                "-sI",
                "--max-redirs",
                "0",
                "--proto",
                "=http,https",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code} %{redirect_url}",
                current,
            ],
            timeout=20,
        )
        if out.startswith("Error:"):
            return None  # HEAD unsupported / network error — let the main fetch surface it
        parts = out.strip().split(None, 1)
        code = parts[0] if parts else ""
        redirect = parts[1].strip() if len(parts) > 1 else ""
        if code.startswith("3") and redirect:
            current = redirect
            continue
        return None  # not a redirect — chain ends here, all hops validated
    return "Error: too many redirects (possible SSRF redirect chain)"


def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


# Extensions that clearly denote a non-binary HTML-ish page — used to decide
# whether an HTML response at a file URL is a "download gate" worth following.
_NON_BINARY_EXTS = {"", "html", "htm", "php", "asp", "aspx", "jsp"}


def _is_binary_asset_url(url: str) -> bool:
    """True when the URL's last path segment has a non-HTML file extension."""
    name = url.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    ext = name[dot + 1 :].lower() if dot > 0 else ""
    return ext not in _NON_BINARY_EXTS


def _looks_like_html(path: Path) -> bool:
    """Sniff the first KB for an HTML document marker."""
    try:
        head = path.read_bytes()[:1024].lstrip().lower()
    except OSError:
        return False
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head[:300]


def _resolve_download_gate(url: str, html: str) -> str | None:
    """Pull the real file URL out of an HTML download-redirect interstitial.

    ``curl -L`` follows HTTP redirects but not JS ones, so some sites (e.g.,
    WordPress download gates with a rotating token) serve an HTML page at the
    file URL. The real URL is embedded in that page. Handles three shapes: a JS
    string-replace transform of the current URL, a meta refresh, and a literal
    location assignment. Returns an absolute URL or None.
    """
    m = re.search(r"\.replace\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)", html)
    if m and m.group(1) in url:
        return urljoin(url, url.replace(m.group(1), m.group(2)))

    m = re.search(r"http-equiv=['\"]refresh['\"][^>]*content=['\"][^'\"]*url=([^'\"\s]+)", html, re.I)
    if m:
        return urljoin(url, m.group(1).replace("&amp;", "&"))

    m = re.search(
        r"(?:location\.(?:href|replace)\s*=?\s*\(?|window\.location\s*=)\s*['\"]([^'\"]+)['\"]",
        html,
        re.I,
    )
    if m:
        return urljoin(url, m.group(1))

    return None


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------


@tool
def download_file(url: str, destination: str = "") -> str:
    """Download a file from a URL to the local machine.

    ALWAYS use this tool to download or save a file — do NOT shell out to
    ``curl``/``wget`` via run_command for downloads. This tool follows JS and
    meta-refresh "download redirect" gates (e.g., WordPress token gates) that a
    plain ``curl -L`` silently misses, saving the HTML interstitial as your file
    instead of the real binary. Call it once per URL.

    When to use: Save a binary or text file from the internet to disk —
        installers, archives, datasets, release assets, PDFs, config files, etc.
    When NOT to use: Reading a web page as text (use web_fetch or curl_fetch),
        downloading a video/audio (use ytdlp_download),
        copying files between hosts (use scp or rsync).
    Input: url — the full URL to download from.
        destination — local file path or directory (default: current directory).
        If a directory is given, the filename is derived from the URL.
    Output: Path of the saved file, size, HTTP status code, and download duration.
    """
    err = _validate_url(url)
    if err:
        return err
    err = _resolve_redirects(url)
    if err:
        return err

    # Resolve destination
    if destination:
        dest_path = Path(destination).expanduser().resolve()
    else:
        dest_path = Path.cwd()

    # If destination is a directory (or doesn't exist but no extension),
    # derive filename from URL
    if dest_path.is_dir() or (not dest_path.exists() and not dest_path.suffix):
        dest_path.mkdir(parents=True, exist_ok=True)
        # Extract filename from URL (strip query params)
        url_path = url.split("?")[0].split("#")[0].rstrip("/")
        filename = url_path.split("/")[-1] or "download"
        dest_path = dest_path / filename
    else:
        # Ensure parent directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Build curl command
    #   -L  : follow redirects
    #   -f  : fail silently on HTTP errors (returns exit code 22)
    #   -S  : show errors even with -s
    #   -s  : silent (no progress bar in capture mode)
    #   -o  : output file
    #   -w  : write out metadata after download
    write_format = "%{http_code} %{size_download} %{time_total}"
    output = _run(
        [
            "curl",
            "-L",
            "--proto-redir",
            "=http,https",
            "--max-redirs",
            "10",
            "-f",
            "-S",
            "-s",
            "-o",
            str(dest_path),
            "-w",
            write_format,
            url,
        ],
        timeout=300,
    )

    if output.startswith("Error:"):
        lower = output.lower()
        if "could not resolve host" in lower:
            return f"Error: Could not resolve host. Check the URL: {url}"
        if "connection refused" in lower:
            return f"Error: Connection refused by server: {url}"
        if "ssl" in lower:
            return f"Error: SSL/TLS error connecting to: {url}"
        # HTTP error codes from -f flag
        if "22" in output and "the requested url returned error" in lower:
            return f"Error: Server returned an HTTP error for: {url}"
        return output

    # Parse curl write-out metadata
    parts = output.strip().split()
    http_code = parts[0] if parts else "?"
    time_secs = float(parts[2]) if len(parts) > 2 else 0

    # Verify file exists
    if not dest_path.exists():
        return f"Error: Download completed but file not found at {dest_path}"

    # Follow a JS download gate: we asked for a binary asset but got an HTML
    # interstitial. Resolve the real URL from the page and retry once. The gate
    # page is usually picky about Referer, so pass the original URL via -e.
    followed_gate = False
    if _is_binary_asset_url(url) and _looks_like_html(dest_path):
        real_url = _resolve_download_gate(url, dest_path.read_text(errors="ignore"))
        if real_url and real_url != url and not _validate_url(real_url) and not _resolve_redirects(real_url):
            retry = _run(
                [
                    "curl",
                    "-L",
                    "--proto-redir",
                    "=http,https",
                    "--max-redirs",
                    "10",
                    "-f",
                    "-S",
                    "-s",
                    "-e",
                    url,
                    "-o",
                    str(dest_path),
                    "-w",
                    write_format,
                    real_url,
                ],
                timeout=300,
            )
            if not retry.startswith("Error:") and dest_path.exists() and not _looks_like_html(dest_path):
                retry_parts = retry.strip().split()
                http_code = retry_parts[0] if retry_parts else http_code
                time_secs = float(retry_parts[2]) if len(retry_parts) > 2 else time_secs
                followed_gate = True

    actual_size = dest_path.stat().st_size
    return (
        f"Downloaded: {dest_path}\n"
        f"  Size: {_human_size(actual_size)}\n"
        f"  HTTP status: {http_code}\n"
        f"  Time: {time_secs:.1f}s" + ("\n  (followed a download-redirect gate)" if followed_gate else "")
    )


# ---------------------------------------------------------------------------
# curl_fetch
# ---------------------------------------------------------------------------


@tool
def curl_fetch(url: str, headers_only: bool = False) -> str:
    """Fetch content or HTTP headers from a URL using curl.

    When to use: Inspect HTTP response codes, headers, or raw body content
        from an API or web endpoint — e.g., debugging a REST API, checking
        redirects, or verifying a server is reachable.
    When NOT to use: Downloading a file to disk (use download_file),
        reading a web page as clean readable text (use web_fetch),
        structured web search (use web_search).
    Input: url — the full URL to fetch.
        headers_only — set true to issue a HEAD request and return only headers.
    Output: Raw HTTP response body with metadata (HTTP status, content-type,
        size, time). Truncated to 8000 chars if the response is large.
    """
    err = _validate_url(url)
    if err:
        return err
    err = _resolve_redirects(url)
    if err:
        return err

    if headers_only:
        # -I  : HEAD request (headers only)
        # -L  : follow redirects (chain already SSRF-validated above)
        # -s  : silent
        output = _run(
            ["curl", "-I", "-L", "--proto-redir", "=http,https", "--max-redirs", "10", "-s", url],
            timeout=30,
        )

        if output.startswith("Error:"):
            return output

        return f"Headers for {url}:\n\n{output}"

    # Full fetch with metadata
    #   -L  : follow redirects
    #   -s  : silent
    #   -S  : show errors
    #   -w  : write metadata to stderr so we can separate it from body
    write_format = "\\n---CURL_META---\\nHTTP %{http_code} | %{content_type} | %{size_download} bytes | %{time_total}s"
    output = _run(
        ["curl", "-L", "--proto-redir", "=http,https", "--max-redirs", "10", "-s", "-S", "-w", write_format, url],
        timeout=60,
    )

    if output.startswith("Error:"):
        lower = output.lower()
        if "could not resolve host" in lower:
            return f"Error: Could not resolve host. Check the URL: {url}"
        if "connection refused" in lower:
            return f"Error: Connection refused by server: {url}"
        return output

    # Truncate large responses to avoid flooding the context
    max_chars = 8000
    if len(output) > max_chars:
        # Try to preserve the CURL_META footer
        meta_marker = "---CURL_META---"
        meta_idx = output.rfind(meta_marker)

        if meta_idx > 0:
            meta = output[meta_idx:]
            body = output[:meta_idx]
            truncated_body = body[:max_chars] + f"\n\n... (truncated, {len(body)} chars total)\n"
            output = truncated_body + meta
        else:
            output = output[:max_chars] + f"\n\n... (truncated, {len(output)} chars total)"

    return f"Response from {url}:\n\n{output}"


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_network_tools(registry: ToolRegistry) -> int:
    """Register all network tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Network",
        "Network tools download files and fetch URLs from the internet to the LOCAL machine.",
    )

    tools = [
        download_file,
        curl_fetch,
    ]
    for func in tools:
        registry.register(func, category="Network")
    return len(tools)
