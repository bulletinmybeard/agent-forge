"""Network diagnostic tools — DNS lookups, connectivity probing, and HTTP inspection.

Complements the existing network_tools module (file downloads and URL fetching)
with lower-level diagnostic capabilities:

  dns_lookup  — Resolve any DNS record type for a hostname or IP address.
  net_probe   — ICMP ping reachability + TCP port open/closed check.
  http_check  — HTTP/HTTPS status, redirects, timing, and TLS certificate info.

All tools delegate to standard CLI utilities (dig, ping, nc, curl, openssl)
available on macOS and Linux by default.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.netdiag_tools import register_netdiag_tools

    registry = ToolRegistry()
    register_netdiag_tools(registry)
"""

from __future__ import annotations

import platform
import re
import shlex
import subprocess
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)

_VALID_RECORD_TYPES = {"A", "AAAA", "MX", "TXT", "NS", "CNAME", "PTR", "SOA", "SRV"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str] | str, timeout: int = 30, shell: bool = False) -> str:
    """Run a command and return stdout (or stderr on non-zero exit).

    cmd is an argv list run without a shell. Pass shell=True only for pipelines;
    in that case cmd must be a fully shlex.quote()d string.
    """
    try:
        result = subprocess.run(
            cmd,
            shell=shell,  # noqa: S603
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            stderr = result.stderr.strip()
            output = f"{output}\nSTDERR: {stderr}" if output else f"Error: {stderr}"
        return output or "(no output)"
    except FileNotFoundError as exc:
        # Surface a not-found message the callers can detect (dig fallback path).
        return f"Error: command not found: {exc}"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return f"Error: {exc}"


def _run_rc(cmd: list[str] | str, timeout: int = 10, shell: bool = False) -> tuple[str, int]:
    """Run a command and return (combined output, exit code).

    cmd is an argv list run without a shell. Pass shell=True only for pipelines;
    in that case cmd must be a fully shlex.quote()d string.
    """
    try:
        result = subprocess.run(
            cmd,
            shell=shell,  # noqa: S603
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip() or "(no output)"
        return output, result.returncode
    except FileNotFoundError as exc:
        return f"Error: command not found: {exc}", 1
    except subprocess.TimeoutExpired:
        return f"Error: timed out ({timeout}s)", 1
    except Exception as exc:
        return f"Error: {exc}", 1


def _is_macos() -> bool:
    return platform.system() == "Darwin"


# ---------------------------------------------------------------------------
# dns_lookup
# ---------------------------------------------------------------------------


@tool
def dns_lookup(domain: str, record_type: str = "A") -> str:
    """Resolve DNS records for a domain name or reverse-lookup an IP address.

    When to use: Find the IPs behind a domain (A/AAAA), check mail server
        configuration (MX), discover authoritative nameservers (NS), read
        TXT/SPF/DKIM records, follow canonical name aliases (CNAME), or
        reverse-resolve an IP to a hostname (PTR).
    When NOT to use: Checking if a host is reachable (use net_probe),
        fetching HTTP content (use curl_fetch or web_fetch).
    Input: domain — domain name (e.g., 'example.com') or IP for PTR lookups.
        record_type — DNS record type: A, AAAA, MX, TXT, NS, CNAME, PTR,
                      SOA, SRV (default: A).
    Output: Matching DNS records with TTL values, or a not-found message.

    Examples:
      dns_lookup('example.com')                # IPv4 address (A record)
      dns_lookup('example.com', 'AAAA')        # IPv6 address
      dns_lookup('example.com', 'MX')          # mail servers with priority
      dns_lookup('example.com', 'TXT')         # SPF, DKIM, site verification
      dns_lookup('example.com', 'NS')          # authoritative nameservers
      dns_lookup('example.com', 'CNAME')       # canonical name alias
      dns_lookup('8.8.8.8', 'PTR')             # reverse IP → hostname
    """
    record_type = record_type.upper().strip()
    if record_type not in _VALID_RECORD_TYPES:
        return f"Error: Unknown record type '{record_type}'. Supported: {', '.join(sorted(_VALID_RECORD_TYPES))}"

    # For PTR, auto-convert bare IPv4 addresses into reverse-arpa notation
    target = domain
    if record_type == "PTR" and not domain.endswith(".arpa"):
        parts = domain.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            target = ".".join(reversed(parts)) + ".in-addr.arpa"

    # +noall +answer +authority shows answer and authority sections cleanly.
    # +time=5 +tries=2 prevents long hangs on unresponsive resolvers.
    cmd = ["dig", "+noall", "+answer", "+authority", "+time=5", "+tries=2", target, record_type]
    output = _run(cmd, timeout=15)

    if "command not found" in output.lower():
        # dig not available — fall back to host(1)
        cmd = ["host", "-t", record_type, target]
        output = _run(cmd, timeout=15)

    if output.startswith("Error:"):
        return output

    if not output or output == "(no output)":
        return f"No {record_type} records found for '{domain}'."

    return f"DNS {record_type} lookup for '{domain}':\n\n{output}"


# ---------------------------------------------------------------------------
# net_probe
# ---------------------------------------------------------------------------


@tool
def net_probe(host: str, ports: str = "") -> str:
    """Ping a host and optionally check whether specific TCP ports are open.

    When to use: Verify a server is reachable (ping), confirm a service is
        listening on a port (web: 80/443, SSH: 22, database: 3306/5432, etc.),
        or diagnose network connectivity problems.
    When NOT to use: DNS resolution (use dns_lookup), HTTP content inspection
        (use http_check or curl_fetch), running commands remotely (use ssh).
    Input: host — hostname or IP address to probe.
        ports — comma-separated TCP port numbers to check (e.g., '22,80,443').
                Omit to ping only.
    Output: Ping result (reachable / unreachable, average round-trip time) and
        per-port open/closed status.

    Examples:
      net_probe('192.168.1.1')                  # ping only
      net_probe('example.com', '80,443')        # ping + HTTP/HTTPS ports
      net_probe('db.internal', '3306,5432')     # MySQL + PostgreSQL ports
      net_probe('server.lan', '22,80,443,8080,8443')
    """
    # Coerce ports: LLM may pass a list (["80", "443"]) instead of a string
    if isinstance(ports, list):
        ports = ",".join(str(p) for p in ports)

    lines: list[str] = []

    # --- Ping ---
    # -c 3: send 3 packets.
    # macOS: -W timeout in milliseconds; Linux: -W timeout in seconds.
    if _is_macos():
        ping_cmd = ["ping", "-c", "3", "-W", "1000", host]
    else:
        ping_cmd = ["ping", "-c", "3", "-W", "2", host]

    ping_out = _run(ping_cmd, timeout=15)
    lower = ping_out.lower()

    if any(
        msg in lower
        for msg in (
            "unknown host",
            "cannot resolve",
            "name or service not known",
            "nodename nor servname",
        )
    ):
        lines.append(f"Ping {host}: UNREACHABLE — hostname could not be resolved")
    elif "100% packet loss" in ping_out or "100.0% packet loss" in ping_out:
        lines.append(f"Ping {host}: UNREACHABLE (100% packet loss)")
    elif ping_out.startswith("Error:"):
        lines.append(f"Ping {host}: ERROR — {ping_out}")
    else:
        # Extract packet loss percentage
        loss = ""
        for line in ping_out.splitlines():
            if "packet loss" in line:
                m = re.search(r"(\d+(?:\.\d+)?%)\s+packet loss", line)
                if m:
                    loss = m.group(1)
                break

        # Extract RTT summary line (differs between macOS and Linux)
        rtt = ""
        for line in ping_out.splitlines():
            if "round-trip" in line or ("rtt" in line.lower() and "=" in line):
                rtt = line.strip()
                break

        summary = f"Ping {host}: REACHABLE"
        if loss and loss != "0%":
            summary += f" ({loss} loss)"
        if rtt:
            summary += f" — {rtt}"
        lines.append(summary)

    # --- Port checks ---
    if ports.strip():
        port_list = [p.strip() for p in ports.split(",") if p.strip()]
        lines.append("")
        lines.append("Port check:")
        for port_str in port_list:
            if not port_str.isdigit():
                lines.append(f"  :{port_str}  ERROR — invalid port number")
                continue
            port = int(port_str)
            # nc -z: scan-only (no data), -w: per-connect timeout in seconds.
            # Exit code 0 = open, non-zero = closed / filtered / refused.
            _, rc = _run_rc(["nc", "-z", "-w", "3", host, str(port)], timeout=8)
            status = "OPEN" if rc == 0 else "CLOSED / FILTERED"
            lines.append(f"  :{port:<5d}  {status}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# http_check
# ---------------------------------------------------------------------------


@tool
def http_check(url: str) -> str:
    """Check an HTTP or HTTPS endpoint — status, redirects, timing, and TLS certificate.

    When to use: Verify a web service is responding correctly, inspect HTTP
        status codes and redirect chains, check SSL/TLS certificate validity
        and expiry date, or measure connection and response time.
    When NOT to use: Reading page content (use web_fetch or curl_fetch),
        downloading a file (use download_file), raw TCP connectivity (use net_probe).
    Input: url — full URL to check (http:// or https://). Scheme is added
        automatically if omitted.
    Output: HTTP status code, content-type, response times (connect / TTFB / total),
        redirect count and final URL. For HTTPS: TLS certificate subject, issuer,
        and expiry date.

    Examples:
      http_check('https://example.com')
      http_check('http://api.internal:8080/health')
      http_check('expired.badssl.com')          # TLS error example
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # curl write-out format — each field on its own line as KEY:value.
    # Note: these are plain strings (no f-prefix), so %{...} is literal for curl.
    write_fmt = (
        "HTTP_CODE:%{http_code}\\n"
        "CONTENT_TYPE:%{content_type}\\n"
        "TIME_CONNECT:%{time_connect}\\n"
        "TIME_TTFB:%{time_starttransfer}\\n"
        "TIME_TOTAL:%{time_total}\\n"
        "REDIRECTS:%{num_redirects}\\n"
        "FINAL_URL:%{url_effective}\\n"
        "SSL_VERIFY:%{ssl_verify_result}\\n"
    )

    curl_cmd = ["curl", "-s", "-o", "/dev/null", "-L", "-m", "15", "--write-out", write_fmt, url]
    curl_out = _run(curl_cmd, timeout=20)

    if curl_out.startswith("Error:"):
        lower = curl_out.lower()
        if "could not resolve host" in lower:
            return f"Error: Could not resolve host — {url}"
        if "connection refused" in lower:
            return f"Error: Connection refused — {url}"
        if "ssl" in lower or "tls" in lower:
            return f"Error: TLS/SSL error — {url}\n{curl_out}"
        return f"Error reaching {url}: {curl_out}"

    # Parse KEY:value pairs — use partition so URLs with colons are preserved.
    meta: dict[str, str] = {}
    for line in curl_out.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()

    http_code = meta.get("HTTP_CODE", "?")
    content_type = meta.get("CONTENT_TYPE", "").split(";")[0].strip() or "—"
    time_connect = meta.get("TIME_CONNECT", "?")
    time_ttfb = meta.get("TIME_TTFB", "?")
    time_total = meta.get("TIME_TOTAL", "?")
    redirects = meta.get("REDIRECTS", "0")
    final_url = meta.get("FINAL_URL", url)
    ssl_verify = meta.get("SSL_VERIFY", "")

    try:
        code_int = int(http_code)
        status_icon = "✓" if code_int < 300 else ("→" if code_int < 400 else "✗")
    except ValueError:
        status_icon = "?"

    lines: list[str] = [f"HTTP check: {url}", ""]
    lines.append(f"Status:        {status_icon} {http_code}")
    lines.append(f"Content-Type:  {content_type}")
    lines.append(f"Response time: connect={time_connect}s  TTFB={time_ttfb}s  total={time_total}s")
    if redirects and redirects != "0":
        lines.append(f"Redirects:     {redirects} redirect(s) → {final_url}")
    if ssl_verify:
        verify_str = "OK" if ssl_verify == "0" else f"FAILED (OpenSSL code {ssl_verify})"
        lines.append(f"SSL verify:    {verify_str}")

    # --- TLS certificate details (HTTPS only) ---
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        host_part = parsed.hostname
        port_part = parsed.port or 443
        # Pipeline needs a shell; quote every interpolated value.
        connect = shlex.quote(f"{host_part}:{port_part}")
        servername = shlex.quote(str(host_part))
        openssl_cmd = (
            f"echo '' | openssl s_client "
            f"-connect {connect} "
            f"-servername {servername} "
            f"-verify_quiet 2>/dev/null "
            f"| openssl x509 -noout -subject -issuer -dates 2>/dev/null"
        )
        cert_out = _run(openssl_cmd, timeout=10, shell=True)
        if cert_out and not cert_out.startswith("Error:") and cert_out != "(no output)":
            lines.append("")
            lines.append("TLS Certificate:")
            for cert_line in cert_out.splitlines():
                cert_line = cert_line.strip()
                if cert_line:
                    lines.append(f"  {cert_line}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_netdiag_tools(registry: ToolRegistry) -> int:
    """Register all network diagnostic tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "NetDiag",
        "Network diagnostic tools: resolve DNS records (dns_lookup), "
        "check host reachability and open ports (net_probe), and inspect "
        "HTTP/HTTPS endpoints with TLS certificate details (http_check). "
        "Use for debugging connectivity, verifying service availability, "
        "and resolving hostnames to IPs.",
    )
    tools = [dns_lookup, net_probe, http_check]
    for func in tools:
        registry.register(func, category="NetDiag")
    return len(tools)
