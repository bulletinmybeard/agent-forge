"""Mode dispatcher — parse @-prefixed modes and route to specialised runners.

Supported modes:
    @search   — web-search-first agent (always searches before answering)
    @logs     — log file analysis, error diagnosis, and fix proposals
    @discover — multi-phase investigative agent (system analysis)

Usage::

    from agentforge.modes import parse_mode, get_mode_config

    mode, clean_query = parse_mode("@search best nginx reverse proxy setup")
    # mode = "search", clean_query = "best nginx reverse proxy setup"

    if mode:
        config = get_mode_config(mode)
        # config.profile, config.system_prompt, config.tools, ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Mode parsing
# ---------------------------------------------------------------------------

# Matches @mode at the start of a query, optionally followed by whitespace
_MODE_PATTERN = re.compile(r"^@(\w+)\s*(.*)", re.DOTALL)


def parse_mode(query: str) -> tuple[str | None, str]:
    """Extract an ``@mode`` prefix from the query."""
    query = query.strip()
    match = _MODE_PATTERN.match(query)
    if not match:
        return None, query

    mode = match.group(1).lower()
    clean = match.group(2).strip()

    if mode not in _MODE_REGISTRY:
        # Unknown mode — treat as normal query (don't swallow the text)
        logger.debug("[modes] Unknown mode @%s — passing through", mode)
        return None, query

    logger.info("[modes] Detected mode: @%s", mode)
    return mode, clean


# ---------------------------------------------------------------------------
# Mode configuration
# ---------------------------------------------------------------------------


@dataclass
class ModeConfig:
    """Configuration for a specific execution mode."""

    name: str
    label: str  # human-readable label for the UI
    profile: str = "agent"  # AI profile to use
    system_prompt: str = ""  # override system prompt (empty = use default)
    tools: list[str] | None = None  # tool subset (None = mode-specific logic)
    max_iterations: int = 10  # agent loop iteration cap
    iter_timeout: int | None = None  # per-iteration timeout (None = default 120s)
    max_tool_output: int | None = None  # truncate tool output (None = default 16k chars)
    description: str = ""  # short help text


# ---------------------------------------------------------------------------
# @search mode
# ---------------------------------------------------------------------------

_SEARCH_SYSTEM_PROMPT = """\
You are a web research assistant with access to real-time web search tools.

YOUR WORKFLOW:
1. ALWAYS start by calling web_search with a well-crafted query based on \
the user's question. Search FIRST, answer SECOND.
2. Review the search results. If they provide enough info, synthesise a \
clear answer citing the sources.
3. If a result looks especially relevant, use web_fetch to get the full \
page content for deeper analysis.
4. You may run multiple searches with refined queries if the first round \
doesn't fully answer the question.
5. When you have enough information, provide a comprehensive answer with \
source URLs.

RULES:
- NEVER answer from memory alone — always search first, even if you think \
you know the answer. The user chose @search mode because they want current, \
verified information.
- Cite sources: include URLs in your answer so the user can verify.
- If the search returns no useful results, say so honestly and offer to \
try different search terms.
- Be thorough but concise — don't dump raw search results, synthesise them.
- You also have web_fetch to read full pages when snippets aren't enough.
"""

_SEARCH_TOOLS = [
    "web_search",
    "web_fetch",
    "read_file",  # in case search results reference local files
    "write_file",  # save research to a file if asked
]


# ---------------------------------------------------------------------------
# @logs mode
# ---------------------------------------------------------------------------

_LOGS_SYSTEM_PROMPT = """\
You are a log analysis assistant. You diagnose issues by reading log files, \
running log commands, and cross-referencing errors with web search and your \
own knowledge.

YOUR WORKFLOW (always follow this sequence):
1. FETCH — get the raw logs using the appropriate tool:
   - File path given → read_file(path)
   - Local service → shell('journalctl -u <svc> --no-pager -n 200') or \
shell('docker logs <container> --tail 200') or shell('tail -n 200 /var/log/...')
   - REMOTE host (e.g., "myserver", "staging") → ssh(host, command). \
Just use the host alias — all SSH keys and options are pre-configured. \
Do NOT pass key paths, identity files, usernames, or IPs. \
Examples: ssh('myserver', 'docker logs --tail 200 worker-1')
   - Ambiguous → check common locations: /var/log/, journalctl, docker logs
2. ANALYZE — ALWAYS pass the raw log output to analyze_logs(logs=<output>). \
This tool extracts errors, warnings, patterns, repeated messages, and a health \
assessment. It makes your diagnosis more accurate and shows the user a visible \
processing step.
3. DIAGNOSE — interpret the analyze_logs report:
   - Explain each issue in plain language
   - Identify root causes from the repeated error patterns
   - Use web_search to look up error messages you're not confident about
   - Check if issues are related (cascade failures)
4. REPORT — present findings as:
   - Summary of what was found (healthy / issues detected / critical)
   - Each issue: error message, frequency, likely cause, proposed fix
   - If no errors: confirm the logs look healthy and note any warnings

RULES:
- Read the ACTUAL logs first — never guess what they might contain.
- When you find errors, ALWAYS explain them in plain language. Don't just \
echo the raw error back to the user.
- Use web_search to look up error messages you're not confident about. \
The user wants accurate diagnosis, not guesses.
- For each issue, propose a concrete fix or next step — not just "investigate \
further".
- If logs are very large, focus on the most recent entries and error patterns. \
Use grep/tail to filter before reading everything.
- If asked to explain a specific log message, give a clear explanation of \
what it means, why it happens, and what (if anything) to do about it.
- Be honest about uncertainty — if you're not sure, say so and suggest where \
to look next.
"""

_LOGS_TOOLS = [
    "shell",  # journalctl, docker logs, tail, grep, etc.
    "ssh",  # remote log access (myserver, staging, etc.)
    "read_file",  # read log files directly
    "analyze_logs",  # structured log parsing (errors, patterns, health)
    "write_file",  # save analysis reports if asked
    "web_search",  # look up unfamiliar errors
    "web_fetch",  # read full docs/solutions for errors
]


# ---------------------------------------------------------------------------
# @discover mode (config only — the actual runner is in discovery.py)
# ---------------------------------------------------------------------------

_DISCOVER_DESCRIPTION = (
    "Multi-phase system investigation: scoping → parallel probing → "
    "synthesis → execution. Best for: disk cleanup, security audits, "
    "system diagnostics."
)


# ---------------------------------------------------------------------------
# Mode registry
# ---------------------------------------------------------------------------

_LOGS_DESCRIPTION = (
    "Log analysis agent: reads log files and commands, diagnoses errors, "
    "cross-references with web search, and proposes fixes."
)

_MODE_REGISTRY: dict[str, ModeConfig] = {
    "search": ModeConfig(
        name="search",
        label="Web Search",
        profile="agent",
        system_prompt=_SEARCH_SYSTEM_PROMPT,
        tools=_SEARCH_TOOLS,
        max_iterations=8,
        description="Search-first agent: always queries the web before answering.",
    ),
    "logs": ModeConfig(
        name="logs",
        label="Log Analyzer",
        profile="log-analyzer",
        system_prompt=_LOGS_SYSTEM_PROMPT,
        tools=_LOGS_TOOLS,
        max_iterations=12,
        iter_timeout=300,
        max_tool_output=12_000,
        description=_LOGS_DESCRIPTION,
    ),
    "log": ModeConfig(
        name="logs",
        label="Log Analyzer",
        profile="log-analyzer",
        system_prompt=_LOGS_SYSTEM_PROMPT,
        tools=_LOGS_TOOLS,
        max_iterations=12,
        iter_timeout=300,
        max_tool_output=12_000,
        description=_LOGS_DESCRIPTION,
    ),
    "discover": ModeConfig(
        name="discover",
        label="Discovery",
        profile="agent",  # overridden in ws_endpoint — uses planner/worker profiles
        system_prompt="",  # discovery has its own prompt system
        tools=None,  # discovery manages its own tool calls
        max_iterations=1,  # not used — discovery has its own loop
        description=_DISCOVER_DESCRIPTION,
    ),
    # General-purpose agent modes — bypass ProfileRouter so queries don't
    # get downgraded to "fast".  No tool subset = all registered tools.
    "tooling": ModeConfig(
        name="tooling",
        label="Tooling Agent",
        profile="agent",
        system_prompt="",  # empty = use default agent system prompt
        tools=None,  # None = all registered tools
        max_iterations=12,
        description="General-purpose tool agent: uses all available tools without profile routing.",
    ),
    "agent": ModeConfig(
        name="agent",
        label="Agent",
        profile="agent",
        system_prompt="",
        tools=None,
        max_iterations=12,
        description="Agent mode: direct agent profile, bypasses ProfileRouter.",
    ),
}


def get_mode_config(mode: str) -> ModeConfig | None:
    """Return configuration for a mode, merging config.yaml overrides.

    The static _MODE_REGISTRY provides defaults (system prompt, tools, etc.)
    while config.yaml can override runtime tunables: profile, max_iterations.
    """
    base = _MODE_REGISTRY.get(mode)
    if base is None:
        return None

    # Merge overrides from config.yaml → modes.<canonical_name>.*
    try:
        from agentforge.config import get_config

        cfg = get_config()
        section = f"modes.{base.name}"  # canonical name (e.g., "logs" not "log")

        profile = cfg.get(f"{section}.profile")
        if profile:
            base = ModeConfig(
                name=base.name,
                label=base.label,
                profile=str(profile),
                system_prompt=base.system_prompt,
                tools=base.tools,
                max_iterations=base.max_iterations,
                iter_timeout=base.iter_timeout,
                max_tool_output=base.max_tool_output,
                description=base.description,
            )
        max_iter = cfg.get(f"{section}.max_iterations")
        if max_iter is not None:
            base = ModeConfig(
                name=base.name,
                label=base.label,
                profile=base.profile,
                system_prompt=base.system_prompt,
                tools=base.tools,
                max_iterations=int(max_iter),
                iter_timeout=base.iter_timeout,
                max_tool_output=base.max_tool_output,
                description=base.description,
            )
    except Exception:
        pass  # fall back to static defaults

    return base


def list_modes() -> list[ModeConfig]:
    """Return all registered modes (for UI display)."""
    return list(_MODE_REGISTRY.values())


def is_mode_enabled(mode: str) -> bool:
    """Check if a mode is registered and its dependencies are available."""
    if mode not in _MODE_REGISTRY:
        return False

    if mode == "search":
        # Check if web search is actually available
        try:
            from agentforge.tools.web_search import is_web_search_available

            return is_web_search_available()
        except ImportError:
            return False

    if mode in ("logs", "log"):
        try:
            from agentforge.config import get_config

            return bool(get_config().get("modes.logs.enabled", True))
        except Exception:
            return True

    if mode == "discover":
        try:
            from agentforge.config import get_config

            return bool(get_config().get("discovery.enabled", True))
        except Exception:
            return True  # default enabled

    return True
