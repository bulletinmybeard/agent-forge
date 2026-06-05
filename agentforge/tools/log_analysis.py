"""Log analysis tools — structured parsing and pattern detection for log data.

Provides an ``analyze_logs`` tool that extracts errors, warnings, patterns,
and timestamps from raw log text, giving the model structured findings to
interpret rather than forcing it to parse everything from raw output.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.log_analysis import register_log_analysis_tools

    registry = ToolRegistry()
    register_log_analysis_tools(registry)
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Severity patterns — order matters (most severe first)
_SEVERITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("FATAL", re.compile(r"\b(FATAL|PANIC|panic|EMERG)\b", re.IGNORECASE)),
    ("CRITICAL", re.compile(r"\b(CRITICAL|CRIT)\b", re.IGNORECASE)),
    ("ERROR", re.compile(r"\b(ERROR|ERR|Exception|Traceback|segfault|SIGSEGV|SIGABRT)\b")),
    ("WARNING", re.compile(r"\b(WARNING|WARN|WRN)\b", re.IGNORECASE)),
]

# Known issue patterns
_ISSUE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("permission_denied", re.compile(r"permission denied|access denied|forbidden|403", re.IGNORECASE)),
    (
        "connection_error",
        re.compile(r"connection refused|ECONNREFUSED|connect timeout|connection reset", re.IGNORECASE),
    ),
    ("timeout", re.compile(r"\btimeout\b|timed out|deadline exceeded", re.IGNORECASE)),
    ("out_of_memory", re.compile(r"out of memory|OOM|oom-kill|Cannot allocate memory", re.IGNORECASE)),
    ("disk_full", re.compile(r"no space left|disk full|ENOSPC", re.IGNORECASE)),
    ("resource_exhaustion", re.compile(r"too many open files|EMFILE|ENFILE|ulimit", re.IGNORECASE)),
    ("crash_restart", re.compile(r"restarting|respawning|core dump|killed|SIGKILL|SIGTERM", re.IGNORECASE)),
    ("auth_failure", re.compile(r"authentication fail|auth error|invalid token|unauthorized|401", re.IGNORECASE)),
    ("rate_limit", re.compile(r"rate limit|throttl|429|too many requests", re.IGNORECASE)),
    ("dns_error", re.compile(r"DNS|name resolution|NXDOMAIN|could not resolve", re.IGNORECASE)),
    ("ssl_tls_error", re.compile(r"SSL|TLS|certificate|handshake fail", re.IGNORECASE)),
]

# Timestamp patterns (common formats)
_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"  # ISO-ish
    r"|(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"  # syslog: Mar 12 23:07:03
    r"|(\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})"  # dd/mm/yy HH:MM:SS
)


# ---------------------------------------------------------------------------
# Core analysis function
# ---------------------------------------------------------------------------


def _analyze(text: str) -> dict:
    """Parse log text and return structured findings."""
    lines = text.splitlines()
    total_lines = len(lines)

    # --- Severity counts and sample lines ---
    severity_counts: Counter = Counter()
    severity_samples: dict[str, list[str]] = {s: [] for s, _ in _SEVERITY_PATTERNS}
    max_samples = 5  # keep up to N sample lines per severity

    for line in lines:
        for severity, pattern in _SEVERITY_PATTERNS:
            if pattern.search(line):
                severity_counts[severity] += 1
                if len(severity_samples[severity]) < max_samples:
                    severity_samples[severity].append(line.strip()[:300])
                break  # only count highest severity per line

    # --- Issue pattern detection ---
    issue_counts: Counter = Counter()
    issue_samples: dict[str, list[str]] = {}

    for line in lines:
        for issue_name, pattern in _ISSUE_PATTERNS:
            if pattern.search(line):
                issue_counts[issue_name] += 1
                if issue_name not in issue_samples:
                    issue_samples[issue_name] = []
                if len(issue_samples[issue_name]) < 3:
                    issue_samples[issue_name].append(line.strip()[:300])

    # --- Repeated error detection (exact dedup) ---
    error_lines: list[str] = []
    for line in lines:
        for _, pattern in _SEVERITY_PATTERNS[:3]:  # FATAL, CRITICAL, ERROR
            if pattern.search(line):
                # Strip timestamp for dedup
                cleaned = _TIMESTAMP_RE.sub("", line).strip()
                error_lines.append(cleaned[:200])
                break

    repeated_errors: list[tuple[str, int]] = [
        (msg, count) for msg, count in Counter(error_lines).most_common(10) if count > 1
    ]

    # --- Timestamp range ---
    timestamps: list[str] = []
    for line in lines[:5] + lines[-5:]:
        m = _TIMESTAMP_RE.search(line)
        if m:
            timestamps.append(m.group(0))

    first_ts = timestamps[0] if timestamps else None
    last_ts = timestamps[-1] if len(timestamps) > 1 else None

    # --- Health assessment ---
    fatal_critical = severity_counts.get("FATAL", 0) + severity_counts.get("CRITICAL", 0)
    errors = severity_counts.get("ERROR", 0)
    warnings = severity_counts.get("WARNING", 0)

    if fatal_critical > 0:
        health = "CRITICAL"
    elif errors > 10:
        health = "UNHEALTHY"
    elif errors > 0:
        health = "ISSUES_DETECTED"
    elif warnings > 5:
        health = "WARNINGS"
    elif warnings > 0:
        health = "MOSTLY_HEALTHY"
    else:
        health = "HEALTHY"

    return {
        "total_lines": total_lines,
        "health": health,
        "time_range": {"first": first_ts, "last": last_ts},
        "severity_counts": dict(severity_counts),
        "issue_counts": dict(issue_counts),
        "repeated_errors": repeated_errors,
        "severity_samples": {k: v for k, v in severity_samples.items() if v},
        "issue_samples": issue_samples,
    }


def _format_report(findings: dict) -> str:
    """Format analysis findings into a readable report."""
    parts: list[str] = []

    # Header
    health = findings["health"]
    total = findings["total_lines"]
    parts.append("=== LOG ANALYSIS REPORT ===")
    parts.append(f"Status: {health} | Lines analyzed: {total}")

    tr = findings["time_range"]
    if tr["first"]:
        parts.append(f"Time range: {tr['first']} → {tr['last'] or '(same)'}")

    parts.append("")

    # Severity summary
    sc = findings["severity_counts"]
    if sc:
        parts.append("--- SEVERITY COUNTS ---")
        for sev in ["FATAL", "CRITICAL", "ERROR", "WARNING"]:
            if sev in sc:
                parts.append(f"  {sev}: {sc[sev]}")
        parts.append("")

    # Issue patterns
    ic = findings["issue_counts"]
    if ic:
        parts.append("--- DETECTED ISSUES ---")
        for issue, count in sorted(ic.items(), key=lambda x: -x[1]):
            label = issue.replace("_", " ").title()
            parts.append(f"  {label}: {count} occurrence(s)")
            samples = findings["issue_samples"].get(issue, [])
            for s in samples[:2]:
                parts.append(f"    → {s}")
        parts.append("")

    # Repeated errors
    repeated = findings["repeated_errors"]
    if repeated:
        parts.append("--- REPEATED ERRORS (likely root causes) ---")
        for msg, count in repeated:
            parts.append(f"  [{count}x] {msg}")
        parts.append("")

    # Error/warning samples
    samples = findings["severity_samples"]
    if samples:
        parts.append("--- SAMPLE LOG LINES ---")
        for sev in ["FATAL", "CRITICAL", "ERROR", "WARNING"]:
            lines = samples.get(sev, [])
            if lines:
                parts.append(f"  {sev}:")
                for line in lines:
                    parts.append(f"    {line}")
        parts.append("")

    # No issues found
    if not sc and not ic:
        parts.append("No errors, warnings, or known issue patterns detected.")
        parts.append("The logs appear healthy.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use analyze_logs AFTER fetching log content via ssh(), shell(), or read_file(). "
        "Pass the raw log output as the 'logs' parameter. The tool will parse the text and "
        "return a structured analysis with severity counts, issue patterns, repeated errors, "
        "and a health assessment. Then interpret the results for the user."
    )
)
def analyze_logs(logs: str) -> str:
    """Analyze raw log text — extract errors, warnings, patterns, and health status.

    When to use: After fetching log content via ssh(), shell(), or read_file().
        Pass the raw log output to extract structured findings.
    When NOT to use: To fetch logs (use ssh/shell/read_file first), real-time
        log streaming (this analyzes static text), or if logs are already parsed.

    Pass the raw output from docker logs, journalctl, log files, etc.
    Returns a structured report with:
    - Overall health assessment (HEALTHY → CRITICAL)
    - Severity counts (FATAL, CRITICAL, ERROR, WARNING)
    - Known issue patterns (permission denied, OOM, timeouts, etc.)
    - Repeated error messages (likely root causes)
    - Sample log lines for each severity level

    logs: the raw log text to analyze (from ssh, shell, or read_file output)

    Examples:
      # After fetching remote logs:
      ssh('myserver', 'docker logs worker-1 --tail 200')
      analyze_logs(logs=<output from ssh>)

      # After reading a local log file:
      read_file('/var/log/syslog')
      analyze_logs(logs=<output from read_file>)
    """
    if not logs or not logs.strip():
        return "No log content provided. Fetch logs first with ssh(), shell(), or read_file()."

    logger.info("[analyze_logs] Analyzing %d characters of log text", len(logs))

    findings = _analyze(logs)
    report = _format_report(findings)

    logger.info(
        "[analyze_logs] Health: %s | Errors: %d | Warnings: %d | Issues: %d",
        findings["health"],
        findings["severity_counts"].get("ERROR", 0),
        findings["severity_counts"].get("WARNING", 0),
        sum(findings["issue_counts"].values()),
    )

    return report


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_log_analysis_tools(registry: ToolRegistry) -> int:
    """Register log analysis tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Log Analysis",
        "Log analysis tools parse raw log text and extract structured findings. "
        "Use analyze_logs AFTER fetching logs with ssh, shell, or read_file.",
    )

    tools = [analyze_logs]
    for func in tools:
        registry.register(func, category="Log Analysis")
    return len(tools)
