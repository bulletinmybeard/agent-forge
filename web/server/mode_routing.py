"""Mode prefix detection for chat and REST query paths.

Parses @-aliases at the start of a prompt (or anywhere for RAG aliases) and
returns the cleaned query plus the forced mode name.
"""

from __future__ import annotations

import re

from agentforge.mode_prefixes import RAG_SEARCH_ALIASES, RAG_SEARCH_MODE

STICKY_MODES = frozenset(
    (
        "web_search",
        "logs",
        "sql",
        "scheduler",
        "monitor",
        "research",
        "coding",
        "review",
        "plan",
        "build",
        "film",
    )
)

CHAT_ALIASES = {"@chat"}
AGENT_ALIASES = {"@agent"}
SEARCH_ALIASES = RAG_SEARCH_ALIASES
WEB_SEARCH_ALIASES = {"@search"}
LOGS_ALIASES = {"@logs"}
DISCOVER_ALIASES = {"@discover"}
SQL_ALIASES = {"@sql"}
PIPELINE_ALIASES = {"@pipeline"}
SCHEDULER_ALIASES = {"@scheduler"}
MONITOR_ALIASES = {"@monitor"}
REVIEW_ALIASES = {"@review"}
RESEARCH_ALIASES = {"@research"}
CODING_ALIASES = {"@coding", "@code"}
PLAN_ALIASES = {"@plan"}
BUILD_ALIASES = {"@build"}
FILM_ALIASES = {"@film", "@movie", "@movies"}
CONNECTOR_ALIASES = {"@conn", "@connector"}
# RAG aliases that can appear anywhere in the query (not just at the start)
ANYWHERE_ALIASES = RAG_SEARCH_ALIASES

_PREFIX_GROUPS: list[tuple[set[str], str]] = [
    (CHAT_ALIASES, "chat"),
    (SQL_ALIASES, "sql"),
    (AGENT_ALIASES, "agent"),
    (WEB_SEARCH_ALIASES, "web_search"),
    (LOGS_ALIASES, "logs"),
    (SEARCH_ALIASES, RAG_SEARCH_MODE),
    (DISCOVER_ALIASES, "discover"),
    (PIPELINE_ALIASES, "pipeline"),
    (REVIEW_ALIASES, "review"),
    (RESEARCH_ALIASES, "research"),
    (SCHEDULER_ALIASES, "scheduler"),
    (MONITOR_ALIASES, "monitor"),
    (CODING_ALIASES, "coding"),
    (PLAN_ALIASES, "plan"),
    (BUILD_ALIASES, "build"),
    (FILM_ALIASES, "film"),
]


_TEXT_TRANSFORM_RE = re.compile(
    r"\b(?:reformat|rewrite|rephrase|paraphrase|proofread|translate|format)\b"
    r"|\b(?:convert|turn)\b.{0,40}\binto\b",
    re.IGNORECASE,
)
_DISK_REQUEST_RE = re.compile(
    r"\b(?:read|open|edit|write|save|delete|remove|rename|move|copy|run|execute|inspect|cat|stat|grep)\b"
    r"|\b(?:on\s+disk|in\s+place)\b"
    r"|\b(?:the|this|that)\s+file\b",
    re.IGNORECASE,
)
_SUPPLIED_TEXT_RE = re.compile(
    r"\b(?:below|following|pasted|here)\b"
    r"|\b(?:this|the|these)\s+(?:list|text|paths?|lines?|paragraph)\b",
    re.IGNORECASE,
)
_PATH_TOKEN_RE = re.compile(r"(?:~/|/Users/|/home/|/opt/|/var/|/tmp/|/etc/)[^\s`\"')\]]+")


def _instruction_preamble(query: str) -> str:
    """Text before the pasted body. A blank line splits the two."""
    return re.split(r"\n\s*\n", query, maxsplit=1)[0]


def instruction_for_routing(query: str) -> str:
    """The request the router should classify.

    Text under a blank line is pasted data (a note, a list, a log). Mode
    regexes and ``classify_intent`` both use this, so words in the paste
    do not pick the mode.
    """
    preamble = _instruction_preamble(query or "").strip()
    return preamble or (query or "").strip()


def is_supplied_text_transform(query: str) -> bool:
    """True when the message already contains the text to rewrite.

    Disk verbs and the transform request are judged on the preamble only.
    The pasted body may say "run" or "cron job" without turning the prompt
    into a filesystem or scheduler task. A verb in the preamble that reads
    or changes a file stays with the normal classifier.
    """
    if not query:
        return False
    preamble = _instruction_preamble(query)
    if not _TEXT_TRANSFORM_RE.search(preamble):
        return False
    if _DISK_REQUEST_RE.search(preamble):
        return False
    if len(_PATH_TOKEN_RE.findall(query)) >= 2:
        return True
    return _SUPPLIED_TEXT_RE.search(query) is not None


_MD_LINK_REQUEST_RE = re.compile(r"markdown\s+links?", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
_GROUPED_FILES_RE = re.compile(r"^(?P<directory>(?:~/|/).+?/)\s*\((?P<files>[^)]+)\)\s*$")
_SKIP_LABEL_PARTS = frozenset({".claude", ".local", "docs", "www", "projects", "Users"})


def markdown_path_links(query: str) -> str | None:
    """Turn a pasted path list into Markdown links, or None if that is not the ask.

    The label is the project directory (the segment after ``projects/``).
    A trailing ``(a.md, b.md)`` on one bullet becomes one link per file.
    Paths are copied as written, including a leading ``~/``.
    """
    if not is_supplied_text_transform(query) or not _MD_LINK_REQUEST_RE.search(query):
        return None
    lines: list[str] = []
    for raw in query.splitlines():
        item = _LIST_ITEM_RE.match(raw)
        if not item:
            continue
        body = item.group(1).strip()
        if len(body) >= 2 and body[0] == "`" and body[-1] == "`":
            body = body[1:-1].strip()
        grouped = _GROUPED_FILES_RE.match(body)
        if grouped:
            directory = grouped.group("directory")
            files = [part.strip().strip("`") for part in grouped.group("files").split(",")]
            paths = [directory + name for name in files if name]
            grouped_files = True
        elif body.startswith("~/") or body.startswith("/"):
            paths = [body]
            grouped_files = False
        else:
            continue
        for path in paths:
            lines.append(f"- [{_project_label(path, grouped=grouped_files)}]({path})")
    if len(lines) < 2:
        return None
    return "```markdown\n" + "\n".join(lines) + "\n```"


def _project_label(path: str, *, grouped: bool) -> str:
    parts = [part for part in path.split("/") if part]
    filename = ""
    if parts and "." in parts[-1]:
        filename = parts[-1].rsplit(".", 1)[0]
        parts = parts[:-1]
    project = ""
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts):
            project = parts[idx + 1]
    if not project:
        for part in reversed(parts):
            if part not in _SKIP_LABEL_PARTS and not part.startswith("."):
                project = part
                break
    if grouped and filename:
        return f"{project}/{filename}" if project else filename
    return project or filename or path


def strip_mode_prefix(query: str) -> tuple[str, str | None]:
    """Detect mode aliases in the query and strip them.

    Start-of-query aliases (@agent, @search, @logs, etc.) are checked first.
    Anywhere aliases (@qdrant / @docs / @find) can appear at any position.

    Returns (cleaned_query, forced_mode).
    forced_mode is "chat", "agent", "search", "web_search", "logs", "discover",
    "review", or None.
    """
    stripped = query.lstrip()
    lower = stripped.lower()

    for aliases, mode in _PREFIX_GROUPS:
        for alias in aliases:
            if lower.startswith(alias):
                rest = stripped[len(alias) :].lstrip()
                return rest, mode

    for alias in sorted(ANYWHERE_ALIASES, key=len, reverse=True):
        if alias in lower:
            idx = lower.index(alias)
            rest = (stripped[:idx] + stripped[idx + len(alias) :]).strip()
            return rest, RAG_SEARCH_MODE

    return query, None


# Dedicated clients that always mean one custom agent. The email app tags
# the session ``source=afemail`` but the installed build does not put
# ``@afemail`` on the question, so the generic agent runs and mail tools
# are rejected.
_CLIENT_SOURCE_MODES: dict[str, str] = {
    "afemail": "custom:afemail",
}


def pinned_client_mode(
    source: str | None,
    forced_mode: str | None,
    custom_mode: str | None,
    query: str,
) -> str | None:
    """Mode a dedicated client always uses, or None to keep classification.

    An explicit built-in prefix (``@search``, ``@agent``, ...) or a different
    custom alias wins. A leading ``@`` that matched nothing stays unclassified
    so the unknown-prefix error card still fires.
    """
    pinned = _CLIENT_SOURCE_MODES.get((source or "").strip().lower())
    if not pinned:
        return None
    if forced_mode and forced_mode != pinned:
        return None
    if custom_mode and custom_mode != pinned:
        return None
    if query.lstrip().startswith("@") and not custom_mode and not forced_mode:
        return None
    return pinned
