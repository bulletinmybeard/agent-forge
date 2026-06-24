"""WebSocket endpoint — hybrid search + agent mode.

Supports TWO execution modes, selected per-query by a lightweight classifier:

1. **Search mode** (default): Qdrant-based RAG pipeline
       query → refine → embed → search → score-gate → re-rank → LLM answer

2. **Agent mode**: AgentLoop with system tools (SSH, Docker, file ops, etc.)
       query → route → AgentLoop(tools) → result

Session persistence via SQLite — every WS event is persisted so the
frontend can reconstruct the exact same UI on page reload.

The client may connect with ``?session_id=<uuid>`` to resume an existing
session, or omit it to start fresh (the session is created on first query).
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import pathlib
import platform
import queue as _queue
import re
import re as _re
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import date as _date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from agentforge.config import set_request_provider_override, set_request_role_override_map, set_request_session_id
from agentforge.connectors._account import account_slug, label_slug
from app.models.knowledge import KnowledgeSearchRequest
from app.services.knowledge_service import knowledge_service

from . import protocol
from .agent_bridge import AgentBridge
from .confirm import ConfirmationBroker
from .database import ChatDatabase
from .queue.models import JobStatus
from .queue.store import job_store
from .secret import SecretBroker

if TYPE_CHECKING:
    import ollama

logger = logging.getLogger(__name__)

# Framework config path (relative to this file's parent directory)
_fw_config_path = Path(__file__).resolve().parents[2] / "framework-config.yaml"

# Prompts ship inside the agentforge package; resolve from its install location
# so this works whether agentforge is pip-installed or run from a source checkout.
import agentforge as _af_pkg

_PROMPTS_DIR = Path(_af_pkg.__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt from the framework prompts directory."""
    return (_PROMPTS_DIR / f"{name}.md").read_text()


def _strip_wrapping_fence(text: str) -> str:
    """Strip an outer ```lang ... ``` wrapper that some LLMs add around markdown output.

    Cloud models often respond to "write in markdown" by wrapping the entire
    response in a ```markdown ... ``` fence, which then renders as a single
    monospace code block in the UI instead of as rendered markdown.

    Only strips when the whole text is a single fenced block (exactly two
    fence markers, at the very start and very end). Legitimate embedded code
    blocks are left alone.
    """
    if not text:
        return text
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.split("\n")
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return text
    fence_lines = sum(1 for ln in lines if ln.strip().startswith("```"))
    if fence_lines != 2:
        return text
    return "\n".join(lines[1:-1]).strip()


# ---------------------------------------------------------------------------
# Tool-call extraction & post-write hook wiring (shared by all runners)
# ---------------------------------------------------------------------------

# Tools whose results should trigger the post-write verification hook.
# Must match _WRITE_TOOLS in web/server/_hooks.py.
_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "code_edit",
        "write_file",
        "revert_file",
        "revert_lines",
    }
)


def _redact_text(text: str) -> str:
    """Apply secret redaction to text before sending to the client.

    Returns the original text unchanged if the redactor is disabled or
    unavailable.  Never raises.
    """
    try:
        from agentforge.secret_redactor import get_redactor

        return get_redactor().redact(text).text
    except Exception:
        return text


def _redact_tool_results(tool_calls: list[dict]) -> None:
    """Redact secrets in tool result text **in-place**.

    Call AFTER diff/compare emit functions (which need raw results to
    parse unified diffs and verified-write headers) but BEFORE the
    results are persisted to the DB or sent to the client.
    """
    for tc in tool_calls:
        raw = tc.get("result")
        if raw and isinstance(raw, str):
            tc["result"] = _redact_text(raw)


def _extract_tool_calls_with_results(iterations: list) -> list[dict]:
    """Flatten an AgentLoop iteration log into a list of call+result dicts.

    Each entry is ``{"name", "args", "result"}`` where ``result`` is the
    tool's output text (empty string if the framework did not record one).
    The post-write hook uses ``result`` to parse the verified-write header.
    """
    out: list[dict] = []
    for it in iterations:
        _calls = it.tool_calls or []
        _results = it.tool_results or []
        _results_by_idx = {i: r for i, r in enumerate(_results)}
        for idx, tc in enumerate(_calls):
            entry = {
                "name": tc["name"],
                "args": tc.get("arguments", tc.get("args", {})),
            }
            res = _results_by_idx.get(idx)
            if res is not None:
                entry["result"] = res.get("result", "")
            out.append(entry)
    return out


def _parse_verified_write(result_text: str) -> dict | None:
    """Parse a ``✓ VERIFIED ...`` result into a dict with the diff payload.

    Returns a dict with keys ``path``, ``pre_hash``, ``post_hash``,
    ``snapshot_id``, ``additions``, ``deletions``, ``diff_text``, and
    ``action`` — or ``None`` if the result text is not a verified-write
    payload.

    The verified-write format emitted by ``code_edit`` / ``revert_file`` is::

        ✓ VERIFIED <filename> {updated|reverted from snapshot …} (+A -D lines)
        pre_hash=<sha256>
        post_hash=<sha256>
        path=<absolute path>
        snapshot_id=<sha256 of pre-state>   (optional)
        snapshot_saved_at=<ISO 8601>        (optional)
        (To undo: revert_file(...))         (optional)
        \n
        --- a/<filename>                    (unified diff)
        +++ b/<filename>
        @@ -1,N +1,M @@
        ...

    We split header (first N lines of ``key=value``) from the unified diff
    (everything from the first ``---`` line onwards) and count additions /
    deletions from the diff lines directly so the client doesn't have to
    re-derive them from the header.
    """
    if not result_text or "✓ VERIFIED" not in result_text:
        return None

    lines = result_text.splitlines()
    header: dict[str, str] = {}
    diff_start = None
    for i, line in enumerate(lines):
        # The unified diff begins with a "--- a/…" / "--- …" marker.  We
        # anchor on the two-character prefix rather than "--- a/" because
        # the ``_unified_diff`` helper uses a plain filename, not a/ b/.
        if line.startswith("--- ") and diff_start is None:
            diff_start = i
            break
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            if k in {"path", "pre_hash", "post_hash", "snapshot_id", "snapshot_saved_at"}:
                header[k] = v.strip()

    if "path" not in header or "pre_hash" not in header or "post_hash" not in header:
        return None

    diff_text = "\n".join(lines[diff_start:]) if diff_start is not None else ""
    additions = sum(1 for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    deletions = sum(1 for ln in diff_text.splitlines() if ln.startswith("-") and not ln.startswith("---"))

    # Derive a short action verb from the ✓ VERIFIED header line so the UI
    # can pick an accent colour / verb ("edited" vs "reverted" vs "written").
    first = lines[0] if lines else ""
    if "reverted" in first:
        action = "reverted"
    elif "written" in first or "created" in first:
        action = "written"
    else:
        action = "edited"

    return {
        "path": header["path"],
        "pre_hash": header["pre_hash"],
        "post_hash": header["post_hash"],
        "snapshot_id": header.get("snapshot_id", ""),
        "additions": additions,
        "deletions": deletions,
        "diff_text": diff_text,
        "action": action,
    }


async def _fire_post_write_hooks(
    session_id: str,
    all_tool_calls: list[dict],
    *,
    mode: str,
    model: str,
) -> None:
    """Fire hooks_post_write for every write-class tool call in the list.

    Safe to call on any runner — if the list contains no write tools, this
    is a no-op.  Failures inside the hook itself are swallowed by the hook.
    """
    from ._hooks import hooks_post_write

    for _tc in all_tool_calls:
        if _tc.get("name", "") in _WRITE_TOOL_NAMES:
            await hooks_post_write(
                session_id=session_id,
                tool_name=_tc["name"],
                args=_tc.get("args", {}) or {},
                result_text=_tc.get("result", "") or "",
                mode=mode,
                model=model,
            )


async def _emit_file_diff_events(
    all_tool_calls: list[dict],
    send_and_persist,
) -> None:
    """Emit a ``file.diff`` WebSocket event for every verified write.

    Scans *all_tool_calls* for entries whose name is in ``_WRITE_TOOL_NAMES``
    and whose result text contains a ``✓ VERIFIED`` header.  For each match,
    parses the unified diff out of the tail of the result and fires a
    ``file.diff`` message via *send_and_persist* — the per-runner closure
    that handles both ``ws.send_json`` and SQLite chat-history persistence.

    This is intentionally separate from :func:`_fire_post_write_hooks`
    (which handles audit logging and the verified-write receipt in the
    result store) so it can be called BEFORE the final ``agent.result``
    / ``agent.summary`` messages, placing the diff card above them in
    the rendered chat UI.

    Failures are swallowed at DEBUG level — emitting a UI nicety must
    never abort a successful run.
    """
    if not all_tool_calls or send_and_persist is None:
        return
    for _tc in all_tool_calls:
        _name = _tc.get("name", "")
        if _name not in _WRITE_TOOL_NAMES:
            continue
        parsed = _parse_verified_write(_tc.get("result", "") or "")
        if parsed is None:
            continue
        try:
            diff_msg = protocol.file_diff(
                tool=_name,
                path=parsed["path"],
                pre_hash=parsed["pre_hash"],
                post_hash=parsed["post_hash"],
                additions=parsed["additions"],
                deletions=parsed["deletions"],
                diff_text=_redact_text(parsed["diff_text"]),
                snapshot_id=parsed["snapshot_id"],
                action=parsed["action"],
            )
            await send_and_persist(diff_msg, msg_type="file_diff")
        except Exception:
            logger.debug("file.diff emit failed", exc_info=True)


# --- Regex helpers for parsing diff_files output ---
_DIFF_HEADER_RE = re.compile(
    r"^Diff:\s+(\S+)\s+vs\s+(\S+)",
    re.MULTILINE,
)
_DIFF_STATS_RE = re.compile(
    r"Stats:\s*(\d+)\s+additions?,\s*(\d+)\s+deletions?",
)


def _parse_diff_files_result(result: str) -> dict | None:
    """Parse a diff_files tool result and extract unified-diff info.

    Returns a dict with keys expected by ``protocol.file_diff()`` or
    ``None`` if the result doesn't contain a parseable unified diff.
    """
    if not result or "Error:" in result[:20]:
        return None

    # Must contain unified diff markers to be renderable
    if "\n--- " not in result and not result.startswith("--- "):
        return None

    # Extract file names from header line ("Diff: a.yaml vs b.yaml")
    header_m = _DIFF_HEADER_RE.search(result)
    file_a = header_m.group(1) if header_m else ""
    file_b = header_m.group(2) if header_m else ""

    # Extract stats
    stats_m = _DIFF_STATS_RE.search(result)
    additions = int(stats_m.group(1)) if stats_m else 0
    deletions = int(stats_m.group(2)) if stats_m else 0

    # Extract the unified diff body (everything from the first "--- " line)
    idx = result.find("\n--- ")
    if idx < 0 and result.startswith("--- "):
        idx = -1  # starts at position 0
    diff_text = result[idx + 1 :] if idx >= 0 else result

    return {
        "path": f"{file_a} vs {file_b}" if file_a else "(comparison)",
        "additions": additions,
        "deletions": deletions,
        "diff_text": diff_text.rstrip(),
    }


def _generate_unified_diff(file_a: str, file_b: str, context_lines: int = 3) -> dict | None:
    """Generate a unified diff directly from two file paths.

    Used as a fallback when ``diff_files`` produced a semantic diff
    (YAML/JSON/CSV deepdiff) instead of a unified text diff.  Returns
    a dict compatible with ``protocol.file_diff()`` or ``None`` on error.
    """

    try:
        path_a = Path(file_a).expanduser().resolve()
        path_b = Path(file_b).expanduser().resolve()
        if not path_a.is_file() or not path_b.is_file():
            return None

        text_a = path_a.read_text(errors="replace").splitlines(keepends=True)
        text_b = path_b.read_text(errors="replace").splitlines(keepends=True)

        diff_lines = list(
            difflib.unified_diff(
                text_a,
                text_b,
                fromfile=str(path_a),
                tofile=str(path_b),
                n=context_lines,
            )
        )

        if not diff_lines:
            return None  # files are identical

        additions = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        deletions = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
        diff_text = "".join(diff_lines).rstrip()

        return {
            "path": f"{path_a.name} vs {path_b.name}",
            "additions": additions,
            "deletions": deletions,
            "diff_text": diff_text,
        }
    except Exception:
        logger.debug("_generate_unified_diff failed", exc_info=True)
        return None


async def _emit_file_compare_events(
    all_tool_calls: list[dict],
    send_and_persist,
) -> None:
    """Emit ``file.diff`` events for ``diff_files`` tool calls.

    Renders file comparisons using the same diff UI card as verified writes,
    with a distinct "compared" action style.

    Two strategies:
    1. Parse the unified diff from the tool result (text format).
    2. If the result is semantic (YAML/JSON deepdiff — no unified markers),
       generate a unified diff directly from the file paths in the tool args.
    """
    if not all_tool_calls or send_and_persist is None:
        return
    for _tc in all_tool_calls:
        if _tc.get("name") != "diff_files":
            continue

        parsed = _parse_diff_files_result(_tc.get("result", "") or "")

        # Fallback: generate unified diff from file paths when result
        # is semantic (YAML/JSON/CSV deepdiff — no unified markers).
        if parsed is None:
            args = _tc.get("args") or _tc.get("arguments") or {}
            file_a = args.get("file_a", "")
            file_b = args.get("file_b", "")
            if file_a and file_b:
                parsed = _generate_unified_diff(file_a, file_b)

        if parsed is None:
            continue

        try:
            diff_msg = protocol.file_diff(
                tool="diff_files",
                path=parsed["path"],
                pre_hash="",
                post_hash="",
                additions=parsed["additions"],
                deletions=parsed["deletions"],
                diff_text=_redact_text(parsed["diff_text"]),
                snapshot_id="",
                action="compared",
            )
            await send_and_persist(diff_msg, msg_type="file_diff")
        except Exception:
            logger.debug("file.diff (compare) emit failed", exc_info=True)


router = APIRouter()


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds into a human-friendly string.

    <60s  → "12.3s"
    <3600 → "2m 7.8s"
    else  → "1h 3m 12s"
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = seconds % 60
        return f"{m}m {s:.1f}s" if s >= 0.05 else f"{m}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(round(seconds % 60))
    parts = [f"{h}h"]
    if m:
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Location context helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared state — initialised at app startup (see app.py lifespan)
# ---------------------------------------------------------------------------

_runtime: SearchRuntime | None = None
_db: ChatDatabase | None = None
_canvas_db = None  # CanvasDatabase | None — injected from app.py

# Signalled (from the background init thread) once SearchRuntime is ready.
# WebSocket connections wait on this before accessing _runtime.
_runtime_ready = asyncio.Event()


def set_database(db: ChatDatabase) -> None:
    """Set the shared database reference (called from app.py lifespan)."""
    global _db
    _db = db


def set_canvas_database(canvas_db) -> None:
    """Set the canvas database reference (called from app.py lifespan)."""
    global _canvas_db
    _canvas_db = canvas_db


def is_canvas_enabled() -> bool:
    """Whether the Canvas feature is available (canvas DB initialised)."""
    return _canvas_db is not None


def get_db() -> ChatDatabase:
    if _db is None:
        raise RuntimeError("Database not initialised")
    return _db


class SearchRuntime:
    """Holds shared objects for BOTH the search pipeline AND the agent loop.

    Created once at startup, used by every WebSocket connection.
    Combines agentforge's Qdrant search with py-mini-ai-framework's
    AgentLoop + ToolRegistry for hybrid search+agent mode.
    """

    # @source / #type shorthands are loaded per-deployment from config.yaml
    # (search.source_aliases / search.source_type_aliases) in __init__ — keeps
    # deployer-specific source names out of the published codebase.
    SOURCE_ALIASES: dict[str, str | list[str]] = {}
    SOURCE_TYPE_ALIASES: dict[str, str] = {}

    def __init__(self) -> None:
        # --- AgentForge imports (search pipeline) ----------------------
        service_root = Path(__file__).resolve().parent.parent.parent
        if str(service_root) not in sys.path:
            sys.path.insert(0, str(service_root))

        from app.config import settings as af_settings

        self.af_settings = af_settings

        # @source / #type shorthands — per-deployment, loaded from config.yaml.
        self.SOURCE_ALIASES = dict(af_settings.search.source_aliases)
        self.SOURCE_TYPE_ALIASES = dict(af_settings.search.source_type_aliases)

        # Build selectable profile list from framework-config.yaml (abstract
        # model profiles are filtered out by list_selectable_profiles()).
        profiles = _get_profiles_from_yaml()
        self.profiles = profiles or ["cloud-light", "cloud-heavy", "cloud-coder"]

        # The system prompt used for title generation (lightweight)
        self.system_prompt = (
            "You are AgentForge, an AI knowledge concierge. You answer questions "
            "about internal APIs, documentation, code, and databases using "
            "semantic search over indexed knowledge."
        )

        # Known sources/documents cache — loaded once at startup
        self.known_sources: dict[str, dict[str, str]] = {}
        self.known_documents: dict[str, str] = {}
        self._load_knowledge_cache()

        # --- py-mini-ai-framework imports (agent + tools) -----------------
        self.agent_available = False
        self.registry = None
        self.tool_count = 0
        self.agent_system_prompt = ""
        self.agent_profiles: dict[str, list[str]] = {}  # profile → tool subset

        self._init_agent_tools()

        # Raw config.yaml for dynamic prompt injection in custom agents
        _config_yaml_path = service_root / "config.yaml"
        if _config_yaml_path.exists():
            with open(_config_yaml_path) as _cf:
                self._raw_config: dict = yaml.safe_load(_cf) or {}
        else:
            self._raw_config = {}

        # Custom agents loaded from custom_agents.yaml
        # alias (lowercase) → full agent config dict (includes "id" and "prompt_text")
        self.custom_agents: dict[str, dict] = {}
        self._load_custom_agents()

        # Skills loaded from skills.yaml — domain-specific instruction sets
        # alias (lowercase) → skill config dict; id → skill config dict
        self.skills: dict[str, dict] = {}  # alias → config
        self.skills_by_id: dict[str, dict] = {}  # id → config
        self.skills_max: int = 3  # max skills per query
        self._load_skills()

        # User context: personal workspace info injected into all agent prompts
        self.user_context: str = ""
        self._load_user_context()

        logger.info(
            "SearchRuntime ready — profiles: %s, %d sources, %d documents, "
            "agent: %s (%d tools), custom agents: %d, skills: %d",
            self.profiles,
            len(set(v["source_name"] for v in self.known_sources.values())),
            len(set(self.known_documents.values())),
            "enabled" if self.agent_available else "disabled",
            self.tool_count,
            len({cfg["id"] for cfg in self.custom_agents.values()}),
            len(self.skills_by_id),
        )

    def _init_agent_tools(self) -> None:
        """Initialise framework's ToolRegistry and ProfileRouter."""
        try:
            # Initialise framework config (must happen before any tool imports)
            from agentforge.config import get_config, reset_config

            reset_config()
            get_config(_fw_config_path)

            from agentforge.tools import ToolRegistry, register_all_tools
            from agentforge.tools.system import get_system_context

            self.registry = ToolRegistry()
            self.tool_count = register_all_tools(self.registry)

            # System prompt with context
            sys_ctx = get_system_context()
            tool_hints = self.registry.get_model_hints()
            template = _load_prompt("agent")
            self.agent_system_prompt = template.format(
                sys_ctx_summary=sys_ctx["summary"],
                tool_hints=tool_hints,
            )

            # Condensed variant for iteration 2+ (strips BSD warnings,
            # protected-path rules, per-tool hints, sibling projects).
            condensed_template = _load_prompt("agent_condensed")
            self.agent_system_prompt_condensed = condensed_template.format(
                sys_ctx_summary=sys_ctx["condensed_summary"],
                tool_hints=self.registry.get_model_hints_condensed(),
            )

            # Tool subsets per profile.
            # Dedicated tools have better hints, structured params, and error
            # handling than shell("some command …").  shell() is kept as a
            # universal escape hatch (CommandGuard-protected).
            #
            # Base (19 tools) — every profile gets these
            _base_tools = [
                # core
                "shell",  # universal escape hatch (guard-protected)
                "read_file",  # structured file reading + PDF
                "write_file",  # reliable multi-line file creation
                "find_files",  # glob search (better than shell find)
                "grep_text",  # content search (better than shell grep)
                "download_file",  # URL download (better than shell curl)
                # remote
                "ssh",  # remote commands (host-allowlisted)
                "health_check",  # composite remote health check
                # web
                "web_search",  # internet search
                "web_fetch",  # fetch web page content (static/raw HTML)
                "web_fetch_rendered",  # headless Chromium — SPAs, JS-rendered pages
                "web_screengrab",  # headless Chromium — on-demand full-page screenshot
                # media — image/video manipulation
                "video_convert",  # ffmpeg — format conversion, trim, GIF
                "image_convert",  # ImageMagick — format conversion
                "image_resize",  # ImageMagick — resize by dimensions/%
                "image_optimize",  # ImageMagick — web-ready compression
                "image_metadata",  # ImageMagick — EXIF + properties
                "generate_icons",  # ImageMagick — favicon/app icon set from source image
                # media — audio
                "ardour_extract_ranges",  # Ardour DAW range marker extraction
                "audio_concat",  # concatenate audio files with silence gaps
                # media — download
                "ytdlp_download",  # yt-dlp — download video/audio
                "ytdlp_info",  # yt-dlp — video metadata
                "ytdlp_list_formats",  # yt-dlp — available formats
                # logs
                "analyze_logs",  # structured log analysis
                # data
                "diff_files",  # multi-format file comparison
                # notification
                "notify",  # macOS system notification via terminal-notifier
                "notify_list",  # list delivered notifications by group
                "notify_remove",  # dismiss/clear notifications by group
            ]
            # Extended — agent/thinker get these on top
            _extended_tools = [
                # dev
                "git_status",  # working tree status
                "git_log",  # commit history
                "git_diff",  # uncommitted changes
                "gh_command",  # GitHub CLI operations
                "code_edit",  # AI-powered file editing
                # ops
                "docker_ps",  # list containers
                "docker_logs",  # container log output
                "docker_compose_status",  # compose service status
                # data
                "jq_query",  # JSON file querying
                "yq_query",  # YAML/TOML/XML querying
                # files
                "archive_create",  # create tar/zip archives
                "archive_extract",  # extract archives
                # infrastructure
                "qdrant_admin",  # Qdrant vector DB inspection
                "redis_inspect",  # Redis state inspection
                # testing
                "test_runner",  # pytest / jest / vitest runner with analysis
                "k6_load_test",  # k6 HTTP load testing
                # Optional read-only tools blended into @agent so it can mix
                # them with local FS writes. Deployer-specific, from config
                # (hashtag_routes.blend_tools) — empty in the public build.
                *_hr_settings.hashtag_routes.blend_tools,
            ]

            _base = list(_base_tools)
            _full = _base + list(_extended_tools)
            # Pipeline profile: structured tools only — shell is intentionally
            # excluded so the model uses read_file/grep_text/execute_sql instead
            # of shell("grep …") / shell("mysql …").  execute_sql is added so
            # the agent can run queries against named databases without falling
            # back to raw shell mysql commands.
            _pipeline = (
                [t for t in _full if t != "shell"]
                # file_info: needed for mtime-based filtering (e.g., "files older than 60 days")
                # without it the model falls back to shell(find … stat …)
                + ["file_info", "execute_sql", "save_result", "load_result", "search_knowledge_base"]
            )
            # Browser-extension profile: the full agent set minus shell, so a
            # web-page-facing agent can't fall back to shell() for
            # downloads — it must invoke download_file / web_fetch as structured
            # tool calls. Same shell-exclusion rationale as _pipeline.
            _browser = [t for t in _full if t != "shell"]
            self.agent_profiles = {
                "fast": _base,  # 28 tools — quick tasks
                "default": _base,  # 28 tools — general tasks
                "agent": _full,  # 48 tools — multi-step tasks
                "thinker": _full,  # 48 tools — complex analysis
                "vision": _base,  # 28 tools — image analysis
                "pipeline": _pipeline,  # 52 tools — structured (no shell) + file_info + git_show + execute_sql + cache + RAG
                "browser": _browser,  # agent set minus shell (browser-extension runs)
            }

            # ── Register search_knowledge_base ────────────────────────────
            # Defined as a closure so it can call the same _smart_search_pipeline
            # used by _run_search, with access to rt (self) for alias resolution.
            self.registry.register(
                _make_search_kb_tool(self),
                name="search_knowledge_base",
                category="Pipeline",
            )
            self.tool_count += 1

            # Register kb_search (the KnowledgeBase app's own collection)
            # Separate from search_knowledge_base, which hits the main RAG
            # collection. This one queries knowledge_entries via knowledge_service
            # and supports parent_id scoping (one document + its attachments).
            async def kb_search(query: str, parent_id: str = "") -> str:
                """Search the user's personal Knowledge Base -- their saved notes, documentation, code snippets, man pages, and reference docs."""

                req = KnowledgeSearchRequest(query=query, limit=8, parent_id=parent_id or None)
                try:
                    data = await asyncio.to_thread(knowledge_service.search, req)
                except Exception as exc:  # noqa: BLE001
                    return f"kb_search failed: {exc}"

                results = data.get("results", [])
                if not results:
                    scope = f" within document {parent_id}" if parent_id else ""
                    return f"No Knowledge Base results for {query!r}{scope}."

                lines = [f"Found {len(results)} Knowledge Base result(s) for {query!r}:", ""]
                for i, r in enumerate(results, 1):
                    content = (r.get("content") or "").strip()[:800]
                    lines.append(f"{i}. [{r.get('content_type', '')}] {r.get('title', '')} (id={r.get('id', '')})")
                    if r.get("tags"):
                        lines.append(f"   tags: {', '.join(r['tags'])}")
                    if content:
                        lines.append(f"   {content}")
                    lines.append("")
                return "\n".join(lines).rstrip()

            self.registry.register(kb_search, name="kb_search", category="Knowledge")
            self.tool_count += 1

            self.agent_available = True
            logger.info(
                "Agent tools initialised — %d tools registered",
                self.tool_count,
            )

        except Exception:
            logger.warning(
                "Could not initialise agent tools — agent mode disabled",
                exc_info=True,
            )

    def _load_knowledge_cache(self) -> None:
        """Cache known sources and documents for @source parsing and auto-detection."""
        try:
            from app.services.indexer_service import indexer_service

            # --- Sources ---
            raw_sources = indexer_service.discover_sources()
            logger.info("Knowledge cache: discovered %d sources from indexer", len(raw_sources))
            for src in raw_sources:
                name = src.get("source_name", src.get("api_name", ""))
                if not name:
                    continue
                info = {
                    "source_name": name,
                    "source_type": src.get("source_type", ""),
                }
                name_lower = name.lower()
                self.known_sources[name_lower] = info
                if name_lower.endswith("-db"):
                    self.known_sources[name_lower[:-3]] = info
                if "-" in name_lower:
                    self.known_sources[name_lower.replace("-", " ")] = info
                    self.known_sources[name_lower.replace("-", "")] = info

            for alias, canonical in self.SOURCE_ALIASES.items():
                # canonical can be a string or a list of strings (multi-source aliases)
                canonicals = canonical if isinstance(canonical, list) else [canonical]
                for c in canonicals:
                    c_lower = c.lower()
                    if c_lower in self.known_sources:
                        self.known_sources[alias.lower()] = self.known_sources[c_lower]
                        break  # use the first match for the alias lookup cache

            # --- Documents ---
            from app.config import settings as af_settings

            stoplist = set()
            if hasattr(af_settings.chunking, "document_lookup_stoplist"):
                stoplist = {s.lower() for s in af_settings.chunking.document_lookup_stoplist}

            for doc in indexer_service.discover_documents():
                doc_name = doc.get("document_name", "")
                if not doc_name:
                    continue
                project = doc_name
                for suffix in ("_CHANGELOG", "_CHANGES", "_HISTORY"):
                    if project.upper().endswith(suffix):
                        project = project[: -len(suffix)]
                        break
                project_lower = project.lower()
                if project_lower in stoplist:
                    continue
                self.known_documents[project_lower] = doc_name
                collapsed = project_lower.replace("-", "").replace("_", "")
                self.known_documents[collapsed] = doc_name
                if "-" in project_lower:
                    self.known_documents[project_lower.replace("-", " ")] = doc_name
                if "_" in project_lower:
                    self.known_documents[project_lower.replace("_", "-")] = doc_name
                self.known_documents[doc_name.lower()] = doc_name

        except Exception:
            logger.warning("Could not load knowledge cache — auto-detect disabled", exc_info=True)

    def _load_custom_agents(self) -> None:
        """Load user-defined custom agents from custom_agents.yaml.

        Builds a mapping of lowercase alias → agent config dict (augmented with
        ``id`` and ``prompt_text`` fields) so that the mode classifier and runner
        can look up agents in O(1) by their trigger alias.
        """
        service_root = Path(__file__).resolve().parent.parent.parent
        config_path = service_root / "custom_agents.yaml"

        if not config_path.exists():
            logger.info("custom_agents.yaml not found — custom agents disabled")
            return

        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

            agents: dict = dict(cfg.get("agents", {}))

            # Private overlay (gitignored) — keeps personal/deployment-specific
            # agents (e.g., @cloud) out of the published repo. Merged on top.
            local_path = service_root / "custom_agents.local.yaml"
            if local_path.exists():
                try:
                    with open(local_path) as lf:
                        local_agents = (yaml.safe_load(lf) or {}).get("agents", {})
                    agents.update(local_agents)
                    logger.info("Merged %d agent(s) from custom_agents.local.yaml", len(local_agents))
                except Exception:
                    logger.warning("Failed to load custom_agents.local.yaml", exc_info=True)

            loaded_ids: list[str] = []

            for agent_id, agent_cfg in agents.items():
                if not isinstance(agent_cfg, dict):
                    continue

                # Agents with no_history opt out of cross-session memory. Done
                # here (not in memory_policy) so private agents stay out of code.
                if agent_cfg.get("no_history"):
                    from web.server.memory_policy import MemoryTier, register_mode_tier

                    register_mode_tier(f"custom:{agent_id}", MemoryTier.NONE)

                # Resolve system prompt: file path or inline string
                prompt_raw = agent_cfg.get("system_prompt", "")
                prompt_text = ""
                if prompt_raw:
                    # Treat as file path if it looks like one (no newlines, non-empty)
                    is_path = isinstance(prompt_raw, str) and "\n" not in prompt_raw.strip()
                    if is_path:
                        prompt_path = service_root / prompt_raw.strip()
                        if prompt_path.exists():
                            prompt_text = prompt_path.read_text()
                        else:
                            logger.warning(
                                "Custom agent '%s': prompt file not found: %s",
                                agent_id,
                                prompt_path,
                            )
                            prompt_text = prompt_raw
                    else:
                        prompt_text = prompt_raw

                # ── Dynamic prompt injection based on config flags ──────
                # Agents can declare ``config_inject`` in custom_agents.yaml
                # to have their system prompt automatically adapted at load
                # time based on values in config.yaml.
                #
                # Format:
                #   config_inject:
                #     <config_dotpath>:         # e.g., "gitlab.read_write"
                #       "true":  "..."          # text injected when truthy
                #       "false": "..."          # text injected when falsy
                #
                # The injected text is prepended before the prompt.
                inject_cfg = agent_cfg.get("config_inject", {})
                if inject_cfg and isinstance(inject_cfg, dict):
                    preamble_parts: list[str] = []
                    for dotpath, variants in inject_cfg.items():
                        if not isinstance(variants, dict):
                            continue
                        # Resolve dotpath (e.g., "gitlab.read_write") in main config
                        keys = dotpath.split(".")
                        node = self._raw_config
                        for k in keys:
                            if isinstance(node, dict):
                                node = node.get(k)
                            else:
                                node = None
                                break
                        # Also check env-var override: GITLAB_READ_WRITE etc.
                        env_key = dotpath.upper().replace(".", "_")
                        env_val = os.environ.get(env_key, "")
                        if env_val:
                            is_truthy = env_val.lower() in ("1", "true", "yes")
                        else:
                            is_truthy = bool(node)
                        variant_key = "true" if is_truthy else "false"
                        text = variants.get(variant_key, "")
                        if text:
                            preamble_parts.append(str(text).strip())
                    if preamble_parts:
                        preamble = "\n\n".join(preamble_parts)
                        prompt_text = preamble + "\n\n" + prompt_text

                enriched = {
                    "id": agent_id,
                    "description": agent_cfg.get("description", agent_id),
                    "profile": agent_cfg.get("profile", "agent"),
                    "tools": agent_cfg.get("tools", []),
                    "max_iterations": int(agent_cfg.get("max_iterations", 10)),
                    "aliases": agent_cfg.get("aliases", [f"@{agent_id}"]),
                    "prompt_text": prompt_text,
                }

                for alias in enriched["aliases"]:
                    self.custom_agents[alias.lower()] = enriched

                loaded_ids.append(agent_id)

            logger.info(
                "Custom agents loaded: %s (%d alias mappings)",
                loaded_ids,
                len(self.custom_agents),
            )

        except Exception:
            logger.warning("Failed to load custom_agents.yaml", exc_info=True)

    def get_custom_agent_by_id(self, agent_id: str) -> dict | None:
        """Return the config dict for a custom agent by its ID, or None."""
        for cfg in self.custom_agents.values():
            if cfg["id"] == agent_id:
                return cfg
        return None

    def list_custom_agents(self) -> list[dict]:
        """Return one config dict per unique custom agent (de-duplicated by id)."""
        seen: dict[str, dict] = {}
        for cfg in self.custom_agents.values():
            seen[cfg["id"]] = cfg
        return list(seen.values())

    # ── Skill loading ──────────────────────────────────────────────────────

    def _load_skills(self) -> None:
        """Load skill definitions from skills.yaml.

        Builds two mappings:
        - ``self.skills``: alias (lowercase) → skill config dict
        - ``self.skills_by_id``: skill id → skill config dict

        Each skill config has these keys after enrichment:
        ``id``, ``description``, ``instruction_file``, ``instruction_text``,
        ``condensed``, ``aliases``, ``keywords``, ``modes``, ``auto_detect``,
        ``priority``.
        """
        service_root = Path(__file__).resolve().parent.parent.parent
        config_path = service_root / "skills.yaml"

        if not config_path.exists():
            logger.info("skills.yaml not found — skills disabled")
            return

        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

            self.skills_max = int(cfg.get("max_skills", 3))
            raw_skills: dict = cfg.get("skills", {})
            loaded_ids: list[str] = []

            for skill_id, skill_cfg in raw_skills.items():
                if not isinstance(skill_cfg, dict):
                    continue

                # Load instruction markdown
                instruction_file = skill_cfg.get("instruction_file", "")
                instruction_text = ""
                if instruction_file:
                    instr_path = service_root / instruction_file.strip()
                    if instr_path.exists():
                        instruction_text = instr_path.read_text(encoding="utf-8").strip()
                    else:
                        logger.warning(
                            "Skill '%s': instruction file not found: %s",
                            skill_id,
                            instr_path,
                        )

                # Build condensed version (for follow-up turns)
                condensed = skill_cfg.get("condensed", "").strip()
                if not condensed and instruction_text:
                    # Auto-generate condensed: first 300 chars + skill name
                    condensed = f"[Active skill: {skill_id}]\n{instruction_text[:300]}…"

                enriched = {
                    "id": skill_id,
                    "description": skill_cfg.get("description", skill_id),
                    "instruction_file": instruction_file,
                    "instruction_text": instruction_text,
                    "condensed": condensed,
                    "aliases": [a.lower() for a in skill_cfg.get("aliases", [])],
                    "keywords": [k.lower() for k in skill_cfg.get("keywords", [])],
                    "modes": skill_cfg.get("modes", []),
                    "disable_for_modes": skill_cfg.get("disable_for_modes", []),
                    "auto_detect": skill_cfg.get("auto_detect", True),
                    "priority": int(skill_cfg.get("priority", 0)),
                }

                # Register in id-based lookup
                self.skills_by_id[skill_id] = enriched

                # Register under all aliases
                for alias in enriched["aliases"]:
                    self.skills[alias] = enriched

                loaded_ids.append(skill_id)

            logger.info(
                "Skills loaded: %s (%d alias mappings)",
                loaded_ids,
                len(self.skills),
            )

        except Exception:
            logger.warning("Failed to load skills.yaml", exc_info=True)

    def list_skills(self) -> list[dict]:
        """Return one config dict per unique skill (de-duplicated by id)."""
        return list(self.skills_by_id.values())

    def get_skill_by_id(self, skill_id: str) -> dict | None:
        """Return the config dict for a skill by its ID, or None."""
        return self.skills_by_id.get(skill_id)

    def _load_user_context(self) -> None:
        """Load the user context Markdown file and append it to agent_system_prompt.

        The file path is read from ``config.yaml`` → ``user_context``.
        Silently skipped when:
          - the config key is missing or empty
          - the file does not exist at the resolved path
        """

        file_setting: str = getattr(self.af_settings, "user_context_file", "")
        if not file_setting:
            return

        # Resolve relative to agentforge/ (parent of app/)
        service_root = Path(__file__).resolve().parent.parent.parent
        ctx_path = Path(file_setting) if Path(file_setting).is_absolute() else service_root / file_setting

        if not ctx_path.exists():
            logger.debug("user_context file not found at %s — skipping", ctx_path)
            return

        try:
            self.user_context = ctx_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Could not read user_context file %s: %s", ctx_path, exc)
            return

        if not self.user_context:
            return

        # Append to the main agent system prompt so _run_agent and
        # _run_discovery pick it up automatically.
        if self.agent_system_prompt:
            self.agent_system_prompt = self.agent_system_prompt + "\n\n---\n\n" + self.user_context

        logger.info("User context loaded from %s (%d chars)", ctx_path, len(self.user_context))


# Per-session sticky filters — persisted across queries in the same WS connection
_session_sticky: dict[str, dict[str, str]] = {}

_session_sql_db: dict[str, str] = {}


def _resolve_source_type(source_name: str, filters: dict[str, str]) -> None:
    """Try to resolve source_type for a source_name not in the knowledge cache.

    Falls back to a direct indexer query so the UI can show e.g.,
    ``source_name=myapi, source_type=api`` even when the cache was empty
    at startup.
    """
    try:
        from app.services.indexer_service import indexer_service

        for src in indexer_service.discover_sources():
            name = src.get("source_name", src.get("api_name", ""))
            if name.lower() == source_name.lower():
                stype = src.get("source_type", "")
                if stype:
                    filters["source_type"] = stype
                # Also use the canonical casing from the indexer
                filters["source_name"] = name
                return
    except Exception:
        logger.debug("Could not resolve source_type for '%s'", source_name)


def _parse_query(
    raw_query: str,
    rt: SearchRuntime,
    session_id: str,
) -> tuple[str, dict[str, str], bool]:
    """Parse @source prefixes and --flags from the query, apply sticky filters.

    Returns (clean_query, filters, is_sticky) for @docs / RAG query parsing.
    """
    sticky = _session_sticky.get(session_id, {})
    parts = raw_query.split()
    query_parts: list[str] = []
    filters: dict[str, str] = {}
    has_explicit_source = False

    hashtag_sources: list[str] = []  # #hashtag source_name filters

    for part in parts:
        if part.startswith("--source="):
            filters["source_type"] = part.split("=", 1)[1]
            has_explicit_source = True
        elif part.startswith("--api="):
            filters["source_name"] = part.split("=", 1)[1]
            has_explicit_source = True
        elif part.startswith("--type="):
            val = part.split("=", 1)[1]
            if val != "all":
                filters["chunk_type"] = val
        elif part.startswith("--domain="):
            filters["domain_group"] = part.split("=", 1)[1]
        elif part.startswith("--limit="):
            try:
                filters["limit"] = str(int(part.split("=", 1)[1]))
            except ValueError:
                query_parts.append(part)
        elif part in ("--verbose", "--brief", "--no-floor"):
            filters[part.lstrip("-").replace("-", "_")] = "true"
        elif part.startswith("#") and len(part) > 1:
            # #hashtag → source_name filter (supports multiple)
            # Strip trailing punctuation (e.g., #mydb? → mydb)
            tag = part[1:].lower().rstrip("?!.,;:")
            if not tag:
                query_parts.append(part)
                continue
            has_explicit_source = True
            # Check SOURCE_TYPE_ALIASES first (e.g., #help → source_type=docs)
            if tag in rt.SOURCE_TYPE_ALIASES:
                filters["source_type"] = rt.SOURCE_TYPE_ALIASES[tag]
                logger.info("#hashtag '%s' → source_type filter '%s'", tag, rt.SOURCE_TYPE_ALIASES[tag])
            # Resolve through known sources / aliases
            elif tag in rt.SOURCE_ALIASES:
                alias_val = rt.SOURCE_ALIASES[tag]
                # Support list values (e.g., "myapi" → ["myapi-spec", "myapi-code"])
                canonicals = alias_val if isinstance(alias_val, list) else [alias_val]
                for canonical in canonicals:
                    resolved = rt.known_sources.get(canonical, {}).get("source_name", canonical)
                    hashtag_sources.append(resolved)
                logger.info(
                    "#hashtag '%s' → source_name filter %s (via alias)", tag, hashtag_sources[-len(canonicals) :]
                )
            elif tag in rt.known_sources:
                resolved = rt.known_sources[tag]["source_name"]
                hashtag_sources.append(resolved)
                logger.info("#hashtag '%s' → source_name filter '%s'", tag, resolved)
            else:
                hashtag_sources.append(tag)
                logger.info("#hashtag '%s' → source_name filter '%s' (literal)", tag, tag)
        elif part.startswith("@"):
            hint = part[1:].lower()
            if not hint:
                query_parts.append(part)
                continue
            has_explicit_source = True
            if hint in rt.SOURCE_TYPE_ALIASES:
                filters["source_type"] = rt.SOURCE_TYPE_ALIASES[hint]
            elif hint in rt.SOURCE_ALIASES:
                alias_val = rt.SOURCE_ALIASES[hint]
                canonicals = alias_val if isinstance(alias_val, list) else [alias_val]
                if len(canonicals) == 1:
                    canonical = canonicals[0]
                    if canonical in rt.known_sources:
                        filters["source_name"] = rt.known_sources[canonical]["source_name"]
                        if rt.known_sources[canonical].get("source_type"):
                            filters["source_type"] = rt.known_sources[canonical]["source_type"]
                    else:
                        filters["source_name"] = canonical
                else:
                    # Multi-source alias → use source_names list for OR filter
                    resolved = []
                    for c in canonicals:
                        resolved.append(rt.known_sources.get(c, {}).get("source_name", c))
                    filters["source_names"] = resolved
                    logger.info("@source '%s' → multi-source filter %s", hint, resolved)
            elif hint in rt.known_sources:
                filters["source_name"] = rt.known_sources[hint]["source_name"]
                if rt.known_sources[hint].get("source_type"):
                    filters["source_type"] = rt.known_sources[hint]["source_type"]
            else:
                # Unknown @source — still use as source_name filter directly.
                filters["source_name"] = hint
                _resolve_source_type(hint, filters)
                logger.info(
                    "@source '%s' not in knowledge cache — using as literal filter (type=%s)",
                    hint,
                    filters.get("source_type", "?"),
                )
        else:
            query_parts.append(part)

    # Apply hashtag source filters
    if hashtag_sources:
        if len(hashtag_sources) == 1:
            filters["source_name"] = hashtag_sources[0]
        else:
            # Multiple hashtags → OR filter (stored as comma-separated list)
            filters["source_names"] = ",".join(hashtag_sources)
        logger.info("Hashtag source filters: %s", hashtag_sources)

    query = " ".join(query_parts)

    # --- Auto-detect source name from query text ---
    auto_detected = False
    multi_source = False
    if "api_name" not in filters and "source_name" not in filters and "source_type" not in filters and rt.known_sources:
        query_lower = query.lower()
        seen: dict[str, dict] = {}
        for key, src_info in rt.known_sources.items():
            if key in query_lower:
                seen[src_info["source_name"]] = src_info
        matched = list(seen.values())
        if len(matched) == 1:
            filters["source_name"] = matched[0]["source_name"]
            if matched[0].get("source_type"):
                filters["source_type"] = matched[0]["source_type"]
            auto_detected = True
        elif len(matched) > 1:
            multi_source = True

    # --- Document-name detection ---
    if filters.get("source_type") == "document" and "document_name" not in filters and rt.known_documents:
        query_lower = query.lower()
        query_collapsed = query_lower.replace("-", "").replace("_", "")
        doc_match: str | None = None
        for key in sorted(rt.known_documents, key=len, reverse=True):
            if key in query_lower or key in query_collapsed:
                doc_match = rt.known_documents[key]
                break
        if doc_match:
            filters["document_name"] = doc_match

    # --- Sticky filter carry-forward ---
    if has_explicit_source:
        pass  # new explicit source replaces sticky
    elif not auto_detected and not multi_source and "api_name" not in filters and "source_name" not in filters:
        if sticky:
            filters.update(sticky)

    # Update sticky
    if multi_source:
        _session_sticky[session_id] = {}
    else:
        new_sticky: dict[str, str] = {}
        if filters.get("source_name"):
            new_sticky["source_name"] = filters["source_name"]
        if filters.get("source_type"):
            new_sticky["source_type"] = filters["source_type"]
        if new_sticky:
            _session_sticky[session_id] = new_sticky
        elif has_explicit_source:
            # User typed @something that wasn't recognized — don't change sticky
            pass
        else:
            # No source in query — if not auto-detected, keep sticky as-is
            pass

    is_sticky = (
        bool(sticky)
        and not has_explicit_source
        and not auto_detected
        and filters.get("source_name") == sticky.get("source_name")
    )

    return query, filters, is_sticky


def _get_profiles_from_yaml() -> list[str]:
    """Return the list of selectable AI profile names.

    Reads ``ai.profiles`` from ``framework-config.yaml`` (merged into
    the agentforge settings object) and filters out any profile tagged
    ``abstract: true`` — those are base/model definitions not meant to
    be selected directly.
    """
    try:
        from app.config import settings as _settings

        return list(_settings.ollama.list_selectable_profiles().keys())
    except Exception:
        logger.debug("Unable to load profile list from settings", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Dynamic session instructions — #remember / #forget
# ---------------------------------------------------------------------------

# #remember [global:] <text>   → store instruction
# #forget [global|all]          → clear instructions
_REMEMBER_RE = re.compile(
    r"^#remember\b",
    re.IGNORECASE,
)
_FORGET_RE = re.compile(
    r"^#forget\b",
    re.IGNORECASE,
)
_REMEMBER_GLOBAL_RE = re.compile(
    r"^#remember\s+global:\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_REMEMBER_TEXT_RE = re.compile(
    r"^#remember\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)


class _NullConvMemory:
    """Fallback used when ConversationMemory is not initialized (e.g., tests, disabled)."""

    def delete_exchange(self, session_id: str, prompt: str) -> bool:  # noqa: D401
        return False


def _conv_memory():
    """Return the ConversationMemory singleton, or a no-op stub if uninitialized."""
    from .conversation_memory import get_conversation_memory

    mem = get_conversation_memory()
    return mem if mem is not None else _NullConvMemory()


async def _handle_retry_query(
    *,
    ws,
    db,
    session_id: str,
    prompt_text: str,
    edited_text: str | None,
    conv_memory,
    in_flight: bool,
    resubmit,
) -> bool:
    """Validate + trim transcript + scrub memory for a retry request.

    Returns True when the caller should proceed to dispatch the re-run via
    *resubmit*, False when a query.retry.error was already sent.
    """
    last = db.get_last_user_query(session_id)
    sent = prompt_text or ""
    sent_no_prefix = re.sub(r"^\s*@[\w:-]+\s+", "", sent)
    stored = (last.content or "") if last is not None else None
    if last is None or stored not in (sent, sent_no_prefix):
        await ws.send_json(protocol.query_retry_error(prompt_text, "not_found"))
        return False

    if in_flight:
        await ws.send_json(protocol.query_retry_error(prompt_text, "in_flight"))
        return False

    db.delete_messages_from_sequence(session_id, last.sequence)
    conv_memory.delete_exchange(session_id, last.content or "")

    new_text = edited_text.strip() if edited_text else (sent or last.content or "")
    await resubmit(new_text)
    return True


async def _handle_remember(ws, db, session_id: str, text: str) -> None:
    """Persist a user instruction from a #remember message and confirm to client."""
    # Determine scope: global if text starts with "global:"
    global_match = _REMEMBER_GLOBAL_RE.match(text)
    if global_match:
        instr_text = global_match.group(1).strip()
        scope_session_id = None  # NULL = global
        scope = "global"
    else:
        plain_match = _REMEMBER_TEXT_RE.match(text)
        if not plain_match or not plain_match.group(1).strip():
            # Just "#remember" with no text — show current list
            instrs = db.get_session_instructions(session_id)
            if not instrs:
                await ws.send_json(
                    {
                        "type": "result",
                        "text": "No instructions saved for this session yet. Use `#remember <instruction>` to add one.",
                    }
                )
            else:
                lines = "\n".join(
                    f"{i + 1}. {instr.text} *(id:{instr.id}, {instr.scope})*" for i, instr in enumerate(instrs)
                )
                await ws.send_json(
                    {
                        "type": "result",
                        "text": f"**Active instructions ({len(instrs)}):**\n{lines}",
                    }
                )
            return
        instr_text = plain_match.group(1).strip()
        scope_session_id = session_id
        scope = "session"

    instr = db.add_session_instruction(instr_text, session_id=scope_session_id)
    total = len(db.get_session_instructions(session_id))
    logger.info(
        "Session instruction saved (scope=%s, id=%d, session=%s): %r",
        scope,
        instr.id,
        session_id,
        instr_text[:60],
    )
    await ws.send_json(
        protocol.instruction_saved(
            instruction_id=instr.id,
            text=instr_text,
            scope=scope,
            total=total,
        )
    )


async def _handle_forget(
    ws,
    db,
    session_id: str,
    text: str,
    broker: "ConfirmationBroker | None" = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Clear instructions from a #forget message, asking for confirmation first."""
    lower = text.lower().strip()
    if "global" in lower and "all" not in lower:
        scope = "global"
        preview = db.get_global_instructions()
    else:
        global_too = "all" in lower
        scope = "all" if global_too else "session"
        preview = (
            db.get_session_instructions(session_id)
            if not global_too
            else (db.get_session_instructions(session_id) + db.get_global_instructions())
        )

    count_preview = len(preview)
    if count_preview == 0:
        await ws.send_json(protocol.instruction_cleared(count=0, scope=scope))
        return

    # Build a human-readable summary for the confirm dialog
    scope_label = {"session": "this session", "global": "all sessions (global)", "all": "this session and globally"}[
        scope
    ]
    lines = "\n".join(f"  • {i.text}" for i in preview[:10])
    suffix = f"\n  … and {count_preview - 10} more" if count_preview > 10 else ""
    confirm_prompt = (
        f"Clear {count_preview} instruction{'s' if count_preview != 1 else ''} for {scope_label}?\n{lines}{suffix}"
    )

    # Request confirmation via the standard confirm dialog
    if broker:
        confirmed = await broker.request(confirm_prompt)
        if not confirmed:
            return  # user cancelled — do nothing
    # else: no broker (shouldn't happen in normal flow) — proceed without confirm

    # Perform the deletion
    if scope == "global":
        instrs = db.get_global_instructions()
        count = 0
        for i in instrs:
            db.delete_session_instruction(i.id)
            count += 1
    else:
        global_too = scope == "all"
        count = db.clear_session_instructions(session_id, global_too=global_too)

    logger.info(
        "Session instructions cleared (scope=%s, count=%d, session=%s)",
        scope,
        count,
        session_id,
    )
    await ws.send_json(protocol.instruction_cleared(count=count, scope=scope))


# ---------------------------------------------------------------------------
# Mode classifier — decides "search" vs "agent" for each query
# ---------------------------------------------------------------------------

# Keywords that strongly suggest agent mode (system operations, not docs)
_AGENT_KEYWORDS = {
    # Infrastructure / remote
    "docker",
    "container",
    "ssh",
    "myserver",
    "vm",
    "server",
    "restart",
    "reboot",
    "deploy",
    "systemctl",
    "service",
    "disk",
    "memory",
    "cpu",
    "process",
    "kill",
    "ps",
    "mkdir",
    "chmod",
    "chown",
    "cron",
    "cronjob",
    "apt",
    "yum",
    "pip install",
    "npm install",
    "tail",
    "grep",
    "find",
    "ls",
    "cat",
    "nginx",
    "apache",
    "redis",
    "postgres",
    "mysql",
    "backup",
    "restore",
    "download",
    "upload",
    "scp",
    "running",
    "status",
    "health",
    "uptime",
    "load",
    # File operations
    "zip",
    "unzip",
    "tar",
    "gzip",
    "compress",
    "extract",
    "copy",
    "move",
    "delete",
    "remove",
    "rename",
    "rm",
    "cp",
    "mv",
    "ln",
    "folder",
    "directory",
    # Git operations
    "git",
    # Filesystem exploration (only terms unlikely to appear in doc questions)
    "tree",
    "clone",
    # Shell / CLI (package managers, runtimes, build tools)
    "poetry",
    "npm",
    "npx",
    "node",
    "pipx",
    "cargo",
    "make",
    "cmake",
    "pip install",
    "npm install",
    "poetry install",
}

# Patterns that suggest the user wants to EXECUTE something (not look up docs)
_AGENT_PATTERNS = [
    r"\brun\b.*\bon\b",  # "run X on Y"
    r"\bcheck\b.*\b(status|running|health)\b",
    r"\brestart\b",
    r"\bshow me.*\b(running|active|logs)\b",
    r"\blist\b.*\b(containers|processes|services)\b",
    r"\bconnect\b.*\b(to|via)\b",
    r"\bexecute\b",
    r"\bssh\b",
    r"\bdocker\s+(ps|logs|exec|run|stop|start|restart)\b",
    r"\bgit\s+(status|log|diff|show|branch|checkout|pull|push|stash|commit|add|reset)\b",
    # File operations
    r"\bzip\b.*\b(folder|directory|file|into)\b",
    r"\bcopy\b.*\bto\b",
    r"\bmove\b.*\bto\b",
    r"\bdelete\b.*\b(file|folder|zip|original|from|the)\b",
    r"\brename\b.*\bto\b",
    r"\bextract\b.*\b(to|into|from)\b",
    r"\bcompress\b.*\b(folder|directory|file|into)\b",
    r"\btar\b.*\b(folder|directory|file)\b",
    r"\bcreate\b.*\b(folder|directory|file)\b",
    # Filesystem exploration
    r"\btree\s+(view|of)\b",  # "tree view of", "tree of"
    r"\b(show|list|find)\b.*\b(files|folders)\b",  # "show me ... files", "find all files"
    r"\bclone\b.*\bhttps?://",  # "clone https://..." (URL present → operational)
    r"(~/|/[Uu]sers/|/home/|/opt/|/var/|/tmp/|/etc/)",  # absolute/home paths → likely operational
    # Shell / CLI
    r"\b(npm|npx|poetry|pip|pipx|cargo|make)\s+\w+",  # "npm install", "poetry run", etc.
    r"\b(node|python|python3)\s+\S+",  # "node app.js", "python script.py"
    r"\b(install|uninstall|upgrade)\b.*\b(package|module|dependency|lib)\b",
]


# ---------------------------------------------------------------------------
# Per-mode heuristic patterns
# ---------------------------------------------------------------------------
# Sub-millisecond high-confidence routing for clear natural-language
# phrasings of each mode's intent. Checked BEFORE the agent keyword
# cluster + agent regex patterns so phrases like "show me errors in
# nginx logs" route to logs instead of being captured by the generic
# agent "show me ... logs" pattern.
#
# Conservative on purpose — patterns require a clear *topic verb +
# object* combination so they don't fire on tangentially-similar text.
# When in doubt the heuristic falls through to the existing agent
# checks and (at escalation_threshold=medium+) to the LLM. False
# positives here cost user trust; false negatives just cost one LLM
# round-trip.
#
# Order matters within this dict — Python dicts are insertion-ordered
# and we evaluate top-down, returning the first matching mode. Place
# more specific intents first.
_MODE_PATTERNS: dict[str, list[str]] = {
    # ── @scheduler ─────────────────────────────────────────────────
    # "schedule a daily backup at 2am", "run this every 15 minutes",
    # "set up a cron job for nightly cleanup"
    "scheduler": [
        r"\bschedule\b.*\b(at\s+\d|every|daily|weekly|hourly|nightly|cron)\b",
        r"\b(create|add|set\s+up)\s+(a\s+)?(scheduled\s+(job|task)|cron(\s*job)?)\b",
        r"\b(every|each)\s+\d+\s+(minute|hour|day|week)s?\b",
        r"\bcron\b.*\b(job|expression|schedule|tab)\b",
        r"\b(nightly|hourly|daily)\s+(backup|job|task|run|cleanup|sync)\b",
        r"\b(list|show)\b.*\bscheduled\s+(jobs|tasks)\b",
        r"\bcancel\s+(the\s+)?(scheduled\s+)?(job|task)\b",
    ],
    # ── @monitor ───────────────────────────────────────────────────
    # "watch this URL", "alert me when X changes", "track this page"
    "monitor": [
        r"\bwatch\s+(this\s+)?(url|page|site|website)\b",
        r"\b(monitor|track)\b.*\b(url|page|site|website|for\s+changes?|for\s+updates?)\b",
        r"\balert\s+me\b.*\b(when|if)\b.*\b(page|site|url|change|update)\b",
        r"\bset\s+up\s+(a\s+)?(monitor|watcher)\s+(for|on)\b",
        r"\b(list|show)\s+(my\s+)?(active\s+)?monitors?\b",
        r"\bstop\s+monitoring\b",
    ],
    # ── @review ────────────────────────────────────────────────────
    # "review my changes", "code quality check on X", "audit this PR"
    "review": [
        r"\breview\b.*\b(my|the|this)\s+(code|changes?|commits?|diff|pr|pull[-\s]request)\b",
        r"\b(check|review|audit)\b.*\b(code\s+quality|error\s+handling|test\s+coverage|type\s+safety)\b",
        r"\bcode\s+review\b",
        r"\bquality\s+check\b.*\b(on|for|of)\b",
        r"\b(audit|inspect)\s+(my\s+)?(staged\s+)?changes?\b",
        r"\bparallel\s+(code\s+)?review\b",
    ],
    # ── @sql ───────────────────────────────────────────────────────
    # "how many orders last week", "top 10 customers by revenue"
    "sql": [
        r"\bhow\s+many\b.*\b(rows|records|entries|orders?|users?|customers?|invoices?|sales?|transactions?)\b",
        r"\b(top|bottom)\s+\d+\b.*\b(by|per|in)\b.*\b(revenue|sales|orders?|count|users?|customers?)\b",
        r"\b(count|sum|total|average|avg)\b.*\b(rows|records|orders?|users?|customers?|sales?|invoices?|transactions?)\b",
        r"\b(query|select)\b.*\b(database|table|from\s+\w+|rows)\b",
        r"\b(list|show)\b.*\bdaily\s+(active|new|unique)\s+\w+\b",
        r"\b(total\s+)?revenue\s+(per|by|breakdown\s+by)\b",
    ],
    # ── @logs ──────────────────────────────────────────────────────
    # "show me errors in nginx logs", "tail syslog", "what errors happened"
    "logs": [
        r"\b(show|tail|grep|analyse|analyze)\b.*\blogs?\b",
        r"\b(what|which|how\s+many)\s+(errors?|warnings?|crashes?|exceptions?)\b.*\b(today|hour|recent|last|past|happened|occurred)\b",
        r"\b(stack\s*trace|stacktrace)\b",
        r"\b(error|warning|crash|exception)\b.*\b(in|from|inside)\b.*\blogs?\b",
        r"\b(last|recent|latest)\s+(\d+\s+)?(lines?\s+of\s+)?(syslog|access\s+log|error\s+log|application\s+log)\b",
        r"\b(failed\s+login|404|500|503|timeout)\b.*\blogs?\b",
        r"\blogs?\b.*\b(from|since|today|yesterday|last\s+(hour|day|week))\b",
    ],
    # ── @discover ──────────────────────────────────────────────────
    # "investigate why X is slow", "audit security posture", "full overview"
    "discover": [
        r"\binvestigate\b.*\b(why|the\s+system|server|deployment|network|why\s+\w+\s+is)\b",
        r"\baudit\b.*\b(security|posture|stack|deployment|firewall|open\s+ports)\b",
        r"\b(full|complete)\s+(overview|audit|investigation)\b",
        r"\bcomprehensive\s+(health|check|analysis|review)\b",
        r"\bmap\s+(out\s+)?(all\s+)?(the\s+)?(services|dependencies|stack|deployment)\b",
        r"\b(analyse|analyze)\s+(resource\s+usage|deployment|infrastructure)\b",
        r"\b(what'?s|whats)\s+(running\s+on|the\s+state\s+of)\b",
    ],
    # ── @web_search ────────────────────────────────────────────────
    # "search the web for X", "look up online", "find on the internet"
    # NB: kept last because "search" alone is too generic. Patterns
    # require an internet/web/online qualifier.
    "web_search": [
        r"\bsearch\b.*\b(the\s+(web|internet)|online|google)\b",
        r"\bweb\s+search\b",
        r"\blook\s*up\b.*\b(online|on\s+the\s+(web|internet))\b",
        r"\bfind\b.*\b(on\s+the\s+(web|internet)|online)\b",
        r"\b(google|search|lookup)\b.*\b(for|about)\b.*\b(news|article|blog|tutorial)\b",
        r"\bwhat'?s?\s+new\s+in\s+\w+",  # "what's new in Python 3.13"
        r"\b(latest|recent|new)\s+(version|release|features?|news)\s+(of|from|in)\b",
        r"\b(who|what|when|where)\s+(directed|founded|invented|wrote|released)\b",
    ],
}

# Compile once at module load — patterns are static.
_MODE_PATTERN_REGEX: dict[str, list] = {
    mode: [re.compile(p, re.IGNORECASE) for p in patterns] for mode, patterns in _MODE_PATTERNS.items()
}


def _match_mode_patterns(query_lower: str) -> str | None:
    """Return the first mode whose pattern set matches ``query_lower``.

    Order follows ``_MODE_PATTERNS.keys()`` — more specific intents
    first, broader ones (web_search) last so they don't shadow.
    Returns ``None`` when nothing matches; the caller then falls through
    to the agent keyword/pattern checks.
    """
    for mode, regexes in _MODE_PATTERN_REGEX.items():
        for regex in regexes:
            if regex.search(query_lower):
                return mode
    return None


_STICKY_MODES = frozenset(("web_search", "logs", "sql", "scheduler", "monitor", "research", "coding"))


_CHAT_ALIASES = {"@chat"}
_AGENT_ALIASES = {"@agent"}
_SEARCH_ALIASES = {"@docs"}
_WEB_SEARCH_ALIASES = {"@search"}
_LOGS_ALIASES = {"@logs"}
_DISCOVER_ALIASES = {"@discover"}
_SQL_ALIASES = {"@sql"}
_PIPELINE_ALIASES = {"@pipeline"}
_SCHEDULER_ALIASES = {"@scheduler"}
_MONITOR_ALIASES = {"@monitor"}
_REVIEW_ALIASES = {"@review"}
_RESEARCH_ALIASES = {"@research"}
_CODING_ALIASES = {"@coding", "@code"}
_CONNECTOR_ALIASES = {"@conn", "@connector"}
# Aliases that can appear anywhere in the query (not just at the start)
_ANYWHERE_ALIASES = {"@docs"}


_CANVAS_URL_RE = re.compile(r'https?://[^\s\'"<>)\]]+')
_CANVAS_TAG_RE = re.compile(r"(?<!\w)#([a-zA-Z][\w-]*)")


async def _canvas_scan_query(
    ws: WebSocket,
    session_id: str,
    query_text: str,
    attachments: list | None,
    *,
    incognito: bool = False,
) -> None:
    """Auto-detect canvas items from an incoming user query.

    Scans for URLs, #hashtags, and file attachments. Dedup is handled
    by the UNIQUE constraint in CanvasDatabase.add_item() — duplicates
    simply return the existing item without creating a new row.

    Skipped when canvas is disabled (_canvas_db is None) or incognito.
    """
    if _canvas_db is None or incognito or not session_id:
        return

    ws_closed = False

    async def _emit(item: dict) -> None:
        nonlocal ws_closed
        if ws_closed:
            return
        try:
            await ws.send_json(protocol.canvas_item_added(item))
        except (WebSocketDisconnect, RuntimeError):
            ws_closed = True

    # URL scanner
    for url in _CANVAS_URL_RE.findall(query_text):
        url = url.rstrip(".,;:!?)")
        label = url.replace("https://", "").replace("http://", "")[:80]
        try:
            item = _canvas_db.add_item(session_id, "url", url, label)
            await _emit(item)
        except Exception:
            logger.debug("Canvas URL scan error for %r", url, exc_info=True)

    # Tag scanner — #hashtags in the query
    for tag in _CANVAS_TAG_RE.findall(query_text):
        tag_lower = tag.lower()
        # Skip mode aliases (@agent, @search, etc.) — they're not content tags
        if query_text.lstrip().startswith(f"@{tag_lower}"):
            continue
        try:
            item = _canvas_db.add_item(session_id, "tag", tag_lower, f"#{tag_lower}")
            await _emit(item)
        except Exception:
            logger.debug("Canvas tag scan error for %r", tag_lower, exc_info=True)

    # File scanner — attachments sent with this query
    for attachment in attachments or []:
        filename = attachment.get("name") or attachment.get("filename") or ""
        if not filename:
            continue
        label = filename[:80]
        try:
            item = _canvas_db.add_item(session_id, "file", filename, label)
            await _emit(item)
        except Exception:
            logger.debug("Canvas file scan error for %r", filename, exc_info=True)


def _strip_mode_prefix(query: str) -> tuple[str, str | None]:
    """Detect mode aliases in the query and strip them.

    Start-of-query aliases (@agent, @search, @logs, etc.) are checked first.
    Anywhere aliases (@qdrant) can appear at any position in the query.

    Returns (cleaned_query, forced_mode).
    forced_mode is "chat", "agent", "search", "web_search", "logs", "discover", "review", or None.
    """
    stripped = query.lstrip()
    lower = stripped.lower()

    # Start-of-query detection (all non-anywhere aliases)
    _PREFIX_GROUPS: list[tuple[set[str], str]] = [
        (_CHAT_ALIASES, "chat"),
        (_SQL_ALIASES, "sql"),
        (_AGENT_ALIASES, "agent"),
        (_WEB_SEARCH_ALIASES, "web_search"),
        (_LOGS_ALIASES, "logs"),
        (_SEARCH_ALIASES, "search"),
        (_DISCOVER_ALIASES, "discover"),
        (_PIPELINE_ALIASES, "pipeline"),
        (_REVIEW_ALIASES, "review"),
        (_RESEARCH_ALIASES, "research"),
        (_SCHEDULER_ALIASES, "scheduler"),
        (_MONITOR_ALIASES, "monitor"),
        (_CODING_ALIASES, "coding"),
    ]
    for aliases, mode in _PREFIX_GROUPS:
        for alias in aliases:
            if lower.startswith(alias):
                rest = stripped[len(alias) :].lstrip()
                return rest, mode

    # Anywhere-in-query detection (@qdrant)
    for alias in _ANYWHERE_ALIASES:
        if alias in lower:
            idx = lower.index(alias)
            rest = (stripped[:idx] + stripped[idx + len(alias) :]).strip()
            return rest, "search"

    return query, None


# Keep backward-compatible name so callers still work
def _strip_agent_prefix(query: str) -> tuple[str, bool]:
    """Backward-compatible wrapper around _strip_mode_prefix."""
    cleaned, mode = _strip_mode_prefix(query)
    return cleaned, mode == "agent"


def _inject_user_context(prompt: str, rt: "SearchRuntime") -> str:
    """Append the user context block to *prompt* if one is configured.

    Used by runners whose system prompt is built locally (web_search,
    log_analysis, custom_agent).  _run_agent and _run_discovery get the
    context via rt.agent_system_prompt, which is already augmented at
    SearchRuntime init time.
    """
    if rt.user_context:
        return prompt + "\n\n---\n\n" + rt.user_context
    return prompt


def _inject_skills(prompt: str, overrides: dict | None, condensed: bool = False) -> str:
    """Append skill instructions to a system prompt if skills are present.

    Reads ``_skills`` from *overrides* (injected by the dispatch handler) and
    builds the skill instruction block.  On the first turn, the full skill
    markdown is injected.  On follow-ups (``_skills_condensed=True``), only
    the condensed summary is used to save tokens.
    """
    skills = (overrides or {}).get("_skills")
    if not skills:
        return prompt
    # Use condensed mode from overrides if set, otherwise fall back to parameter
    use_condensed = (overrides or {}).get("_skills_condensed", condensed)
    skill_block = _build_skill_prompt(skills, condensed=use_condensed)
    if skill_block:
        return prompt + skill_block
    return prompt


def _build_skill_prompt(skills: list[dict], condensed: bool = False) -> str:
    """Build the skill instruction block to inject into a system prompt.

    When ``condensed=False`` (first turn), the full instruction text from
    the skill's markdown file is injected.

    When ``condensed=True`` (follow-up turns), only the condensed summary
    is injected to save tokens.

    Multiple skills are separated by horizontal rules.
    """
    if not skills:
        return ""

    parts: list[str] = []
    for skill in skills:
        if condensed:
            text = skill.get("condensed", "")
        else:
            text = skill.get("instruction_text", "")
        if text:
            parts.append(text)

    if not parts:
        return ""

    # Wrap in a clear delimiter so the model knows this is skill guidance
    body = "\n\n---\n\n".join(parts)
    header = "[Active Skills — follow these guidelines for this task]"
    return f"\n\n{header}\n\n{body}"


def _merge_skills(client_skills: list[dict], server_skills: list[dict]) -> list[dict]:
    """Merge client-supplied skills with the server's keyword matches.

    External clients (e.g., Felix) retrieve their own skills from the vector
    store and pass them via ``overrides._skills``. Those must never be clobbered
    by the keyword resolver, so client skills come first and win on id collision;
    server matches are appended after de-duplication.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for skill in list(client_skills) + list(server_skills):
        if not isinstance(skill, dict):
            continue
        sid = str(skill.get("id") or skill.get("instruction_text", "")[:40])
        if sid in seen:
            continue
        seen.add(sid)
        merged.append(skill)
    return merged


def _resolve_skills(
    query: str,
    rt: SearchRuntime,
    mode: str,
) -> tuple[str, list[dict], str | None]:
    """Detect skills that should be activated for this query.

    Two detection paths:
    1. **Explicit alias** — ``@deploy``, ``@cr``, etc. anywhere in the query.
       The alias is stripped from the query text.  When the alias-matched skill
       has a ``modes`` list that does **not** contain the current *mode*, the
       function returns a *promoted_mode* suggestion — the first compatible mode
       from the skill's list — so the caller can upgrade the dispatch mode.
    2. **Keyword auto-detect** — if ≥2 keywords from a skill's keyword list
       appear in the query, the skill is auto-selected (unless auto_detect=false).
       Keyword matches are still filtered by mode compatibility (no promotion).

    Skills are filtered by mode compatibility for keyword matches.  Alias matches
    always pass through so the caller can decide whether to promote.

    Returns ``(cleaned_query, matched_skills, promoted_mode)`` where
    *matched_skills* is sorted by priority (descending) and capped at
    ``rt.skills_max``, and *promoted_mode* is ``None`` when no promotion is
    needed.
    """
    if not rt.skills_by_id:
        return query, [], None

    matched: dict[str, dict] = {}  # skill_id → config (de-dupe)
    cleaned = query
    promoted_mode: str | None = None

    # --- Pass 1: Explicit alias detection (strip from query) ----------------
    # Alias-matched skills are ALWAYS included.  If the skill's modes list
    # doesn't contain the current mode, we suggest promoting to the first
    # compatible mode (preferring "agent" when present).
    query_lower = query.lower()
    for alias, skill_cfg in rt.skills.items():
        if alias in query_lower:
            matched[skill_cfg["id"]] = skill_cfg
            # Strip the alias from the query
            idx = query_lower.index(alias)
            cleaned = (cleaned[:idx] + cleaned[idx + len(alias) :]).strip()
            query_lower = cleaned.lower()

            # Check if mode promotion is needed
            if skill_cfg["modes"] and not promoted_mode:
                base_mode = mode.split(":", 1)[-1] if mode.startswith("custom:") else mode
                if mode not in skill_cfg["modes"] and base_mode not in skill_cfg["modes"]:
                    # Suggest the first compatible mode, preferring "agent"
                    if "agent" in skill_cfg["modes"]:
                        promoted_mode = "agent"
                    else:
                        promoted_mode = skill_cfg["modes"][0]

    # --- Pass 2: Keyword auto-detect ----------------------------------------
    words = set(re.sub(r"[^\w\s/~.]", "", query_lower).split())
    for skill_cfg in rt.skills_by_id.values():
        if skill_cfg["id"] in matched:
            continue  # Already matched via alias
        if not skill_cfg["auto_detect"]:
            continue
        if not skill_cfg["keywords"]:
            continue
        # Check mode compatibility (keyword matches are strict — no promotion)
        if skill_cfg["modes"]:
            base_mode = mode.split(":", 1)[-1] if mode.startswith("custom:") else mode
            if mode not in skill_cfg["modes"] and base_mode not in skill_cfg["modes"]:
                continue
        # Count keyword hits
        hits = sum(1 for kw in skill_cfg["keywords"] if kw in words)
        if hits >= 2:
            matched[skill_cfg["id"]] = skill_cfg

    # Filter out skills disabled for the active mode
    for skill_id in list(matched):
        disable_for = matched[skill_id].get("disable_for_modes") or []
        if mode in disable_for:
            logger.debug("Skill '%s' suppressed — covered by mode '%s'", skill_id, mode)
            del matched[skill_id]

    # Sort by priority (highest first) and cap
    result = sorted(matched.values(), key=lambda s: s["priority"], reverse=True)
    result = result[: rt.skills_max]

    if result:
        logger.info(
            "Skills resolved: %s (for mode=%s%s)",
            [s["id"] for s in result],
            mode,
            f" → promoted to {promoted_mode}" if promoted_mode else "",
        )

    return cleaned, result, promoted_mode


def _agent_ref_for_connection(conn: dict, rt: SearchRuntime) -> str | None:
    """Agent ref for a connection: its account agent if grouped, else its own."""
    mgr = rt.connection_manager
    acct = (conn.get("account_identifier") or "").strip()
    if acct:
        cfg = mgr._account_agents.get(account_slug(acct))
        if cfg:
            return f"custom:{cfg['id']}"
    cfg = mgr._agents.get(conn["id"])
    return f"custom:{cfg['id']}" if cfg else None


def _resolve_connector_agent(query: str, rt: SearchRuntime) -> tuple[str, str | None]:
    """Route a @conn query to the right connector agent.

    Resolution order:
    1. **Hashtag** — ``#label`` anywhere in the query matches a connection by
       label slug (e.g., ``#gitlab-com``, ``#hello-rschu-me``). The hashtag is
       stripped from the query before execution.
    2. **Keyword matching** — email/inbox → gmail, file/folder → drive,
       merge/pipeline → gitlab. Picks the first matching connection of that type.
    3. **Fallback** — the most recently used connection (keeps hashtag-less
       follow-ups like ``@conn yes`` on the connector you just used).
    """
    if not hasattr(rt, "connection_manager") or rt.connection_manager is None:
        return query, None

    connections = rt.connection_manager.list_connections()
    active = [c for c in connections if c["status"] == "active"]
    if not active:
        return query, None
    active.sort(key=lambda c: c.get("last_used_at") or "", reverse=True)

    # -- 1. Hashtag targeting: #label or #account-slug -> that connection's agent
    conn_by_tag: dict[str, dict] = {}
    for c in active:
        conn_by_tag[label_slug(c["label"])] = c
        acct = (c.get("account_identifier") or "").strip()
        if acct:
            conn_by_tag.setdefault(account_slug(acct), c)

    for m in re.finditer(r"#([a-zA-Z][\w-]*)", query):
        conn = conn_by_tag.get(m.group(1).lower())
        if not conn:
            continue
        ref = _agent_ref_for_connection(conn, rt)
        if ref:
            cleaned = (query[: m.start()].rstrip() + " " + query[m.end() :].lstrip()).strip()
            return cleaned, ref

    # -- 2. Single connection shortcut --------------------------------------
    if len(active) == 1:
        return query, _agent_ref_for_connection(active[0], rt)

    # -- 3. Keyword-based type matching -------------------------------------
    lower = query.lower()
    _TYPE_KEYWORDS = {
        "gmail": {
            "email",
            "mail",
            "inbox",
            "thread",
            "label",
            "unread",
            "sender",
            "subject",
            "unsubscribe",
            "newsletter",
        },
        "google_drive": {"file", "folder", "document", "sheet", "spreadsheet", "slide", "pdf", "drive", "shared"},
        "gitlab": {
            "merge",
            "pipeline",
            "runner",
            "branch",
            "commit",
            "gitlab",
            "mr",
            "ci",
            "cd",
            "job",
            "project",
            "repo",
        },
        "bigquery": {"bigquery", "pypi", "downloads", "dataset", "query", "table", "package", "stats", "statistics"},
    }

    scores = {t: sum(1 for kw in kws if kw in lower) for t, kws in _TYPE_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    target_type = best if scores[best] > 0 else None

    if target_type:
        for c in active:
            if c["connector_type"] == target_type:
                ref = _agent_ref_for_connection(c, rt)
                if ref:
                    return query, ref

    # -- 4. Fallback — first active connection ------------------------------
    return query, _agent_ref_for_connection(active[0], rt)


def _strip_custom_prefix(query: str, rt: SearchRuntime) -> tuple[str, str | None]:
    """Check whether *query* starts with (or implicitly targets) a custom agent.

    Three detection strategies:

    1. **Connector umbrella** — ``@conn`` / ``@connector`` at the beginning routes
       to the best-matching connector agent based on query keywords.

    2. **Start-of-query alias** — ``@cloud``, ``@debug``, etc. at the beginning
       of the query.  Returns the cleaned query and ``"custom:<agent_id>"``.

    3. **Anywhere hashtag triggers** — ``#myservice`` or ``#mytag`` anywhere
       in the query automatically routes to the ``cloud`` custom agent.

    Returns ``(cleaned_query, "custom:<agent_id>")`` on match, or
    ``(query, None)`` if no custom alias is found.
    """
    stripped = query.lstrip()
    lower = stripped.lower()

    # 1. Connector umbrella (@conn, @connector)
    for alias in _CONNECTOR_ALIASES:
        if lower.startswith(alias + " ") or lower == alias:
            rest = stripped[len(alias) :].lstrip()
            return _resolve_connector_agent(rest, rt)

    # 2. Start-of-query alias detection (@cloud, @debug, etc.)
    for alias, agent_cfg in rt.custom_agents.items():
        # Match "alias " (with trailing space) or bare alias at end of string
        if lower.startswith(alias + " ") or lower == alias:
            rest = stripped[len(alias) :].lstrip()
            return rest, f"custom:{agent_cfg['id']}"

    # 2. Hashtag routes — activate the configured custom agent when one of its
    #    hashtags appears anywhere (hashtag_routes in config; none in the public
    #    build). An explicit built-in mode prefix (@agent, @search, ...) at the
    #    start of the query still wins, so e.g.,
    #    "@agent list files and save to ~/Downloads/foo.txt #myhashtag"
    #    does not silently re-route.
    if _CLOUD_HASHTAGS and _HASHTAG_ROUTE_AGENT and any(tag in lower for tag in _CLOUD_HASHTAGS):
        _, _builtin_mode = _strip_mode_prefix(query)
        if _builtin_mode is None:
            route_cfg = rt.custom_agents.get("@" + _HASHTAG_ROUTE_AGENT)
            if route_cfg:
                return query, f"custom:{route_cfg['id']}"

    return query, None


# Hashtag routes — bespoke shortcuts that auto-promote a query to a custom
# agent (``hashtag_routes`` in config.yaml; empty in the public build). Used by
# ``_strip_custom_prefix`` to route, and by ``_normalize_hashtags`` to replace
# the tags with natural-language equivalents when the query did NOT route to the
# agent (so the LLM doesn't read e.g., ``#myservice`` as an SSH host alias).
from app.config import settings as _hr_settings  # noqa: E402

_HASHTAG_ROUTE_AGENT = _hr_settings.hashtag_routes.agent  # e.g., "cloud"
_HASHTAG_ROUTE_MODE = f"custom:{_HASHTAG_ROUTE_AGENT}" if _HASHTAG_ROUTE_AGENT else ""
_CLOUD_HASHTAGS = tuple(_hr_settings.hashtag_routes.tags.keys())
_CLOUD_HASHTAG_REPLACEMENTS = dict(_hr_settings.hashtag_routes.tags)
_CLOUD_HASHTAG_RE = (
    re.compile(r"(?<!\w)(" + "|".join(re.escape(tag) for tag in _CLOUD_HASHTAGS) + r")\b", re.IGNORECASE)
    if _CLOUD_HASHTAGS
    else None
)


def _normalize_cloud_hashtags(query: str, mode: str) -> str:
    """Replace routing hashtags with natural-language references.

    Skipped for the route's own agent mode (its prompt understands the
    hashtags as topic markers). For every other mode (notably ``agent``), the
    raw hashtag confuses the LLM — qwen3.5 has been seen to interpret
    ``#myservice`` as an SSH host alias and call ``ssh '#myservice'``. Replacing with
    the configured name removes the ambiguity. No-op when no routes configured.
    """
    if _CLOUD_HASHTAG_RE is None or mode == _HASHTAG_ROUTE_MODE:
        return query
    if not any(tag in query.lower() for tag in _CLOUD_HASHTAGS):
        return query

    def _sub(match: re.Match) -> str:
        return _CLOUD_HASHTAG_REPLACEMENTS.get(match.group(1).lower(), match.group(0))

    return _CLOUD_HASHTAG_RE.sub(_sub, query)


def _classify_mode_heuristic(
    query: str,
    rt: SearchRuntime,
    last_mode: str = "chat",
) -> tuple[str, str]:
    """Classify a query as 'chat', 'search', 'agent', etc. (synchronous heuristic).

    Returns ``(mode, confidence)`` where confidence is one of:
      - "high"    — explicit prefix, custom alias, keyword cluster (≥2 hits),
                    sticky tier 1 (short follow-up with strong signal),
                    sticky agent (with explicit agent hint)
      - "medium"  — single pattern match, sticky tier 2 (long with context+action),
                    very-short sticky agent without explicit hint
      - "low"     — chat fallback (genuinely ambiguous, no signal)

    The confidence drives the LLM escalation tier in ``_classify_mode``:
    by default only "low" escalates (cheapest), but operators can flip the
    threshold to "medium" to widen the LLM safety net for borderline cases.
    """
    if not rt.agent_available:
        return ("chat", "low")

    query_lower = query.lower()

    # Custom agent aliases take priority — checked before built-in prefixes
    _, custom_mode = _strip_custom_prefix(query, rt)
    if custom_mode:
        logger.info("Mode classifier: %s (custom agent alias)", custom_mode)
        return (custom_mode, "high")

    # Explicit mode prefix: @agent/@tooling/@tools/@run/@exec → agent
    #                        @search/@web → web_search (web search via agent)
    #                        @docs/@find → search (local RAG)
    #                        @discover/@discovery/@investigate → discover
    _, forced_mode = _strip_mode_prefix(query)
    if forced_mode == "agent":
        logger.info("Mode classifier: agent (explicit prefix)")
        return ("agent", "high")
    if forced_mode == "web_search":
        logger.info("Mode classifier: web_search (explicit prefix)")
        return ("web_search", "high")
    if forced_mode == "logs":
        logger.info("Mode classifier: logs (explicit prefix)")
        return ("logs", "high")
    if forced_mode == "search":
        logger.info("Mode classifier: search (explicit prefix)")
        return ("search", "high")
    if forced_mode == "discover":
        logger.info("Mode classifier: discover (explicit prefix)")
        return ("discover", "high")
    if forced_mode == "sql":
        logger.info("Mode classifier: sql (explicit prefix)")
        return ("sql", "high")
    if forced_mode == "scheduler":
        logger.info("Mode classifier: scheduler (explicit prefix)")
        return ("scheduler", "high")
    if forced_mode == "monitor":
        logger.info("Mode classifier: monitor (explicit prefix)")
        return ("monitor", "high")
    if forced_mode == "research":
        logger.info("Mode classifier: research (explicit prefix)")
        return ("research", "high")
    if forced_mode == "coding":
        logger.info("Mode classifier: coding (explicit prefix)")
        return ("coding", "high")

    # If the query has an unrecognised @-prefix (e.g., @cooding typo, or
    # a deprecated source like @myapi / @changelog), surface that loudly
    # instead of silently falling into search. The runner translates this
    # sentinel into an error card listing the available modes, so users
    # learn the right prefix instead of staring at a confusing search
    # result that "didn't find anything".
    if query_lower.lstrip().startswith("@"):
        logger.info("Mode classifier: unknown_prefix (no built-in or custom alias matched)")
        return ("unknown_prefix", "high")

    # Per-mode pattern check — sub-millisecond high-confidence routing for
    # clear natural-language phrasings ("search the web for X", "what
    # errors in logs", "schedule a daily backup", etc.). Checked BEFORE
    # the agent keyword/pattern blocks so phrases like "show me errors
    # in nginx logs" route to @logs instead of being captured by the
    # generic agent "show me ... logs" pattern. See `_MODE_PATTERNS`
    # for the (conservative) rule list.
    mode_match = _match_mode_patterns(query_lower)
    if mode_match is not None:
        logger.info("Mode classifier: %s (per-mode pattern)", mode_match)
        return (mode_match, "high")

    # Check keyword overlap (strip punctuation so "delete?" matches "delete")
    words = set(re.sub(r"[^\w\s/~.]", "", query_lower).split())
    hits = words & _AGENT_KEYWORDS
    if len(hits) >= 2:
        logger.info("Mode classifier: agent (keywords: %s)", hits)
        return ("agent", "high")

    # Check regex patterns. A single pattern hit (no keyword cluster) is
    # weaker evidence than ≥2 keywords — mark medium so the LLM can take a
    # second look when the escalation threshold is "medium" or lower.
    for pattern in _AGENT_PATTERNS:
        if re.search(pattern, query_lower):
            logger.info("Mode classifier: agent (pattern: %s)", pattern)
            return ("agent", "medium")

    # Sticky mode: if the previous query was web_search or logs, stay in
    # that mode for follow-ups.  Two tiers:
    #   1. Short queries (≤ 15 words): sticky if pronouns/phrases or ≤ 10 words
    #   2. Any length: sticky if the query references prior context AND
    #      involves tool-like actions (save, search, file ops, TMDB verbs)
    if last_mode in _STICKY_MODES:
        _FOLLOWUP_PRONOUNS = re.compile(r"\bthem\b|\bthose\b|\bthese\b|\bthey\b|\bits?\b|\bthat\b")
        _FOLLOWUP_PHRASES = re.compile(
            r"\bwhat\s+about\b|\bhow\s+about\b|\band\s+what\s+about\b"
            r"|\bwhat\s+if\b|\bwhat\s+else\b|\bhow\s+else\b"
            r"|\band\s+\w+\?$|\bwhich\b|\bwhere\b"
        )
        has_followup = bool(_FOLLOWUP_PRONOUNS.search(query_lower) or _FOLLOWUP_PHRASES.search(query_lower))

        # Tier 1: short follow-ups (original behaviour)
        if len(words) <= 15 and (has_followup or len(words) <= 10):
            logger.info(
                "Mode classifier: %s (sticky — follow-up, %d words, pronoun/phrase=%s)",
                last_mode,
                len(words),
                has_followup,
            )
            return (last_mode, "high")

        # Tier 2: longer follow-ups that reference prior context AND contain
        # action verbs implying tool use (save, search, find, download, etc.)
        # or file-system paths.  These are multi-step instructions building on
        # the previous web_search/logs result — they should NOT fall to RAG.
        _CONTEXT_REF = re.compile(
            r"\bfrom\s+that\b|\bfrom\s+the\s+list\b|\bfrom\s+those\b"
            r"|\bfrom\s+above\b|\babove\s+list\b"
            r"|\bthe\s+highest\b|\bthe\s+lowest\b|\bthe\s+best\b|\bthe\s+top\b"
            r"|\bpick\b.*\bfrom\b|\bchoose\b.*\bfrom\b|\bselect\b.*\bfrom\b"
        )
        _ACTION_VERBS = re.compile(
            r"\bsave\b|\bstore\b|\bwrite\b|\bdownload\b|\bexport\b"
            r"|\bsearch\b|\bfind\b|\blook\s?up\b|\bfetch\b|\bget\b"
            r"|\bcreate\b|\bgenerate\b|\bmake\b"
        )
        _FILE_PATH = re.compile(
            r"~/|/[Uu]sers/|/home/|\bdownloads?\b|\bdesktop\b|\bdocuments?\b"
            r"|\.\w{1,5}\b"
        )
        has_context_ref = bool(has_followup or _CONTEXT_REF.search(query_lower))
        has_action = bool(_ACTION_VERBS.search(query_lower) or _FILE_PATH.search(query_lower))
        if has_context_ref and has_action:
            logger.info(
                "Mode classifier: %s (sticky — long follow-up, %d words, context_ref=%s, action=%s)",
                last_mode,
                len(words),
                has_context_ref,
                has_action,
            )
            # Tier 2 sticky is weaker — long queries can be topic shifts
            # even when they contain context refs and action verbs. Mark
            # medium so the LLM can second-guess at higher escalation
            # thresholds.
            return (last_mode, "medium")

    # Sticky agent mode: if previous query was agent and this looks like a
    # short follow-up (no explicit @source, few words), keep agent mode.
    # Heuristics for "follow-up":
    #   - previous query was agent
    #   - query is short (≤ 15 words) — long questions are likely new topics
    #   - query contains at least one agent keyword, a filesystem path,
    #     a contextual continuity word, OR a pronoun referencing prior results
    #   - query doesn't contain strong search signals (question words + "what/how/why/explain")
    if last_mode == "agent" and len(words) <= 15:
        _SEARCH_SIGNALS = {"what", "how", "why", "explain", "describe", "define", "documentation"}
        has_search_signal = bool(words & _SEARCH_SIGNALS)
        has_any_agent_hint = bool(hits) or bool(
            re.search(
                r"("
                # Filesystem paths
                r"~/|/[Uu]sers/|/home/|/opt/|/var/|/tmp/|\.\w{1,5}$"
                r"|"
                # Continuity words — suggest "keep going in same mode"
                r"\bnow\b|\balso\b|\bthen\b|\band\b|\bsame\b|\bagain\b"
                r"|"
                # Pronouns referencing previous results — strong follow-up signal
                r"\bthem\b|\bthose\b|\bthese\b|\bthey\b|\bits?\b|\bthat\b"
                r"|"
                # Action/intent verbs that imply operating on prior context
                r"\bdelete\b|\bremove\b|\bclean\b|\bback\s?up\b|\barchive\b"
                r"|\bmove\b|\bcopy\b|\brename\b|\bopen\b|\bshow\b|\bdo\b"
                r")",
                query_lower,
            )
        )
        # Signals that override a search keyword in short follow-ups:
        # 1. Pronouns referencing previous results (they, them, those, ...)
        _FOLLOWUP_PRONOUNS = re.compile(r"\bthem\b|\bthose\b|\bthese\b|\bthey\b|\bits?\b|\bthat\b")
        has_followup_pronoun = bool(_FOLLOWUP_PRONOUNS.search(query_lower))
        # 2. Follow-up phrases ("what about X?", "how about X?", "and X?")
        _FOLLOWUP_PHRASES = re.compile(
            r"\bwhat\s+about\b|\bhow\s+about\b|\band\s+what\s+about\b"
            r"|\bwhat\s+if\b|\bwhat\s+else\b|\bhow\s+else\b"
            r"|\band\s+\w+\?$"  # "and port 5173?" — trailing question
        )
        has_followup_phrase = bool(_FOLLOWUP_PHRASES.search(query_lower))
        has_followup_signal = has_followup_pronoun or has_followup_phrase

        if has_any_agent_hint and not has_search_signal:
            logger.info("Mode classifier: agent (sticky — follow-up, hints: kw=%s)", hits or "contextual")
            return ("agent", "high")
        # Very short queries (≤ 8 words) are almost certainly conversational
        # follow-ups.  A search signal like "how" or "what" is overridden
        # when the query also contains a follow-up signal — a pronoun
        # referencing prior results (e.g., "how old are they?") or a
        # follow-up phrase (e.g., "what about port 5173?").
        if len(words) <= 8 and (not has_search_signal or has_followup_signal):
            logger.info(
                "Mode classifier: agent (sticky — very short follow-up, %d words, pronoun=%s, phrase=%s)",
                len(words),
                has_followup_pronoun,
                has_followup_phrase,
            )
            # Very-short sticky agent: no explicit agent keyword/path —
            # confidence is medium, the LLM may better resolve whether
            # this is a topic shift.
            return ("agent", "medium")

    # Default to chat (general LLM knowledge — no Qdrant)
    return ("chat", "low")


async def _classify_mode(
    query: str,
    rt: SearchRuntime,
    last_mode: str = "chat",
    db: ChatDatabase | None = None,
    session_id: str | None = None,
) -> str:
    """Classify a query using heuristic-first routing with LLM escalation.

    Priority order (fastest to slowest):
      1. Custom agent aliases — O(n) string match, <1ms
      2. Explicit @prefix — O(n) string match, <1ms
      3. Heuristic classifier — keyword/pattern/sticky-mode, <1ms
      4. LLM classifier — full Ollama round-trip, 0.5–3s (only for ambiguous)

    The LLM is only consulted when the heuristic returns "chat" (no strong
    signal for any specific mode), providing a ~1.5s average saving on most
    queries that clearly belong to agent/search/web_search/logs modes.

    Returns one of:
      - a valid mode string (chat/search/agent/...)
      - "unknown_prefix" — caller must surface a "no such mode" error card
    """
    from web.server import classifier_audit as _ca

    t0 = time.perf_counter()
    layer = _ca.LAYER_FALLBACK_CHAT
    heuristic_mode = ""
    heuristic_confidence = ""
    llm_mode: str | None = None
    final_mode = "chat"

    # Read the escalation threshold once per call. "low" = only escalate
    # when heuristic returned "chat" (current behaviour). "medium" = also
    # escalate on single-pattern matches + sticky tier 2 + very-short
    # sticky agent. "high" = LLM always runs except for explicit prefixes.
    classifier_profile_name = "fast"
    try:
        from agentforge.config import get_config

        _cfg = get_config()
        threshold = str(_cfg.get("routing.classifier.escalation_threshold", "low")).lower()
        pass_hint = bool(_cfg.get("routing.classifier.pass_hint", True))
        classifier_profile_name = str(_cfg.get("routing.classifier.profile", "fast") or "fast")
    except Exception:
        threshold = "low"
        pass_hint = True

    # Resolve profile → model/provider for telemetry. Falls back to empty
    # strings if the named profile doesn't exist; the actual call site
    # in classify_intent re-tries fast/cloud-light when the name is bad.
    llm_profile = ""
    llm_model = ""
    llm_provider = ""
    try:
        _resolved = get_config().get_profile(classifier_profile_name)
        llm_profile = _resolved.name
        llm_model = _resolved.model
        llm_provider = _resolved.provider
    except Exception:
        llm_profile = classifier_profile_name

    # Confidence rank: higher number = more confident, escalate when
    # heuristic_rank <= threshold_rank.
    _RANK = {"high": 3, "medium": 2, "low": 1}
    threshold_rank = _RANK.get(threshold, 1)

    try:
        # Fast-path: custom agent aliases (no LLM round-trip needed).
        _, custom_mode = _strip_custom_prefix(query, rt)
        if custom_mode:
            logger.info("Mode classifier: %s (custom agent alias)", custom_mode)
            layer = _ca.LAYER_CUSTOM_ALIAS
            heuristic_confidence = "high"
            final_mode = custom_mode
            return custom_mode

        # Fast-path: explicit prefix detection.
        _, forced_mode = _strip_mode_prefix(query)
        if forced_mode:
            logger.info("Mode classifier: %s (explicit prefix)", forced_mode)
            layer = _ca.LAYER_EXPLICIT_PREFIX
            heuristic_confidence = "high"
            final_mode = forced_mode
            return forced_mode

        # Heuristic classifier — returns (mode, confidence).
        heuristic_mode, heuristic_confidence = _classify_mode_heuristic(
            query,
            rt,
            last_mode=last_mode,
        )
        if heuristic_mode == "unknown_prefix":
            # User typed an @-prefix that doesn't match any known mode or
            # custom alias. Surface loudly via the runner's error card.
            layer = _ca.LAYER_UNKNOWN_PREFIX
            final_mode = "unknown_prefix"
            return "unknown_prefix"

        heuristic_rank = _RANK.get(heuristic_confidence, 1)

        # If the heuristic is confident enough for the configured
        # threshold AND not the chat fallback, skip the LLM.
        if heuristic_mode != "chat" and heuristic_rank > threshold_rank:
            logger.info(
                "Mode classifier: %s (heuristic %s — skipped LLM, threshold=%s)",
                heuristic_mode,
                heuristic_confidence,
                threshold,
            )
            if heuristic_mode == last_mode and last_mode not in ("chat", ""):
                layer = _ca.LAYER_STICKY
            else:
                layer = _ca.LAYER_HEURISTIC_KEYWORD
            final_mode = heuristic_mode
            return heuristic_mode

        # Escalate to LLM: either heuristic returned "chat" (low confidence)
        # OR the heuristic_rank is at/below the escalation threshold.
        from agentforge.intent_classifier import RouteResult, classify_intent

        history: list[dict[str, str]] = []
        if db and session_id:
            all_messages = db.get_messages(session_id)
            for msg in all_messages[-6:]:
                if msg.role in ("user", "assistant"):
                    history.append({"role": msg.role, "content": msg.content})

        def _heuristic_fallback(q: str) -> RouteResult:
            # If we have a non-chat heuristic verdict, fall back to that
            # rather than dropping all the way to "chat" when the LLM
            # is unavailable. This preserves the legacy behaviour for
            # high-confidence heuristic picks even when escalation is on.
            if heuristic_mode and heuristic_mode != "chat":
                return RouteResult(
                    mode=heuristic_mode,
                    reason=f"LLM unavailable; using heuristic ({heuristic_confidence})",
                    source="fallback",
                )
            return RouteResult(
                mode="chat",
                reason="LLM + heuristic both ambiguous",
                source="fallback",
            )

        # Pass the heuristic verdict as a hint when enabled — the LLM
        # uses it as a prior and only overrides on clear evidence.
        hint = (heuristic_mode, heuristic_confidence) if pass_hint and heuristic_mode else None

        # Surface registered custom agents to the LLM so it can route
        # prompts like "what's on my cloud storage" directly to custom:<agent>
        # rather than falling to the generic agent mode. rt.custom_agents
        # is the registry populated from custom_agents.yaml at startup.
        custom_agent_metadata: list[dict] = []
        try:
            agents_registry = getattr(rt, "custom_agents", {}) or {}
            seen_ids: set[str] = set()
            for alias, cfg in agents_registry.items():
                agent_id = (cfg.get("id") or alias).strip()
                if agent_id in seen_ids:
                    continue  # multiple aliases share one agent — list once
                seen_ids.add(agent_id)
                custom_agent_metadata.append(
                    {
                        "alias": agent_id,
                        "description": (cfg.get("description") or cfg.get("name") or "").strip(),
                    }
                )
        except Exception:
            logger.debug("Failed to build custom_agents list for LLM router", exc_info=True)

        result = await classify_intent(
            query,
            conversation_history=history,
            fallback_fn=_heuristic_fallback,
            heuristic_hint=hint,
            profile_name=classifier_profile_name,
            custom_agents=custom_agent_metadata or None,
        )
        llm_mode = result.mode
        final_mode = result.mode
        if result.source == "fallback":
            layer = _ca.LAYER_FALLBACK_CHAT
        else:
            layer = _ca.LAYER_LLM
        return result.mode

    finally:
        # Fire-and-forget telemetry. Never raises.
        audit = _ca.get_classifier_audit()
        if audit is not None:
            try:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                await audit.log_verdict(
                    session_id=session_id or "",
                    query=query,
                    last_mode=last_mode,
                    layer=layer,
                    heuristic_mode=heuristic_mode,
                    heuristic_confidence=heuristic_confidence,
                    llm_mode=llm_mode,
                    llm_profile=llm_profile if layer == _ca.LAYER_LLM else "",
                    llm_model=llm_model if layer == _ca.LAYER_LLM else "",
                    llm_provider=llm_provider if layer == _ca.LAYER_LLM else "",
                    final_mode=final_mode,
                    latency_ms=latency_ms,
                )
            except Exception:
                logger.debug("classifier_audit.log_verdict failed", exc_info=True)


def init_runtime() -> SearchRuntime:
    """Create the shared runtime.

    Called from app.py lifespan via run_in_executor (background thread).
    Signals _runtime_ready once complete so the WebSocket handler can proceed.
    """
    global _runtime
    _runtime = SearchRuntime()

    # --- Ensure the knowledge-base collection exists (empty is fine) -------
    # A fresh deployment has no indexed data, so searches (@docs, @sql schema
    # context) would 404 on a missing collection. Create it empty + idempotent
    # so queries succeed and simply return nothing until data is indexed.
    _ensure_kb_collection()

    # --- Initialise connectors (dynamic agents from OAuth connections) ------
    _init_connectors(_runtime)

    # --- Initialise conversation memory -----------------------------------
    _init_conversation_memory(_runtime)

    # --- Initialise tool result cache -------------------------------------
    _init_tool_cache()

    # --- Initialise audit log, result store, session events ---------------
    _init_audit_log()
    _init_classifier_audit()
    _init_result_store()
    _init_session_events()

    # Signal from the background thread — use call_soon_threadsafe so the
    # asyncio event is set on the correct event loop.
    try:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(_runtime_ready.set)
    except RuntimeError:
        # Fallback: no running loop yet (shouldn't happen in normal operation)
        _runtime_ready.set()
    return _runtime


def _init_connectors(rt: SearchRuntime) -> None:
    """Initialise the connector plugin system and load active connections."""
    try:
        from agentforge.connectors import ConnectorRegistry
        from agentforge.connectors.bigquery import BigQueryConnectorPlugin
        from agentforge.connectors.github import GitHubConnectorPlugin
        from agentforge.connectors.gitlab import GitLabConnectorPlugin
        from agentforge.connectors.gmail import GmailConnectorPlugin
        from agentforge.connectors.google import GoogleConnectorPlugin
        from agentforge.connectors.google_drive import GoogleDriveConnectorPlugin
        from agentforge.connectors.manager import ConnectionManager
        from agentforge.connectors.youtube import YouTubeConnectorPlugin

        db = get_db()

        connector_registry = ConnectorRegistry()
        connector_registry.register(GoogleConnectorPlugin())
        connector_registry.register(GitLabConnectorPlugin())
        connector_registry.register(GitHubConnectorPlugin())
        connector_registry.register(GmailConnectorPlugin())
        connector_registry.register(GoogleDriveConnectorPlugin())
        connector_registry.register(BigQueryConnectorPlugin())
        connector_registry.register(YouTubeConnectorPlugin())

        connection_manager = ConnectionManager(
            db_session_factory=db.SessionLocal,
            registry=connector_registry,
            tool_registry=rt.registry,
        )
        connection_manager.load_connections(rt.custom_agents)

        rt.connection_manager = connection_manager
        rt.connector_registry = connector_registry

        conn_count = len(connection_manager.list_connections())
        logger.info("Connectors initialised: %d active connections", conn_count)

        # Make available to the REST API
        from .connectors.api import init_connectors_api

        init_connectors_api(
            connection_manager=connection_manager,
            connector_registry=connector_registry,
            custom_agents=rt.custom_agents,
        )
    except Exception as exc:
        logger.warning("Connectors init failed: %s — connector features unavailable", exc)
        rt.connection_manager = None
        rt.connector_registry = None


def _ensure_kb_collection() -> None:
    """Create the Qdrant knowledge-base collection if it doesn't exist yet.

    Idempotent (``ensure_collection`` no-ops when present). Guarded so a Qdrant
    outage can't break startup — searches will surface their own error then.
    """
    try:
        from app.services.vector_service import vector_service

        vector_service.ensure_collection()
    except Exception as exc:
        logger.warning("Could not ensure KB collection on startup: %s", exc)


def _init_conversation_memory(rt: SearchRuntime) -> None:
    """Initialise the semantic conversation memory service.

    Uses ``app.config.settings`` for Qdrant/embedding connection details (which
    honour environment variables like ``QDRANT_HOST``) and reads the memory-
    specific knobs from ``config.yaml → memory.semantic``.
    """
    try:
        import yaml

        config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg: dict = {}
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

        mem_cfg = cfg.get("memory", {}).get("semantic", {})
        if not mem_cfg.get("enabled", False):
            logger.info("Conversation memory disabled (memory.semantic.enabled=false)")
            return

        from app.config import settings as af_settings
        from app.services.embedding_service import embedding_service

        from .conversation_memory import init_conversation_memory

        mem = init_conversation_memory(
            embedding_service=embedding_service,
            qdrant_host=af_settings.qdrant.host,
            qdrant_port=af_settings.qdrant.port,
            collection=mem_cfg.get("collection", "conversation_memory"),
            dimension=af_settings.embedding.dimension,
            recall_top_k=mem_cfg.get("recall_top_k", 5),
            min_score=mem_cfg.get("min_score", 0.55),
            exclude_current_session=mem_cfg.get("exclude_current_session", False),
        )

        # Purge stale monitor/scheduler memory on startup — these modes store
        # ephemeral state (job IDs, schedules) that becomes stale after a DB
        # reset and causes the LLM to hallucinate nonexistent jobs.
        _PURGE_MODES = ["monitor", "scheduler"]
        for purge_mode in _PURGE_MODES:
            try:
                mem.delete_by_mode(purge_mode)
                logger.info("Purged conversation_memory for mode=%r on startup", purge_mode)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Conversation memory init failed: %s — cross-session recall disabled", exc)


def _init_tool_cache() -> None:
    """Initialise the Redis-backed tool result cache."""
    try:
        import yaml

        config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg: dict = {}
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

        cache_cfg = cfg.get("memory", {}).get("tool_cache", {})
        if not cache_cfg.get("enabled", False):
            logger.info("Tool cache disabled (memory.tool_cache.enabled=false)")
            return

        from .tool_cache import init_tool_cache

        init_tool_cache(
            default_ttl=cache_cfg.get("default_ttl", 300),
        )
    except Exception as exc:
        logger.warning("Tool cache init failed: %s — tool caching disabled", exc)


def _init_audit_log() -> None:
    """Initialise the Redis Streams audit log."""
    try:
        import yaml

        config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg: dict = {}
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

        al_cfg = cfg.get("memory", {}).get("audit_log", {})
        if not al_cfg.get("enabled", False):
            logger.info("Audit log disabled (memory.audit_log.enabled=false)")
            return

        from .audit_log import init_audit_log

        init_audit_log(max_entries=al_cfg.get("max_entries", 50_000))
    except Exception as exc:
        logger.warning("Audit log init failed: %s — audit logging disabled", exc)


def _init_classifier_audit() -> None:
    """Initialise the Redis Streams telemetry for classifier verdicts.

    Reads ``memory.classifier_audit`` from config.yaml. Disabled by
    default to keep this phased rollout opt-in; flip ``enabled: true``
    once you want to start collecting routing telemetry.
    """
    try:
        import yaml

        config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg: dict = {}
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

        ca_cfg = cfg.get("memory", {}).get("classifier_audit", {})
        if not ca_cfg.get("enabled", False):
            logger.info(
                "Classifier audit disabled (memory.classifier_audit.enabled=false)",
            )
            return

        from .classifier_audit import init_classifier_audit

        init_classifier_audit(max_entries=ca_cfg.get("max_entries", 50_000))
    except Exception as exc:
        logger.warning(
            "Classifier audit init failed: %s — classifier telemetry disabled",
            exc,
        )


def _init_result_store() -> None:
    """Initialise the session-scoped Redis result store."""
    try:
        import yaml

        config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg: dict = {}
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

        rs_cfg = cfg.get("memory", {}).get("result_store", {})
        if not rs_cfg.get("enabled", False):
            logger.info("Result store disabled (memory.result_store.enabled=false)")
            return

        from .result_store import init_result_store

        init_result_store(
            session_ttl=rs_cfg.get("session_ttl", 1800),
            max_entry_size=rs_cfg.get("max_entry_size", 100_000),
        )
    except Exception as exc:
        logger.warning("Result store init failed: %s — session result caching disabled", exc)


def _init_session_events() -> None:
    """Initialise the Redis Pub/Sub session event broadcaster."""
    try:
        import yaml

        config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg: dict = {}
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

        se_cfg = cfg.get("memory", {}).get("session_events", {})
        if not se_cfg.get("enabled", False):
            logger.info("Session events disabled (memory.session_events.enabled=false)")
            return

        from .session_events import init_session_event_publisher

        init_session_event_publisher()
    except Exception as exc:
        logger.warning("Session events init failed: %s — Pub/Sub broadcasting disabled", exc)


def get_runtime() -> SearchRuntime:
    """Get the shared runtime (raises if not yet initialised)."""
    if _runtime is None:
        raise RuntimeError("SearchRuntime not initialised — call init_runtime() first")
    return _runtime


# ---------------------------------------------------------------------------
# Image attachment helpers
# ---------------------------------------------------------------------------


def _load_image_attachments(attachments: list) -> list[str]:
    """Read image attachments from disk and return a list of base64-encoded strings.

    Only processes entries where ``is_image`` is truthy and the ``path``
    resolves to a readable file.  Non-image attachments and missing files
    are silently skipped so a bad attachment never breaks the whole request.
    """

    b64_images: list[str] = []
    for att in attachments or []:
        if not att.get("is_image"):
            continue
        path = att.get("path", "")
        if not path:
            continue
        try:
            with open(path, "rb") as fh:
                b64_images.append(base64.b64encode(fh.read()).decode("ascii"))
        except OSError as exc:
            logger.warning("Could not read image attachment %s: %s", path, exc)
    return b64_images


def _load_document_attachments(attachments: list) -> str:
    """Read non-image document attachments and return their text content.

    For PDFs the upload endpoint pre-extracts text to a ``.extracted.md``
    sidecar file — we prefer that.  For plain text/markdown/CSV files we
    read the original directly.  Each document is wrapped in a labelled
    block so the LLM knows which file it came from.

    Returns an empty string when there are no readable documents.
    """

    _TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".log", ".magnet"}
    blocks: list[str] = []

    for att in attachments or []:
        if att.get("is_image"):
            continue  # handled by _load_image_attachments

        name = att.get("name", "attachment")

        # PDF — prefer the pre-extracted markdown sidecar written by the upload endpoint
        extracted_path = att.get("extracted_path", "")
        if extracted_path:
            try:
                text = open(extracted_path, encoding="utf-8", errors="replace").read().strip()
                if text:
                    blocks.append(f"### Attached document: {name}\n\n{text}")
                    continue
            except OSError as exc:
                logger.warning("Could not read extracted PDF %s: %s", extracted_path, exc)

        # Plain text files — read the original directly
        path = att.get("path", "")
        if path and pathlib.Path(path).suffix.lower() in _TEXT_SUFFIXES:
            try:
                text = open(path, encoding="utf-8", errors="replace").read().strip()
                if text:
                    blocks.append(f"### Attached document: {name}\n\n{text}")
            except OSError as exc:
                logger.warning("Could not read text attachment %s: %s", path, exc)

    return "\n\n---\n\n".join(blocks)


def _attachment_from_payload(a: dict):
    """Build an ``Attachment`` from a worker ``_attachments`` entry.

    Preloads the pre-extracted text sidecar (``.extracted.md`` written by the
    upload endpoint for PDFs/Office docs) so non-UTF-8 binaries aren't silently
    dropped on the worker path, mirroring the in-process ``_load_document_attachments``.
    """
    from agentforge.attachments import Attachment

    att = Attachment(path=a["path"], name=a.get("name", ""))
    extracted = a.get("extracted_path")
    if extracted:
        try:
            att.extracted_text = open(extracted, encoding="utf-8", errors="replace").read()
        except OSError:
            pass
    return att


def _inject_attachments(agent_client, messages: list[dict], overrides: dict | None) -> list[dict]:
    """Enrich the last user message with uploaded attachments and return the list.

    Worker modes deliver attachments as ``overrides['_attachments']`` (path/name/
    is_image/extracted_path). This pops them and runs ``AIClient._apply_attachments``
    so images go to the message's ``images`` field and document text/blocks are
    attached provider-aware (text for Ollama, native blocks for Bedrock) — exactly
    like _run_agent. No-op (returns *messages* unchanged) when there are none.

    The pop is intentional: callers may forward ``overrides`` to the worker/agent
    afterwards and ``_attachments`` must not leak as an unknown override key.
    """
    raw_atts = (overrides or {}).pop("_attachments", None)
    if not raw_atts:
        return messages

    atts = [_attachment_from_payload(a) for a in raw_atts if a.get("path")]
    if not atts:
        return messages
    return agent_client._apply_attachments(messages, atts)


def _attachment_text_block(overrides: dict | None) -> str:
    """Concatenated text of uploaded *document* attachments (images skipped).

    For the planner/orchestrator worker runners (research, discover, review,
    coding) that drive off a query *string* rather than a message list — the
    enriched text is appended to the entry query so the planner sees the file.
    Provider-native document blocks (Bedrock) aren't possible through a string,
    so this is plain-text only; PDFs/Office docs are read from their pre-extracted
    sidecar when present. Pops ``overrides['_attachments']``; "" when there are none.
    """
    raw_atts = (overrides or {}).pop("_attachments", None)
    if not raw_atts:
        return ""

    blocks: list[str] = []
    for a in raw_atts:
        if a.get("is_image") or not a.get("path"):
            continue
        text = _attachment_from_payload(a).as_context_text()
        if text:
            blocks.append(f"\n\n--- Attached file: {a.get('name', 'attachment')} ---\n{text}")
    return "".join(blocks)


# ---------------------------------------------------------------------------
# Ollama client helper — respects profile host & api_key
# ---------------------------------------------------------------------------


def _ollama_client_for_profile(profile) -> "ollama.Client":
    """Create an Ollama client bound to the profile's host and auth headers.

    The module-level ``ollama.chat()`` always uses ``OLLAMA_HOST`` and ignores
    per-profile settings.  This helper creates an explicit ``Client`` so that
    profiles with ``host: "https://ollama.com"`` and ``api_key`` go directly
    to the cloud API instead of relaying through a local Ollama instance.
    """
    import ollama as _ollama

    headers = getattr(profile, "headers", None) or {}
    if not headers and getattr(profile, "api_key", ""):
        headers = {"Authorization": f"Bearer {profile.api_key}"}
    return _ollama.Client(host=profile.host, headers=headers)


# ---------------------------------------------------------------------------
# Auto-title generation via fast model
# ---------------------------------------------------------------------------


def _generate_title(query: str) -> str:
    """Use the lightweight cloud model to generate a short chat title.

    Wrapped in ``retry_call`` so transient 5xx / network hiccups from
    cloud-light (same backend used by fact extraction and compaction)
    no longer silently fall through to the truncation fallback.
    """
    try:
        from agentforge.backends._retry import retry_call
        from agentforge.client import AIClient

        client = AIClient(profile="cloud-light")

        def _call():
            return client.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Generate a very short title (max 6 words) for a chat conversation "
                            "that starts with the user message below. Return ONLY the title, "
                            "no quotes, no punctuation at the end, no explanation."
                        ),
                    },
                    {"role": "user", "content": query},
                ],
                temperature=0.3,
            )

        response = retry_call(_call, max_attempts=3, context="title-gen")
        title = (response.content or "").strip().strip("\"'")
        if not title or len(title) > 80:
            title = query[:50].strip()
            if len(query) > 50:
                title = title.rsplit(" ", 1)[0] + "..."
        return title
    except Exception:
        logger.exception("Title generation failed after retries, using truncation fallback")
        title = query[:50].strip()
        if len(query) > 50:
            title = title.rsplit(" ", 1)[0] + "..."
        return title


# ---------------------------------------------------------------------------
# Conversation history reconstruction
# ---------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4
_MAX_HISTORY_TOKENS = 3000
_MAX_HISTORY_TURNS = 10

# Hard ceiling on a single LLM pipeline call (search + refine / chat).
# Ollama clients have their own 600s request timeout, but that only fires
# per-call.  This wraps the entire runner operation so a hung model cannot
# hold the WebSocket open forever.  300s is intentionally below 600s so the
# asyncio.TimeoutError fires first and produces a clear client-side message
# rather than an opaque Ollama connection error.
_PIPELINE_TIMEOUT = 600

# Poll interval for _cancellable_wait — how often we check the cancel event.
# 300ms is a good trade-off: fast enough to feel instant, cheap on CPU.
_CANCEL_POLL_INTERVAL = 0.3


async def _cancellable_wait(
    coro_or_future,
    cancel_event: threading.Event | None,
    *,
    timeout: float | None = _PIPELINE_TIMEOUT,
):
    """Await *coro_or_future* but bail out immediately when *cancel_event* fires.

    This solves the core cancellation problem: ``asyncio.to_thread()`` blocks
    until the background thread finishes, so ``Task.cancel()`` only takes
    effect *after* the (potentially minutes-long) LLM HTTP call returns.

    Instead we wrap the work in its own task and poll ``cancel_event`` in a
    tight async loop.  When the event fires we raise ``CancelledError``
    straight away — the user sees the cancel within ~300 ms.  The orphaned
    background thread keeps running but the agent loop's own cancel check
    (``cancel_event.is_set()``) will terminate it at the next iteration
    boundary.

    Falls back to normal ``await`` when *cancel_event* is ``None``.
    """
    task = asyncio.ensure_future(coro_or_future)

    if cancel_event is None:
        # No cancel support — just respect the timeout
        if timeout is not None:
            return await asyncio.wait_for(task, timeout=timeout)
        return await task

    deadline = (asyncio.get_event_loop().time() + timeout) if timeout else None

    while not task.done():
        if cancel_event.is_set():
            task.cancel()  # best-effort cancel of the inner task
            raise asyncio.CancelledError()
        if deadline and asyncio.get_event_loop().time() >= deadline:
            task.cancel()
            raise asyncio.TimeoutError()
        # Sleep briefly then re-check
        await asyncio.sleep(_CANCEL_POLL_INTERVAL)

    return task.result()


# ---------------------------------------------------------------------------
# Agent progress event callback factory
# ---------------------------------------------------------------------------
# Maps agent event kinds to protocol message constructors.  Used by all
# runner functions (_run_agent, _run_web_search, _run_log_analysis) to
# stream real-time progress to the WebSocket client.

_AGENT_EVENT_BUILDERS: dict[str, Any] = {
    "iteration": lambda d, e: protocol.agent_iteration(
        d["iteration"],
        d["max_iterations"],
        d["messages_in_context"],
        e,
    ),
    "thinking": lambda d, e: protocol.agent_thinking(
        d["iteration"],
        d["status"],
        e,
    ),
    "tool_exec": lambda d, e: protocol.agent_tool_exec(
        iteration=d["iteration"],
        name=d["name"],
        status=d["status"],
        elapsed=e,
        **{k: v for k, v in d.items() if k not in ("iteration", "name", "status")},
    ),
    "retry": lambda d, e: protocol.agent_retry(
        d["iteration"],
        d["attempt"],
        d["max_attempts"],
        d["reason"],
        d["delay_seconds"],
        e,
    ),
    "recovery": lambda d, e: protocol.agent_recovery(
        d["iteration"],
        d["tool"],
        d["error"],
        d["attempt"],
        d["max_retries"],
        e,
    ),
    "escalation": lambda d, e: protocol.agent_escalation(
        d["iteration"],
        d["type_detail"],
        d["consecutive_errors"],
        d["search_query"],
        e,
    ),
    "warning": lambda d, e: protocol.agent_warning(
        d["iteration"],
        d["category"],
        d["message"],
        e,
    ),
    "stream_token": lambda d, _e: protocol.result_chunk(d["token"]),
    "stream_done": lambda _d, _e: protocol.result_done(),
}

# Exceptional event kinds that should be persisted to DB (survive reconnects)
_PERSISTENT_EVENT_KINDS = frozenset(("retry", "recovery", "escalation", "warning"))


# ---------------------------------------------------------------------------
# Screenshot injection — ensures screenshots from web_fetch_rendered / web_screengrab
# appear in the chat even when the LLM summarises them away.
# ---------------------------------------------------------------------------

# Matches the hidden marker embedded in tool results by web_fetch_rendered/web_screengrab.
# The LLM never includes HTML comments in its text response, so this is a reliable
# side-channel that bypasses any LLM URL mangling.
_SCREENSHOT_MARKER_RE = re.compile(r"<!-- SCREENSHOT:(/api/screenshots/[^\s>]+) -->")


def _inject_screenshots(result_text: str, ctx) -> str:
    """Append screenshot thumbnails to *result_text* from hidden markers in tool results.

    Tools embed ``<!-- SCREENSHOT:/api/screenshots/filename.png -->`` in their
    output.  LLMs ignore HTML comments, so the path is never mangled.
    This function scans all tool results for these markers and appends any
    screenshots not already present in the LLM's response.
    """
    if not result_text:
        return result_text

    tool_screenshots: list[str] = []

    def _collect(text: str) -> None:
        for match in _SCREENSHOT_MARKER_RE.finditer(text):
            path = match.group(1)
            if path not in tool_screenshots:
                tool_screenshots.append(path)

    # Scan agent iterations metadata
    for it in ctx.metadata.get("agent_iterations", []):
        for tr in it.tool_results or []:
            _collect(str(tr.get("result", "")))

    # Scan tool-role messages in ctx.messages
    for msg in getattr(ctx, "messages", []) or []:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            _collect(str(msg.get("content", "")))

    # Scan ctx.tool_results shortcut
    for tr in getattr(ctx, "tool_results", []) or []:
        if isinstance(tr, dict):
            _collect(str(tr.get("result", "")))

    if not tool_screenshots:
        logger.debug("[_inject_screenshots] No screenshot markers found in tool results")
        return result_text

    # Strip any LLM-mangled screenshot references from the result text.
    # The LLM sometimes constructs broken absolute URLs like
    # "https://target-site.com/api/screenshots/file.png" by prepending the
    # target domain to the relative path.  Remove these so we can inject a
    # proper clickable thumbnail instead.
    cleaned = result_text
    for path in tool_screenshots:
        filename = path.rsplit("/", 1)[-1]  # e.g., "example.com_8f3be50bca.png"
        # Remove markdown image/link references containing the filename
        cleaned = re.sub(
            r"!?\[[^\]]*\]\([^)]*" + re.escape(filename) + r"[^)]*\)",
            "",
            cleaned,
        )
        # Remove bare URLs containing the screenshot filename
        cleaned = re.sub(
            r"https?://[^\s)]*" + re.escape(filename) + r"[^\s)]*",
            "",
            cleaned,
        )
    # Clean up any leftover blank lines from the removals
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()

    logger.info("[_inject_screenshots] Injecting %d screenshot(s): %s", len(tool_screenshots), tool_screenshots)
    parts = [cleaned, "", "---", ""]
    for path in tool_screenshots:
        parts.append(f"[![Screenshot]({path})]({path})")
    return "\n".join(parts)


def _make_agent_event_callback(
    send_sync: Callable[[dict], None],
    db: Any,
    session_id: str,
    total_start: float,
) -> Callable[[str, dict], None]:
    """Create an on_event callback for AgentLoop that streams progress to WS.

    Transient events (iteration, thinking, tool_exec) are sent over WS only.
    Exceptional events (retry, recovery, escalation, warning) are also persisted
    to the DB so they survive reconnects and show in restored history.
    """

    def _agent_event(kind: str, data: dict) -> None:
        builder = _AGENT_EVENT_BUILDERS.get(kind)
        if builder:
            elapsed = time.perf_counter() - total_start
            msg = builder(data, elapsed)
            send_sync(msg)
            if kind in _PERSISTENT_EVENT_KINDS:
                db.add_message(
                    session_id=session_id,
                    role="assistant",
                    msg_type=f"agent.{kind}",
                    content=data.get("message") or data.get("reason", ""),
                    metadata=msg,
                )

    return _agent_event


# ---------------------------------------------------------------------------
# Conversation memory helpers
# ---------------------------------------------------------------------------

# Matches one (or several stacked) "[YYYY-MM-DD HH:MM] " prefixes at the start
# of a stored message.  We strip these before re-annotating with a fresh
# timestamp so the prefix can never stack cumulatively across reloads even if
# the LLM copied a previous prefix verbatim into its own output.
_TS_PREFIX_RE = re.compile(r"^(?:\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*)+")

# Lines emitted by verified-write tools (code_edit, revert_file, revert_lines,
# write_file).  When such a message gets recalled as "memory context" the LLM
# can copy the shape verbatim and hallucinate a successful revert — we must
# never feed these markers back into the context window.
_VERIFIED_WRITE_MARKERS = (
    "✓ VERIFIED",
    "pre_hash=",
    "post_hash=",
    "snapshot_id=",
    "path=",
)


def _strip_ts_prefix(text: str) -> str:
    """Remove any leading ``[YYYY-MM-DD HH:MM] `` timestamp prefix(es)."""
    if not text:
        return text
    return _TS_PREFIX_RE.sub("", text)


def _sanitize_recalled_response(text: str) -> str:
    """Drop verified-write header lines from a recalled assistant response.

    Recalled memories must never become a template for fabricated tool
    results.  We remove any line starting with the well-known verified-write
    markers (after stripping any leading stacked timestamp prefixes on that
    line), leaving the natural prose summary behind.
    """
    if not text:
        return text
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        # Strip any stacked timestamp prefixes and leading whitespace so we
        # still catch marker lines that were annotated by the history loader.
        probe = _strip_ts_prefix(line.lstrip()).lstrip()
        if any(probe.startswith(m) for m in _VERIFIED_WRITE_MARKERS):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines).strip()
    return _strip_ts_prefix(cleaned)


def _recall_conversation_memory(query: str, session_id: str) -> list[dict[str, str]]:
    """Retrieve semantically relevant past exchanges and format as context.

    Returns a list with a single system message containing recalled memories,
    or an empty list if nothing relevant was found.
    """
    from .conversation_memory import get_conversation_memory

    mem = get_conversation_memory()
    if not mem:
        return []

    # Read max_age_days from config (default 14)
    try:
        from app.config import settings as _cfg

        max_age_days = _cfg.memory.semantic_max_age_days
    except Exception:
        max_age_days = 14

    try:
        memories = mem.recall(query, exclude_session=session_id, max_age_days=max_age_days)
    except Exception as exc:
        logger.debug("Conversation recall error: %s", exc)
        return []

    if not memories:
        return []

    # Format as a single system-role message.  The preamble makes it explicit
    # that this is REFERENCE ONLY — never a template to parrot back — so the
    # model still calls tools when the user asks for a file mutation.
    lines = [
        "[Relevant context from previous conversations — REFERENCE ONLY. "
        "This data may be days or weeks old. NEVER trust file contents, command "
        "outputs, or system state from this block. Always re-run tools to verify.]"
    ]
    for i, m in enumerate(memories, 1):
        lines.append(f"\n--- Memory {i} (score {m['score']:.2f}) ---")
        user_q = _strip_ts_prefix(m["query"])
        lines.append(f"User asked: {user_q}")
        # Truncate response to keep context budget reasonable AND strip any
        # verified-write markers so the LLM can't copy the shape.
        resp = _sanitize_recalled_response(m["response"])
        if not resp:
            # Nothing left after sanitization — skip this memory entirely
            lines.append("Assistant answered: (tool execution — details omitted)")
            continue
        if len(resp) > 400:
            resp = resp[:400] + "…"
        lines.append(f"Assistant answered: {resp}")

    return [{"role": "system", "content": "\n".join(lines)}]


# Tiered per-mode memory policy lives in web.server.memory_policy.
# The helpers below forward mode + incognito to that module so write /
# read behavior is controlled from a single source of truth.


def _store_conversation_exchange(
    session_id: str,
    query: str,
    response: str,
    mode: str = "",
    model: str = "",
    incognito: bool = False,
) -> None:
    """Store a completed exchange in conversation memory.  Fire-and-forget.

    Tier gate lives inside ``ConversationMemory.store_exchange``; we still
    short-circuit here to avoid importing memory/embedder for no-op calls.
    """
    from .memory_policy import should_store_conversation

    if not should_store_conversation(mode, incognito=incognito):
        return

    from .conversation_memory import get_conversation_memory

    mem = get_conversation_memory()
    if not mem:
        logger.warning("Conversation memory not initialised — skipping store")
        return

    try:
        mem.store_exchange(
            session_id=session_id,
            query=query,
            response=response,
            mode=mode,
            model=model,
            incognito=incognito,
        )
    except Exception as exc:
        logger.warning("Failed to store conversation exchange: %s", exc)


def _extract_facts_from_exchange(
    db: "ChatDatabase",
    session_id: str,
    query: str,
    response: str,
    mode: str = "",
    incognito: bool = False,
) -> None:
    """Extract structured facts from an exchange and store in SQLite.  Fire-and-forget."""
    try:
        from .fact_extraction import extract_and_store_facts

        extract_and_store_facts(
            db,
            session_id,
            query,
            response,
            mode=mode,
            incognito=incognito,
        )
    except Exception as exc:
        logger.debug("Fact extraction error: %s", exc)


def _get_instructions_for_context(db: "ChatDatabase", session_id: str) -> list[dict[str, str]]:
    """Load session + global instructions and format as a system message block.

    Returns a one-element list containing a system message, or [] if none exist.
    """
    try:
        instrs = db.get_session_instructions(session_id)
        if not instrs:
            return []
        lines = "\n".join(f"- {i.text}" for i in instrs)
        return [{"role": "system", "content": f"[Your Instructions]\n{lines}"}]
    except Exception as exc:
        logger.debug("Instruction context loading error: %s", exc)
        return []


def _get_facts_for_context(db: "ChatDatabase") -> list[dict[str, str]]:
    """Load known facts and format as a system message for context injection."""
    try:
        from .fact_extraction import get_relevant_facts_for_context

        # Read staleness threshold from config
        try:
            from app.config import settings as _cfg

            stale_days = _cfg.memory.fact_stale_days
        except Exception:
            stale_days = 30
        return get_relevant_facts_for_context(db, stale_days=stale_days)
    except Exception as exc:
        logger.debug("Fact context loading error: %s", exc)
        return []


def _store_last_exchange_from_db(
    db: "ChatDatabase",
    session_id: str,
    mode: str = "",
    model: str = "",
    incognito: bool | None = None,
) -> None:
    """Read the last query+result from the DB and store in conversation memory.

    Convenience wrapper so runners don't need to track local answer
    variables. Memory tier and incognito gating live inside
    ``_store_conversation_exchange`` / ``_extract_facts_from_exchange``
    via ``memory_policy``; this function forwards the flags and lets the
    policy decide.

    When ``incognito`` is ``None`` (the default for callers that haven't
    been updated), the flag is read back from the just-persisted result
    message's ``is_incognito`` column. That keeps every existing caller
    correct without threading the flag through every runner signature.
    """
    try:
        msgs = db.get_messages(session_id)
        # Walk backward to find the last result, then the query before it
        last_result = ""
        last_query = ""
        last_is_incognito = False
        for msg in reversed(msgs):
            if msg.type == "result" and msg.content and not last_result:
                last_result = msg.content
                last_is_incognito = bool(getattr(msg, "is_incognito", False))
            elif msg.type == "query" and msg.content and last_result and not last_query:
                last_query = msg.content
                break

        effective_incognito = bool(incognito) if incognito is not None else last_is_incognito

        if last_query and last_result:
            logger.info(
                "Storing conversation exchange for session %s (mode=%s, incognito=%s): query=%r response=%d chars",
                session_id,
                mode or "default",
                effective_incognito,
                last_query[:60],
                len(last_result),
            )
            _store_conversation_exchange(
                session_id,
                last_query,
                last_result,
                mode,
                model,
                incognito=effective_incognito,
            )
            _extract_facts_from_exchange(
                db,
                session_id,
                last_query,
                last_result,
                mode=mode,
                incognito=effective_incognito,
            )
        else:
            logger.warning(
                "No query+result pair found for session %s (%d messages, last_query=%r, last_result=%d chars)",
                session_id,
                len(msgs),
                last_query[:40],
                len(last_result),
            )
    except Exception as exc:
        logger.warning("_store_last_exchange_from_db failed: %s", exc)


# ---------------------------------------------------------------------------
# Importance scoring for sliding window
# ---------------------------------------------------------------------------

# Patterns indicating high-value content that should survive eviction
_IMPORTANCE_FILE_PATH = re.compile(r"~/|/[Uu]sers/|/home/|/tmp/|/var/|\.\w{1,5}\b")
_IMPORTANCE_CODE = re.compile(r"```|def |class |SELECT |INSERT |CREATE TABLE|DROP ")
_IMPORTANCE_SCHEMA = re.compile(r"\btable\b|\bcolumn\b|\bschema\b|\bdatabase\b|\bindex\b", re.IGNORECASE)
_IMPORTANCE_INSTRUCTION = re.compile(
    r"\balways\b|\bremember\b|\bprefer\b|\bdon.t\b|\bnever\b|\bimportant\b|\bmake sure\b",
    re.IGNORECASE,
)
_IMPORTANCE_TRIVIAL = re.compile(
    r"^(ok|okay|thanks|thank you|got it|sure|yes|no|cool|nice|great|perfect|lgtm|👍)\s*[.!]?$",
    re.IGNORECASE,
)


def _score_turns(turns: list[dict[str, str]]) -> list[dict]:
    """Score each turn for importance and annotate with ``_score`` and ``_pos``.

    Returns a new list of dicts (same keys as input + ``_score``, ``_pos``).
    Higher score = more worth keeping in the context window.
    """
    total = len(turns)
    scored = []
    for i, turn in enumerate(turns):
        score = 0
        content = turn["content"]

        # Recency bonus: last 6 messages get +2
        if i >= total - 6:
            score += 2

        # Content-based signals
        if _IMPORTANCE_FILE_PATH.search(content):
            score += 2
        if _IMPORTANCE_CODE.search(content):
            score += 3
        if _IMPORTANCE_SCHEMA.search(content):
            score += 2
        if _IMPORTANCE_INSTRUCTION.search(content):
            score += 4

        # Trivial message penalty — strip optional timestamp prefix before matching
        bare = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*", "", content.strip())
        if _IMPORTANCE_TRIVIAL.match(bare):
            score -= 2

        # Long assistant responses tend to be substantive
        if turn["role"] == "assistant" and len(content) > 500:
            score += 1

        scored.append({**turn, "_score": score, "_pos": i})

    return scored


# Per-message char caps for history truncation. Tuned so:
# - User queries: usually < 200 chars; 800 is plenty even for verbose ones
# - Assistant results: long YAML reviews / log dumps used to cause topic
#   bleed when the prior turn was on a different subject; 1500 chars
#   keeps the "what was discussed" essence without dominating attention
_HISTORY_USER_MSG_CAP_CHARS = 800
_HISTORY_ASSISTANT_MSG_CAP_CHARS = 1500
_HISTORY_TRUNC_MARKER = (
    "\n\n[…response truncated for context — {omitted} chars omitted. Ask 'show full previous response' to retrieve.]"
)


def _truncate_history_messages(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Cap each prior history message in place so a long previous result
    doesn't dominate LLM attention on a topic-shift follow-up.

    Truncates from the end (preserves the start of the response, which
    usually has the heading / verdict / first paragraph — the most
    useful context for follow-up linking). The CURRENT user query is
    appended downstream and never seen here.
    """
    out: list[dict[str, str]] = []
    for msg in messages:
        content = msg.get("content", "") or ""
        cap = _HISTORY_ASSISTANT_MSG_CAP_CHARS if msg.get("role") == "assistant" else _HISTORY_USER_MSG_CAP_CHARS
        if len(content) > cap:
            omitted = len(content) - cap
            content = content[:cap] + _HISTORY_TRUNC_MARKER.format(omitted=omitted)
        out.append({"role": msg.get("role", "user"), "content": content})
    return out


def _build_conversation_history(
    db: ChatDatabase,
    session_id: str,
    incognito: bool = False,
    query: str = "",
    mode: str = "",
) -> list[dict[str, str]] | None:
    """Reconstruct conversation history from DB for the search pipeline's memory.

    Returns a list of {role, content} dicts suitable for passing to
    agentforge's response_refiner (which supports conversation_history).
    Returns None if no history exists.

    **Incognito isolation:**
    - When ``incognito=True``: loads ONLY ``is_incognito`` messages from the
      current session (same-session follow-ups work).  Cross-session semantic
      memory and fact injection are skipped entirely.
    - When ``incognito=False``: loads only non-incognito messages (original
      behaviour).  Incognito messages never leak into normal context.

    When ``query`` is provided and conversation memory is enabled, semantically
    relevant past exchanges (potentially from other sessions) are prepended as
    a ``[Memory]`` system message before the sliding window history.
    """
    try:
        db_messages = db.get_messages(session_id)
    except Exception:
        logger.debug("Could not load history for session %s", session_id)
        return None

    db_messages = [m for m in db_messages if not getattr(m, "is_volatile", False)]

    if incognito:
        # Private mode: include ONLY incognito messages from this session.
        # This allows within-session follow-ups while preventing cross-session
        # leakage.  Semantic memory + facts are skipped below.
        db_messages = [m for m in db_messages if m.is_incognito]
    else:
        # Normal mode: permanently exclude incognito messages — regardless of
        # the current toggle state.  This is the DB-level isolation guarantee:
        # once a message was sent during an incognito session it never leaks
        # back into the conversation history even after the user turns
        # incognito off.
        db_messages = [m for m in db_messages if not m.is_incognito]

    # Extract conversation turns: query → user, result → assistant
    # Each turn is annotated with a timestamp prefix so the LLM can reason
    # about when messages were sent (e.g., "when was my last message?",
    # "how long did we spend on X?").
    #
    # IMPORTANT: we strip any existing "[YYYY-MM-DD HH:MM] " prefix from the
    # stored content before prepending a fresh one.  Without this, models
    # that parroted a previous timestamp back into their own reply would
    # cause cumulative stacking (`[23:43]`, `[23:43] [23:43]`, …) every time
    # the history was reloaded.  Stripping first makes the annotation
    # idempotent.
    turns: list[dict[str, str]] = []
    for msg in db_messages:
        ts_prefix = ""
        _ts = getattr(msg, "created_at", None)
        if _ts:
            ts_prefix = f"[{_ts.strftime('%Y-%m-%d %H:%M')}] "
        content = _strip_ts_prefix(msg.content) if msg.content else msg.content
        if msg.type == "query" and content:
            turns.append({"role": "user", "content": f"{ts_prefix}{content}"})
        elif msg.type == "result" and content:
            turns.append({"role": "assistant", "content": f"{ts_prefix}{content}"})

    # --- Semantic recall --------------------------------------------------
    # Retrieve relevant past exchanges from conversation memory and prepend
    # them as a system context block.  Gated by memory_policy — only FULL
    # tier modes (@docs, default chat, @pipeline) recall; SESSION tier
    # (@agent, @research, @sql, …) and NONE tier (@cloud, @monitor, …)
    # skip the injection entirely, preventing stale context from steering
    # the model toward a different query / pattern.
    from .memory_policy import should_inject_facts, should_recall_conversation

    memory_prefix: list[dict[str, str]] = []
    if query and should_recall_conversation(mode, incognito=incognito):
        memory_prefix = _recall_conversation_memory(query, session_id)

    # --- Session instructions -----------------------------------------------
    # User-authored instructions via #remember — injected first, highest priority.
    # Skip for incognito: instructions persist to SQLite and must not leak.
    instructions_prefix = [] if incognito else _get_instructions_for_context(db, session_id)

    # --- Fact injection ---------------------------------------------------
    # Facts inject for FULL and SESSION tiers (volatile NONE-tier modes
    # skip to avoid preferences leaking into audit-sensitive outputs).
    facts_prefix = _get_facts_for_context(db) if should_inject_facts(mode, incognito=incognito) else []

    if not turns and not memory_prefix and not facts_prefix and not instructions_prefix:
        return None

    # --- Importance-weighted sliding window ---------------------------------
    # Instead of pure recency, score each turn pair and keep the highest-value
    # messages within the token budget.  The most recent 3 turns always survive
    # (recency guarantee), then remaining budget is filled by importance score.
    scored_turns = _score_turns(turns)

    # Always keep the last 6 messages (3 turns) for immediate context
    _ALWAYS_KEEP_RECENT = 6
    recent = scored_turns[-_ALWAYS_KEEP_RECENT:]
    candidates = scored_turns[:-_ALWAYS_KEEP_RECENT] if len(scored_turns) > _ALWAYS_KEEP_RECENT else []

    # Budget consumed by the always-kept recent turns
    token_count = sum(len(t["content"]) // _CHARS_PER_TOKEN for t in recent)

    # Fill remaining budget with highest-scored older turns
    candidates.sort(key=lambda t: t.get("_score", 0), reverse=True)
    bonus: list[dict[str, str]] = []
    for turn in candidates:
        turn_tokens = len(turn["content"]) // _CHARS_PER_TOKEN
        if token_count + turn_tokens > _MAX_HISTORY_TOKENS:
            continue
        if len(recent) + len(bonus) >= _MAX_HISTORY_TURNS * 2:
            break
        bonus.append(turn)
        token_count += turn_tokens

    # Merge and sort by original position to maintain chronological order
    all_selected = bonus + recent
    all_selected.sort(key=lambda t: t.get("_pos", 0))

    # Strip internal scoring keys
    selected = [{"role": t["role"], "content": t["content"]} for t in all_selected]

    # Per-message truncation — cap each prior message so a long previous
    # assistant result doesn't dominate the LLM's attention on the
    # follow-up turn. Without this, a 3 KB YAML review from the previous
    # turn made small models (devstral-small-2 etc.) re-summarise the
    # prior topic when the user asked about a different file. Caps are
    # generous — short follow-up references like "the highest one from
    # the previous list" still have enough surrounding text to land
    # correctly. The CURRENT user query is appended downstream (see
    # _run_agent's history_messages.append at the dispatch site) and
    # NEVER gets truncated here.
    selected = _truncate_history_messages(selected)

    # Ensure we start with a user message
    while selected and selected[0]["role"] == "assistant":
        selected.pop(0)

    if selected:
        logger.info(
            "Loaded %d history message(s) for session %s (~%d tokens)",
            len(selected),
            session_id,
            token_count,
        )

    # Prepend instructions + facts + semantic memory recall before the sliding window
    # Order: [instructions] → [facts] → [memory] → [conversation history]
    result = instructions_prefix + facts_prefix + memory_prefix + (selected or [])
    return result or None


# ---------------------------------------------------------------------------
# Context-window usage tracking
# ---------------------------------------------------------------------------

# Approximate context-window sizes (tokens) for known models.
# Falls back to 131 072 for unknown models (most cloud models support 128K+).
_MODEL_CONTEXT_SIZES: dict[str, int] = {
    "devstral-small-2": 32_768,
    "qwen2.5-coder": 32_768,
    "qwen3-coder-next": 131_072,
    "mistral-large-3": 131_072,
    "ministral-3": 131_072,
    "mistral-small-3.2": 131_072,
    # New models
    "minimax-m2.5": 196_608,  # 198K context
    "glm-5": 196_608,  # 198K context (DSA-optimized)
    "nemotron-3-super": 262_144,  # 256K context
    "nemotron-3-nano": 1_048_576,  # 1M context
    "nemotron-cascade-2": 262_144,  # 256K context
}
_DEFAULT_CONTEXT_SIZE = 131_072


def _model_context_size(model: str) -> int:
    """Return the context-window size for *model* (best-effort lookup)."""
    base = model.split(":")[0]  # "devstral-small-2:24b-cloud" → "devstral-small-2"
    if base in _MODEL_CONTEXT_SIZES:
        return _MODEL_CONTEXT_SIZES[base]
    # Partial prefix match (e.g., "mistral-large" in "mistral-large-3")
    for key, size in _MODEL_CONTEXT_SIZES.items():
        if key in base or base in key:
            return size
    return _DEFAULT_CONTEXT_SIZE


def _estimate_session_tokens(db: "ChatDatabase", session_id: str) -> tuple[int, int]:
    """Return (estimated_tokens, message_count) for a session's full DB history."""
    try:
        db_messages = db.get_messages(session_id)
    except Exception:
        return 0, 0

    total_chars = 0
    count = 0
    for msg in db_messages:
        count += 1
        if msg.content:
            total_chars += len(msg.content)
        if msg.metadata_json:
            total_chars += len(msg.metadata_json) // 3  # metadata is JSON, lower density
        if msg.tool_calls_json:
            total_chars += len(msg.tool_calls_json) // 3
    return total_chars // _CHARS_PER_TOKEN, count


async def _send_secret_redaction_warning(
    ws: "WebSocket",
    client: Any,
) -> None:
    """If the last AIClient call redacted secrets, warn the user via WS.

    Reads ``client.last_redaction`` and emits a ``secret.redacted`` event if
    there were findings.  Safe to call even when the client has no
    ``last_redaction`` attribute (e.g., older code paths).
    """
    try:
        redaction = getattr(client, "last_redaction", None)
        if redaction and redaction.had_secrets:
            types = list({f.secret_type for f in redaction.findings})
            await ws.send_json(
                protocol.secret_redacted(
                    count=len(redaction.findings),
                    secret_types=types,
                )
            )
    except Exception:
        pass  # never fail the run because of a WS warning


async def _send_context_usage(
    ws: "WebSocket",
    db: "ChatDatabase",
    session_id: str,
    model: str,
) -> None:
    """Estimate context usage and send a context.usage event to the client."""
    used, msg_count = _estimate_session_tokens(db, session_id)
    capacity = _model_context_size(model)
    pct = min((used / capacity) * 100, 100.0) if capacity > 0 else 0.0

    # Always include token_usage from DB so the client can show "~" for
    # models that don't report counts (Ollama) and real numbers for those that do (Bedrock).
    real_usage: dict[str, int] | None = None
    try:
        session = db.get_session(session_id)
        if session:
            sd = session.to_dict() if hasattr(session, "to_dict") else {}
            real_usage = {
                "prompt_tokens": sd.get("prompt_tokens", 0) or 0,
                "completion_tokens": sd.get("completion_tokens", 0) or 0,
                "total_tokens": sd.get("total_tokens", 0) or 0,
            }
    except Exception:
        pass

    try:
        msg = protocol.context_usage(
            used_tokens=used,
            max_tokens=capacity,
            percent=pct,
            message_count=msg_count,
        )
        if real_usage is not None:
            msg["token_usage"] = real_usage
        await ws.send_json(msg)
    except Exception:
        pass  # WS may have closed


def _persist_token_usage(
    db: "ChatDatabase",
    session_id: str,
    ctx: Any,
) -> None:
    """Persist accumulated token usage from a PipelineContext to the session.

    Reads ``ctx.metadata["token_usage"]`` (set by the agent loop) and
    atomically increments the session counters.  No-op if no usage data.
    """
    usage = (ctx.metadata or {}).get("token_usage") if ctx else None
    if not usage:
        return
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    if prompt > 0 or completion > 0:
        try:
            db.add_token_usage(session_id, prompt, completion)
            logger.debug(
                "Token usage persisted for session %s: +%d prompt, +%d completion",
                session_id[:12],
                prompt,
                completion,
            )
        except Exception:
            logger.debug("Failed to persist token usage", exc_info=True)


def _ctx_tokens(ctx: Any) -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) from a PipelineContext's token_usage metadata."""
    usage = (ctx.metadata or {}).get("token_usage", {}) if ctx else {}
    return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _persist_token_usage_raw(
    db: "ChatDatabase",
    session_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Persist raw token counts directly (for runners without PipelineContext)."""
    if prompt_tokens > 0 or completion_tokens > 0:
        try:
            db.add_token_usage(session_id, prompt_tokens, completion_tokens)
            logger.debug(
                "Token usage persisted for session %s: +%d prompt, +%d completion",
                session_id[:12],
                prompt_tokens,
                completion_tokens,
            )
        except Exception:
            logger.debug("Failed to persist token usage", exc_info=True)


async def _compact_session(
    ws: "WebSocket",
    db: "ChatDatabase",
    session_id: str,
    rt: "SearchRuntime",
) -> None:
    """Summarise session history via LLM and replace messages with a compact block."""
    try:
        db_messages = db.get_messages(session_id)
    except Exception:
        logger.warning("Compact: could not load messages for %s", session_id)
        await ws.send_json(protocol.agent_error("Could not load session history"))
        return

    # Build a text representation of the conversation
    conversation_lines: list[str] = []
    for msg in db_messages:
        if msg.type == "query" and msg.content:
            conversation_lines.append(f"User: {msg.content}")
        elif msg.type == "result" and msg.content:
            # Truncate very long results
            text = msg.content[:2000] + "..." if len(msg.content) > 2000 else msg.content
            conversation_lines.append(f"Assistant: {text}")
        elif msg.type == "tool_calls" and msg.tool_calls_json:
            try:
                calls = json.loads(msg.tool_calls_json) if isinstance(msg.tool_calls_json, str) else msg.tool_calls_json
                names = [c.get("name", "?") for c in (calls or [])]
                conversation_lines.append(f"Tools called: {', '.join(names)}")
            except Exception:
                pass

    if not conversation_lines:
        await ws.send_json(protocol.agent_error("No conversation to compact"))
        return

    conversation_text = "\n".join(conversation_lines[-60:])  # last 60 entries max

    compact_prompt = (
        "Summarise this conversation into a concise bullet-point list. "
        "Include: key topics discussed, important results/answers, "
        "tool operations performed, file paths created or modified, "
        "and any pending tasks or open questions. "
        "Be factual and specific — preserve names, numbers, and paths.\n\n"
        f"CONVERSATION:\n{conversation_text}"
    )

    # Use a fast/cheap model for summarisation. Wrapped in retry_call so
    # a transient 5xx from the cloud-light backend doesn't lose the whole
    # compaction pass and strand the user at their 90% context ceiling.
    try:
        from agentforge.backends._retry import retry_call
        from agentforge.client import AIClient

        summariser = AIClient(profile="cloud-light")

        def _compact_call():
            return summariser.chat(
                messages=[
                    {"role": "system", "content": "You are a concise conversation summariser."},
                    {"role": "user", "content": compact_prompt},
                ],
            )

        response = retry_call(_compact_call, max_attempts=3, context="compact-summary")
        summary = response.content.strip() if hasattr(response, "content") else ""
        if not summary:
            summary = "(Summary generation returned empty result)"
    except Exception as exc:
        logger.warning("Compact: LLM summarisation failed after retries: %s", exc)
        summary = f"(Summary failed: {exc})"

    # Phase D: extract structured facts from the FULL conversation before deleting.
    # This preserves key knowledge (preferences, system details, decisions) in the
    # user_facts table so they survive compaction and remain available to future sessions.
    try:
        from .fact_extraction import extract_and_store_facts

        # Build combined conversation text for extraction
        full_query = " | ".join(msg.content[:200] for msg in db_messages if msg.type == "query" and msg.content)
        full_response = " | ".join(msg.content[:300] for msg in db_messages if msg.type == "result" and msg.content)
        n_facts = extract_and_store_facts(db, session_id, full_query[:1500], full_response[:2000])
        if n_facts:
            logger.info("Compact: extracted %d fact(s) before compaction for session %s", n_facts, session_id[:12])
    except Exception as exc:
        logger.warning("Compact: fact extraction before compaction failed: %s", exc)

    # Delete old messages from DB and insert the compact summary

    compact_date = _date.today().isoformat()
    compact_header = (
        f"[Session compacted on {compact_date}. Information below reflects state at that time and may be outdated.]"
    )
    compact_text = f"**Session Summary (compacted)**\n\n{compact_header}\n\n{summary}"
    try:
        db.delete_messages(session_id)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="result",
            content=compact_text,
            metadata={"type": "result", "text": compact_text, "compacted": True},
        )
    except Exception as exc:
        logger.warning("Compact: DB cleanup failed: %s", exc)
        await ws.send_json(protocol.agent_error(f"Compaction DB error: {exc}"))
        return

    # Notify client
    await ws.send_json(protocol.session_compacted(summary=summary))
    logger.info("Session %s compacted — %d messages → 1 summary + facts preserved", session_id, len(db_messages))


# ---------------------------------------------------------------------------
# Job queue helpers — enqueue to the worker and poll for results
# ---------------------------------------------------------------------------

# Modes that run via the SAQ worker (long-running, tool-based)
_WORKER_MODES = frozenset(
    (
        "agent",
        "web_search",
        "logs",
        "sql",
        "discover",
        "pipeline",
        "review",
        "research",
        "coding",
    )
)

_POLL_INTERVAL = 0.5  # seconds between job-status polls while waiting for worker


def _is_worker_mode(mode: str) -> bool:
    """Return True if this mode should be dispatched to the SAQ worker.

    Connector agents (custom:connector:* and the aggregated custom:connector-account:*)
    run inline because their tools are closures bound to ConnectionManager in the
    agentforge-web process — a worker has no access to them.
    """
    if mode.startswith(("custom:connector:", "custom:connector-account:")):
        return False
    return mode in _WORKER_MODES or mode.startswith("custom:")


def _is_initial_prompt(db, session_id: str | None) -> bool:
    """True when this is a session's opening prompt (no assistant turn yet).

    Gates opening-prompt refinement so follow-up turns aren't rewritten. Uses
    "no assistant message yet" as a persistence-timing-robust proxy for "first
    prompt". Never raises — a history read error just treats it as initial.
    """
    if not session_id:
        return True
    try:
        return not any(getattr(m, "role", "") == "assistant" for m in db.get_messages(session_id))
    except Exception:  # noqa: BLE001 — never block a query on a history read
        return True


async def _enqueue_job(
    session_id: str,
    query: str,
    mode: str,
    overrides: dict | None = None,
) -> str:
    """Create a job in the store, register it, and enqueue via dispatch_compat.

    Cancellation flows through agentforge-web's job_store HTTP endpoint, which
    SAQ's SaqCancelEvent polls.
    """
    from .queue.dispatch_compat import enqueue_agent_job

    job_id = uuid.uuid4().hex

    # Register in job_store so the reconnect logic and cancellation work.
    job_store.create_job(
        job_id=job_id,
        session_id=session_id,
        query=query,
        mode=mode,
        overrides=overrides,
    )

    # Pass all data as task args — the worker does NOT read job_store.db.
    overrides_json = json.dumps(overrides) if overrides else None
    enqueue_agent_job(job_id, session_id, query, mode, overrides_json)
    logger.info("Enqueued job %s (session=%s, mode=%s)", job_id, session_id, mode)
    return job_id


async def _wait_job_done(
    session_id: str,
    job_id: str,
) -> None:
    """Wait for the worker job to reach a terminal state.

    Worker messages arrive in real-time via the /internal/sessions/{id}/event
    HTTP endpoint (pushed by HttpCallbackSocket) — no polling needed for
    message forwarding.  This task just keeps track of job lifecycle so the
    WS handler knows when the run is finished.
    """
    logger.info("Waiting for job %s (session=%s)", job_id[:8], session_id)
    while True:
        job = job_store.get_job(job_id)
        if job and job.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
            logger.info("Job %s finished with status=%s", job_id[:8], job.status.value)
            return
        await asyncio.sleep(_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


@router.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket, session_id: str | None = None) -> None:
    # Optional API-key auth (off unless security.api_keys is set). When a key is
    # supplied via Sec-WebSocket-Protocol it must be echoed back on accept().
    from app.security import negotiate_ws

    _ws_authorized, _ws_subprotocol = negotiate_ws(ws)
    if not _ws_authorized:
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept(subprotocol=_ws_subprotocol)

    # Wait for background runtime init (started in app.py lifespan).
    # Normally completes within a few seconds; this timeout is a hard guard
    # (service.startup_timeout_seconds in config.yaml, default 120s).
    if not _runtime_ready.is_set():
        logger.info("WebSocket waiting for SearchRuntime to finish initialising…")
        from app.config import settings as _af_settings

        try:
            await asyncio.wait_for(_runtime_ready.wait(), timeout=_af_settings.startup_timeout_seconds)
        except asyncio.TimeoutError:
            await ws.send_json(
                {"type": "agent.error", "error": "Server is still starting up — please reconnect in a moment."}
            )
            await ws.close()
            return

    rt = get_runtime()
    db = get_db()
    broker = ConfirmationBroker()
    secret_broker = SecretBroker()
    loop = asyncio.get_event_loop()

    # Resolve session: use provided ID if it exists in DB, otherwise defer creation
    existing_session = None
    if session_id:
        existing_session = db.get_session(session_id)
        if not existing_session:
            session_id = None

    # Thread-safe sender for agent callbacks
    def send_sync(msg: dict) -> None:
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    broker.set_sender(send_sync)
    secret_broker.set_sender(send_sync)

    # Send session.init with real tool count + stamped provider override
    # (NULL on a fresh connection where we haven't created a session yet).
    _initial_provider = existing_session.provider_override if existing_session else None
    await ws.send_json(
        protocol.session_init(
            session_id=session_id or "",
            tools=rt.tool_count,
            profiles=rt.profiles,
            canvas_enabled=_canvas_db is not None,
            provider_override=_initial_provider,
        )
    )

    # Register this WebSocket so the internal event endpoint can broadcast
    # worker messages to this client in real-time.
    from . import state

    if session_id:
        state.active_ws[session_id] = ws

    # Send active instructions so the client can show the badge on reconnect.
    if session_id:
        try:
            existing_instrs = db.get_session_instructions(session_id)
            if existing_instrs:
                await ws.send_json(protocol.instructions_list([i.to_dict() for i in existing_instrs]))
        except Exception:
            pass

    # -- Cancel infrastructure -----------------------------------------------
    # cancel_event: a threading.Event shared with runners; when set, runners
    # check it between iterations/commands and bail out early.
    # active_task: the asyncio.Task currently running the agent/discovery/search.
    cancel_event: threading.Event | None = None
    active_task: asyncio.Task | None = None

    # Track last query mode for sticky follow-ups.
    # When resuming a session, recover from message history so sticky mode
    # survives WebSocket reconnections.
    last_mode = "chat"
    last_skills: list[dict] = []  # skills from previous turn (for condensed injection)
    if existing_session and session_id:
        try:
            recent_msgs = db.get_messages(session_id)
            # Walk backwards: find the most recent routing message to recover
            # the exact mode (agent, web_search, logs, search).
            for msg in reversed(recent_msgs):
                if msg.type == "routed" and msg.metadata:
                    # Routing messages store the mode in metadata
                    routed_reason = msg.metadata.get("reason", "")
                    if "@logs mode" in routed_reason:
                        last_mode = "logs"
                    elif "@search mode" in routed_reason or "web search" in routed_reason.lower():
                        last_mode = "web_search"
                    elif "@scheduler mode" in routed_reason or "scheduler" in routed_reason.lower():
                        last_mode = "scheduler"
                    elif "@monitor mode" in routed_reason or "monitor" in routed_reason.lower():
                        last_mode = "monitor"
                    else:
                        last_mode = "agent"
                    break
                if msg.type == "tool_calls":
                    last_mode = "agent"
                    break
                if msg.type in ("result", "query"):
                    # Check if this was a query with a mode prefix
                    if msg.type == "query" and msg.metadata:
                        qmode = msg.metadata.get("mode", "")
                        if qmode in ("logs", "web_search", "agent", "scheduler", "monitor", "research"):
                            last_mode = qmode
                            break
                    break  # hit a result/query before any routing → search
        except Exception:
            pass  # keep default "search"

    # Track active job ID for this WS connection (set when we enqueue)
    active_job_id: str | None = None

    logger.info(
        "WebSocket connected — session %s (%s, %d tools)",
        session_id or "(new)",
        "resumed" if existing_session else "fresh",
        rt.tool_count,
    )

    # -- Reconnect to active worker job if one exists --------------------------
    # Worker messages now arrive via HTTP push (HttpCallbackSocket → /internal/
    # sessions/{id}/event) which broadcasts to state.active_ws.  Registering
    # this WS above already means future messages will be forwarded.
    # We just need to resume the "wait for job done" task so the WS handler
    # stays aware of when the run finishes.
    if existing_session and session_id:
        active_job = job_store.get_active_job(session_id)
        if active_job:
            logger.info(
                "Reconnecting to active job %s (status=%s) for session %s",
                active_job.job_id,
                active_job.status.value,
                session_id,
            )
            active_job_id = active_job.job_id
            active_task = asyncio.create_task(_wait_job_done(session_id, active_job_id))

    # -- Replay ephemeral UI events from the Redis buffer ---------------------
    # tool.call / tool.calls.flush / research.progress / research.activity are
    # broadcast-only (not persisted to SQLite). The session_event_buffer keeps
    # the last ~500 of them per session in Redis for 1h so that a page reload
    # DURING a run restores the ToolCallsPanel / research activity view
    # without DB bloat.
    #
    # Gate on an active job: when the run is already finished, the DB has the
    # durable record (result row carries embedded tool_calls), and replaying
    # the buffered tool.call events would build a SECOND ToolCallsPanel on
    # top of the one restoreMessages already produced from the DB → duplicate
    # panels visible on every URL reload until the buffer's 1h TTL expires.
    if session_id and job_store.get_active_job(session_id) is not None:
        try:
            from .session_event_buffer import get_session_event_buffer

            buffered = await get_session_event_buffer().replay(session_id)
            # Drop confirm.request from replay — new runs don't buffer it
            # anymore (see api._BROADCAST_NO_REPLAY_TYPES), but sessions
            # buffered before that fix landed would otherwise keep
            # re-showing the dialog on every reload until their 1 h TTL
            # expires. Filtering here catches those stale entries too.
            _NO_REPLAY = {
                "confirm.request",
                "confirm.response",
                "secret.request",
                "secret.response",
            }
            buffered = [e for e in buffered if e.get("type") not in _NO_REPLAY]
            if buffered:
                logger.info(
                    "Replaying %d buffered UI events for session %s",
                    len(buffered),
                    session_id,
                )
                for evt in buffered:
                    try:
                        await ws.send_json(evt)
                    except Exception:
                        break  # client likely gone mid-replay; nothing to do

                # After replay, if there's no active job right now, send a
                # run.idle marker so the client clears any "working" indicator
                # that was re-armed by replayed tool.call events. Without this
                # the pulsating dot stays on forever after reloading a page
                # whose last run has already completed.
                try:
                    if job_store.get_active_job(session_id) is None:
                        await ws.send_json(protocol.run_idle())
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Event buffer replay failed for %s: %s", session_id, exc)

    async def _process_query(text: str, attachments: list, overrides):
        """Run a single query through the full classification + dispatch pipeline.

        Extracted so the `query.retry` branch can re-enter the same code path
        after trimming the transcript.  Closes over runtime state from
        ``chat_ws`` via ``nonlocal``.
        """
        nonlocal active_task, cancel_event, last_mode, last_skills, active_job_id

        # Apply the session's per-query provider override (if any) into the
        # ContextVar before any AIClient is constructed downstream. The var
        # propagates through asyncio.create_task and run_in_executor, so all
        # ~25 AIClient sites in the framework + ws_endpoint pick it up
        # without per-call plumbing. Reset is safe because each WS message
        # runs as its own task — the contextvar value is task-scoped.
        _session_provider: str | None = None
        _session_source: str | None = None
        if session_id:
            try:
                _sess_row = db.get_session(session_id)
                if _sess_row is not None:
                    _session_provider = _sess_row.provider_override
                    _session_source = getattr(_sess_row, "source", None)
            except Exception as exc:
                logger.debug("Could not read session provider_override: %s", exc)
        set_request_provider_override(_session_provider)
        # Carry the session id so a tool dispatched to a worker can prompt the
        # user (e.g., for a sudo password) back through this session's WS.
        set_request_session_id(session_id)

        # Per-external-app role overrides (app_provider_role_mapping in
        # framework-config.yaml), keyed by the session source tag (e.g., "felix").
        # Layers app-specific role->model picks over the active provider's map
        # for this request only. Same contextvar propagation as the provider
        # override above, so it reaches the agent loop, guard, and retry.
        try:
            from agentforge.config import get_config as _get_fw_config

            _app_maps = _get_fw_config()._app_role_map
            _effective_provider = _session_provider or _get_fw_config()._provider_override
            _app_role_map = (_app_maps.get(_session_source) or {}).get(_effective_provider) if _session_source else None
            set_request_role_override_map(_app_role_map or None)
        except Exception as exc:  # noqa: BLE001 — never let mapping break a query
            logger.debug("Could not apply app role override map: %s", exc)
            set_request_role_override_map(None)

        # ── #remember / #forget — dynamic session instructions ───────
        # Intercept before mode classification so these never reach an
        # agent.  Persisted immediately to SQLite; takes effect on the
        # very next LLM call.
        if _REMEMBER_RE.match(text):
            await _handle_remember(ws, db, session_id, text)
            return
        if _FORGET_RE.match(text):
            await _handle_forget(ws, db, session_id, text, broker=broker, secret_broker=secret_broker)
            return

        # Strip mode prefix (@agent/@search/...) if present
        clean_text, forced_mode = _strip_mode_prefix(text)

        # Classify: search or agent?
        mode = await _classify_mode(text, rt, last_mode=last_mode, db=db, session_id=session_id)
        last_mode = mode  # remember for sticky follow-ups
        logger.info("Query mode: %s — %r", mode, text[:80])

        # ── Unknown @-prefix short-circuit ───────────────────────────────
        # When the user types an @-prefix that doesn't match any built-in
        # mode or custom agent alias, the classifier returns the sentinel
        # "unknown_prefix" instead of silently routing to search (which
        # used to make typos like @cooding look like "search found no
        # relevant results"). Surface the error explicitly with the list
        # of available modes so the typo is fixable from the result card.
        if mode == "unknown_prefix":
            # Pull the bad prefix from the start of the query for the error message

            _m = _re.match(r"\s*(@[A-Za-z0-9_-]+)", text)
            bad_prefix = _m.group(1) if _m else "@?"
            # Build the list of valid prefixes — keep this in sync with
            # _strip_mode_prefix / framework.intent_classifier._PREFIX_MAP.
            built_in = (
                "@chat, @agent, @docs/@qdrant, @search/@web, @logs, "
                "@discover, @sql, @pipeline, @scheduler, @monitor, "
                "@review, @research, @coding/@code"
            )
            custom_aliases = sorted(("@" + a) for a in (getattr(rt, "custom_agents", {}) or {}).keys())
            custom_str = ", ".join(custom_aliases) if custom_aliases else "(none)"
            msg = (
                f"**No such mode `{bad_prefix}`.** Did you mean one of these?\n\n"
                f"- Built-in: {built_in}\n"
                f"- Custom agents: {custom_str}\n\n"
                "If you didn't mean an @-prefix, drop the `@` and let the "
                "router pick a mode based on the prompt itself."
            )
            db.add_message(
                session_id=session_id,
                role="user",
                msg_type="query",
                content=text,
                metadata={"type": "query", "text": text},
                is_incognito=(overrides or {}).get("incognito", False),
            )
            await ws.send_json(protocol.agent_result(text=msg, elapsed=0.0))
            db.add_message(
                session_id=session_id,
                role="assistant",
                msg_type="result",
                content=msg,
                metadata=protocol.agent_result(text=msg, elapsed=0.0),
                is_incognito=(overrides or {}).get("incognito", False),
            )
            await ws.send_json(
                protocol.agent_summary(
                    iterations=1,
                    elapsed=0.0,
                    tool_calls=0,
                    tools={},
                )
            )
            # Reset last_mode so the next non-prefix follow-up doesn't try
            # to sticky-route on this error.
            last_mode = "chat"
            return

        # Use cleaned text (prefix stripped) for execution.
        # For custom agents, strip the custom alias from the query.
        if mode.startswith("custom:"):
            exec_text, _ = _strip_custom_prefix(text, rt)
        else:
            exec_text = clean_text if forced_mode else text

        # ── Skill resolution ──────────────────────────────────
        exec_text, matched_skills, skill_promoted_mode = _resolve_skills(exec_text, rt, mode)

        # Auto-promote mode when a skill alias requires a different
        # mode than what _classify_mode chose (e.g., @api-design in a
        # chat-classified query promotes to "agent").
        if skill_promoted_mode and matched_skills:
            logger.info(
                "Skill alias promoted mode: %s → %s",
                mode,
                skill_promoted_mode,
            )
            mode = skill_promoted_mode
            last_mode = mode

        # Replace routing hashtags (#myservice, #mytag, ...) with
        # natural-language equivalents when the mode is NOT @cloud. The
        # hashtags are routing markers, not topic tags — leaving them in the
        # query confuses non-cloud LLMs (qwen3.5 has been seen to read
        # `#myservice` as an SSH host alias and call ssh against it).
        exec_text = _normalize_cloud_hashtags(exec_text, mode)

        # ── Optional opening-prompt refinement ───────────────────────
        # Rewrite the user's first prompt for clarity/grammar before it runs —
        # only for the LLM-prompt modes (chat / agent / custom agents), and only
        # on the session's opening prompt. Search/RAG modes refine their own
        # query for embedding, so they're excluded. Off unless prompt_refinement
        # is enabled in config.yaml. The enabled check short-circuits first, so
        # there's no history read or refiner call on the common (disabled) path.
        from .prompt_refiner import is_prompt_refinement_enabled, refine_prompt

        if (
            is_prompt_refinement_enabled()
            and (mode in ("chat", "agent") or mode.startswith("custom:"))
            and _is_initial_prompt(db, session_id)
        ):
            _refined = await refine_prompt(exec_text)
            if _refined.changed:
                exec_text = _refined.refined
                try:
                    await ws.send_json(protocol.prompt_refined(_refined.original, _refined.refined))
                except Exception:  # noqa: BLE001 — surfacing is best-effort
                    logger.debug("could not send prompt.refined event", exc_info=True)

        # If no explicit skills but last turn had skills and mode is
        # sticky, carry them forward in condensed mode.
        is_condensed = False
        if not matched_skills and last_skills and mode == last_mode:
            matched_skills = last_skills
            is_condensed = True
        # Merge any client-supplied skills (e.g., Felix's retrieved fleet) with
        # the server's keyword matches so client skills are never clobbered.
        client_skills = (overrides or {}).get("_skills") or []
        combined = _merge_skills(client_skills, matched_skills)
        if combined:
            overrides = dict(overrides or {})
            overrides["_skills"] = combined
            overrides.setdefault("_skills_condensed", is_condensed)
        last_skills = matched_skills

        # Canvas auto-detection — scan query for URLs, tags, and files
        await _canvas_scan_query(
            ws,
            session_id,
            text,
            attachments,
            incognito=(overrides or {}).get("incognito", False),
        )

        # Fresh cancel event for each query
        cancel_event = threading.Event()
        # Reset auto-accept — each new user message starts clean
        broker.auto_accept = False

        # ── Guard: kill any leftover job/task from a previous run ──────
        # Without this, a stale "running" job (e.g., one whose status-update
        # HTTP call failed and never reached DONE) continues sending events
        # alongside the new job, producing duplicate Router + Result blocks
        # in the same chat.  We mark the stale DB row CANCELLED so the worker
        # detects it via SaqCancelEvent.is_set() and stops within ~2 s, and we
        # cancel the _wait_job_done asyncio task so it doesn't linger either.
        if active_task and not active_task.done():
            active_task.cancel()
            active_task = None
        if session_id:
            stale = job_store.cancel_active_jobs(session_id)
            if stale:
                logger.info(
                    "Cancelled %d stale job(s) for session %s before new query",
                    stale,
                    session_id,
                )

        # ── Dispatch: worker queue or inline ──────────────
        if _is_worker_mode(mode):
            # Validate custom agent exists before enqueuing
            agent_cfg = None
            if mode.startswith("custom:"):
                agent_id = mode[len("custom:") :]
                agent_cfg = rt.get_custom_agent_by_id(agent_id)
                if not agent_cfg:
                    logger.warning("Custom agent '%s' not found — falling back to chat", agent_id)
                    active_task = asyncio.create_task(
                        _run_chat(ws, exec_text, session_id, rt, db, overrides, cancel_event=cancel_event)
                    )
                    return

            # Enqueue to the worker.
            # Inject conversation history so the worker's _NullDatabase
            # returns it from get_messages(), giving the agent multi-turn
            # context for follow-up messages like "fix those queries".
            _WORKER_MSG_CHARS = 1500  # default per-message cap before truncation
            # A client whose sends large first-turn context (e.g., a captured
            # page/DOM snapshot) can raise this per request via the generic
            # `history_char_limit` override.
            _hist_override = (overrides or {}).get("history_char_limit")
            if _hist_override is not None:
                try:
                    _WORKER_MSG_CHARS = max(_WORKER_MSG_CHARS, min(int(_hist_override), 64000))
                except (TypeError, ValueError):
                    pass
            _agent_no_history = (
                agent_cfg.get("no_history", False) if mode.startswith("custom:") and agent_cfg else False
            )
            _worker_incognito = _agent_no_history or (overrides or {}).get("incognito", False)
            raw_history = (
                _build_conversation_history(
                    db,
                    session_id,
                    incognito=_worker_incognito,
                    query=exec_text,
                )
                or []
            )
            worker_history = [
                {
                    "role": turn["role"],
                    "content": (
                        turn["content"][:_WORKER_MSG_CHARS] + "…"
                        if len(turn.get("content", "")) > _WORKER_MSG_CHARS
                        else turn.get("content", "")
                    ),
                }
                for turn in raw_history
            ]
            worker_overrides = dict(overrides or {})
            worker_overrides["_conversation_history"] = worker_history
            if _worker_incognito:
                worker_overrides["_incognito_history"] = True
            # Cross-process bridge: the SAQ worker has its own ConfigManager
            # singleton in a separate memory space, so the WS-side ContextVar
            # doesn't propagate. Stuff the session's override into the JSON
            # payload; the worker re-applies it via set_request_provider_override.
            if _session_provider:
                worker_overrides["_provider_override"] = _session_provider
            # Same cross-process bridge for per-app role overrides
            # (app_provider_role_mapping). Compute {role: concrete} for this
            # session's source + provider and pass it; the worker re-applies via
            # set_request_role_override_map (the WS-side ContextVar doesn't cross).
            if _session_source:
                try:
                    from agentforge.config import get_config as _gfc

                    _eff_prov = _session_provider or _gfc()._provider_override
                    _rom = (_gfc()._app_role_map.get(_session_source) or {}).get(_eff_prov)
                    if _rom:
                        worker_overrides["_role_override_map"] = _rom
                except Exception:
                    logger.debug("Could not compute role override map for worker job", exc_info=True)
            # Browser-extension agent runs use the shell-free 'browser' tool
            # profile so the model can't shell() its way around download_file.
            # Selected by an explicit client override (overrides.tool_profile ==
            # "browser") OR by auto-detecting a chrome-extension WS origin. Only
            # the literal "browser" value is honoured — never an arbitrary
            # client string (which would resolve to the full tool set).
            if mode == "agent":
                _origin = ws.headers.get("origin", "") or ""
                _client_tp = (overrides or {}).get("tool_profile")
                if _client_tp == "browser" or _origin.startswith("chrome-extension://"):
                    worker_overrides["_tool_profile"] = "browser"
            worker_overrides.pop("tool_profile", None)  # don't leak raw client value to the runner
            # Pass attachment file paths so the agent can use them
            # (worker runs natively on Mac, has filesystem access)
            if attachments:
                worker_overrides["_attachments"] = [
                    {
                        "path": att.get("path", ""),
                        "name": att.get("name", ""),
                        "is_image": att.get("is_image", False),
                        # Sidecar of pre-extracted text for PDFs/Office docs — without
                        # it the worker can't read non-UTF-8 binaries and silently drops them.
                        "extracted_path": att.get("extracted_path", ""),
                    }
                    for att in attachments
                    if att.get("path")
                ]
            active_job_id = await _enqueue_job(
                session_id,
                exec_text,
                mode,
                worker_overrides,
            )

            # Worker pushes events in real-time via HTTP callback
            # (HttpCallbackSocket → POST /internal/sessions/{id}/event).
            # We just wait for job completion so the WS handler stays
            # aware of lifecycle; no message forwarding needed here.
            # After the job finishes, store the exchange in conversation memory
            # and send context usage (worker can't do this — runs in a separate process).
            _skip_memory = _agent_no_history  # captured for closure

            async def _wait_and_store(sid, jid, _db, _ws, _skip_mem=_skip_memory):
                await _wait_job_done(sid, jid)
                if not _skip_mem:
                    # Run memory storage in a thread to avoid blocking the event loop
                    # (embed() and fact extraction LLM calls are synchronous)
                    _loop = asyncio.get_event_loop()
                    try:
                        await _loop.run_in_executor(
                            None,
                            _store_last_exchange_from_db,
                            _db,
                            sid,
                            mode,
                            "",
                        )
                    except Exception:
                        pass
                # Context usage for worker-completed jobs
                try:
                    _job = job_store.get_job(jid)
                    model_name = (worker_overrides or {}).get("model", "")
                    if model_name:
                        await _send_context_usage(_ws, _db, sid, model_name)
                except Exception:
                    pass

            active_task = asyncio.create_task(_wait_and_store(session_id, active_job_id, db, ws))

        elif mode == "scheduler":
            active_task = asyncio.create_task(
                _run_scheduler(
                    ws,
                    exec_text,
                    session_id,
                    rt,
                    db,
                    overrides,
                    broker=broker,
                    cancel_event=cancel_event,
                    secret_broker=secret_broker,
                )
            )
        elif mode == "monitor":
            active_task = asyncio.create_task(
                _run_monitor(
                    ws,
                    exec_text,
                    session_id,
                    rt,
                    db,
                    overrides,
                    broker=broker,
                    cancel_event=cancel_event,
                    secret_broker=secret_broker,
                )
            )
        elif mode == "search":
            active_task = asyncio.create_task(
                _run_search(ws, exec_text, session_id, rt, db, overrides, cancel_event=cancel_event)
            )
        elif mode.startswith(("custom:connector:", "custom:connector-account:")):
            # Connector agents run inline (tools are closures bound to ConnectionManager)
            agent_id = mode[len("custom:") :]
            # Look up in both custom_agents and connection_manager._agents
            agent_cfg = rt.get_custom_agent_by_id(agent_id)
            if not agent_cfg and hasattr(rt, "connection_manager") and rt.connection_manager:
                connection_id = agent_id.replace("connector:", "", 1)
                agent_cfg = rt.connection_manager._agents.get(connection_id)
            if agent_cfg:
                active_task = asyncio.create_task(
                    _run_custom_agent(
                        ws,
                        exec_text,
                        session_id,
                        rt,
                        db,
                        broker,
                        loop,
                        overrides,
                        cancel_event,
                        agent_cfg,
                        secret_broker=secret_broker,
                    )
                )
            else:
                logger.warning("Connector agent '%s' not found — falling back to chat", agent_id)
                active_task = asyncio.create_task(
                    _run_chat(ws, exec_text, session_id, rt, db, overrides, cancel_event=cancel_event)
                )
        else:
            # Default: chat (general LLM knowledge, no Qdrant)
            active_task = asyncio.create_task(
                _run_chat(
                    ws, exec_text, session_id, rt, db, overrides, attachments=attachments, cancel_event=cancel_event
                )
            )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "ping":
                await ws.send_json(protocol.pong())

            elif msg_type == "query":
                text = data.get("text", "").strip()
                # Strip stray HTML tags (e.g., <mark> from browser extensions)
                if "<" in text:
                    text = re.sub(r"<[^>]+>", "", text).strip()
                attachments = data.get("attachments", [])
                overrides = data.get("overrides")
                if not text and not attachments:
                    continue

                # Create session on first query if we don't have one yet
                if not session_id:
                    session_id = data.get("session_id", uuid.uuid4().hex)
                    # Per-session provider override is stamped here and never
                    # mutated afterwards. NULL = use the global default. The
                    # frontend only sends `provider` on the very first query.
                    initial_provider = (overrides or {}).get("provider")
                    # External clients self-identify via overrides.source so
                    # their sessions can be kept out of the
                    # human Agent Chat sidebar. The web UI omits it -> "web".
                    initial_source = (overrides or {}).get("source") or "web"
                    db.create_session(
                        session_id,
                        title="New chat",
                        provider_override=initial_provider,
                        source=initial_source,
                    )
                    if initial_provider:
                        logger.info(
                            "Session %s created with provider_override=%s",
                            session_id,
                            initial_provider,
                        )
                    await ws.send_json(
                        protocol.session_init(
                            session_id=session_id,
                            tools=rt.tool_count,
                            profiles=rt.profiles,
                            canvas_enabled=_canvas_db is not None,
                            provider_override=initial_provider,
                        )
                    )
                    logger.info("Created new session %s", session_id)
                    # Register WS now that we have a session_id
                    from . import state

                    state.active_ws[session_id] = ws

                await _process_query(text, attachments, overrides)

            elif msg_type == "query.retry":
                prompt_text = data.get("prompt_text", "")
                edited_text = data.get("edited_text")
                if not session_id:
                    await ws.send_json(protocol.query_retry_error(prompt_text, "not_found"))
                    continue

                in_flight = bool(
                    (active_task is not None and not active_task.done()) or job_store.active_job_count(session_id)
                )

                async def _resubmit(new_text: str):
                    await _process_query(new_text, [], None)

                proceeded = await _handle_retry_query(
                    ws=ws,
                    db=db,
                    session_id=session_id,
                    prompt_text=prompt_text,
                    edited_text=edited_text,
                    conv_memory=_conv_memory(),
                    in_flight=in_flight,
                    resubmit=_resubmit,
                )
                if not proceeded:
                    continue

            elif msg_type == "query.reroute":
                # User clicked the Router → [mode] chip and picked a
                # different mode. Cancel anything in flight, log the
                # override to telemetry (this is the "ground truth"
                # signal for the auto-router we're working toward), and
                # re-run the original query with the new mode forced
                # via explicit @-prefix.
                original_text = (data.get("original_text") or data.get("prompt_text") or "").strip()
                original_mode = data.get("original_mode") or ""
                new_mode = (data.get("new_mode") or "").strip()
                if not original_text or not new_mode:
                    logger.warning(
                        "query.reroute: missing original_text or new_mode (got %r / %r)",
                        original_text,
                        new_mode,
                    )
                    continue
                if not session_id:
                    logger.warning("query.reroute: no session bound, ignoring")
                    continue

                # Cancel in-flight task / worker job before re-running.
                if active_task and not active_task.done():
                    active_task.cancel()
                    active_task = None
                cancelled_jobs = job_store.cancel_active_jobs(session_id) or 0
                if cancelled_jobs:
                    logger.info(
                        "query.reroute: cancelled %d in-flight job(s) for session %s",
                        cancelled_jobs,
                        session_id,
                    )

                # Telemetry — record the override so the classifier
                # accuracy analysis later can use it as a label.
                try:
                    from .classifier_audit import get_classifier_audit

                    audit = get_classifier_audit()
                    if audit is not None:
                        await audit.log_override(
                            session_id=session_id,
                            query=original_text,
                            original_mode=original_mode,
                            override_mode=new_mode,
                        )
                except Exception:
                    logger.debug("classifier_audit.log_override failed", exc_info=True)

                # Acknowledge in the UI so the user sees the switch.
                try:
                    await ws.send_json(
                        protocol.query_reroute_ack(
                            original_mode=original_mode,
                            new_mode=new_mode,
                        )
                    )
                except Exception:
                    pass

                # Re-enter _process_query with explicit @-prefix so the
                # classifier deterministically routes to the user's choice.
                # Custom-agent aliases get the same treatment ("@" + alias).
                reroute_prefix = "@" + new_mode.lstrip("@")
                # Strip any leading @-prefix from the original text so we
                # don't double-prefix when the user is re-routing FROM an
                # already-prefixed prompt.
                _no_prefix_text = re.sub(r"^\s*@[A-Za-z0-9_:-]+\s*", "", original_text)
                rerouted_text = f"{reroute_prefix} {_no_prefix_text}".strip()
                logger.info(
                    "query.reroute: %r → %r (session=%s)",
                    original_mode,
                    new_mode,
                    session_id,
                )
                await _process_query(rerouted_text, [], None)

            elif msg_type == "confirm.response":
                confirmed = data.get("confirmed", False)
                auto_accept = data.get("auto_accept", False)
                request_id = data.get("request_id", "")
                broker.resolve(request_id, confirmed)
                # Enable auto-accept for remainder of this agent run
                if confirmed and auto_accept:
                    broker.auto_accept = True
                    logger.info("Auto-accept enabled for session %s", session_id)
                # Also store for worker polling via HttpConfirmationBroker.
                # The worker cannot receive WS messages directly, so it polls
                # GET /internal/sessions/{id}/confirm/{request_id} instead.
                from . import state as _state

                key = f"{session_id}:{request_id}"
                _state.confirm_responses[key] = {
                    "confirmed": confirmed,
                    "auto_accept": auto_accept,
                }

            elif msg_type == "secret.response":
                request_id = data.get("request_id", "")
                cancelled = bool(data.get("cancelled"))
                value = None if cancelled else data.get("value")
                # In-process path (web-run shell): resolve the broker future.
                if cancelled:
                    resolved_inproc = secret_broker.resolve(request_id, cancelled=True)
                else:
                    resolved_inproc = secret_broker.resolve(request_id, value=value)
                # Worker path (split dispatch) ONLY: stash for the native local
                # worker to poll via GET /internal/sessions/{id}/secret/{request_id}.
                # Memory-only, popped on first read; never logged or persisted.
                # Skip when an in-process waiter already consumed the response,
                # otherwise the plaintext secret would linger in state unread.
                if not resolved_inproc:
                    from . import state as _state

                    _state.secret_responses[f"{session_id}:{request_id}"] = (
                        {"cancelled": True} if cancelled else {"value": value}
                    )

            elif msg_type == "cancel":
                logger.info("Cancel requested — session %s", session_id)
                # Cancel worker job if one is active
                cancelled_jobs = 0
                if session_id:
                    cancelled_jobs = job_store.cancel_active_jobs(session_id) or 0
                # Also cancel any inline task
                if cancel_event is not None:
                    cancel_event.set()
                if active_task is not None and not active_task.done():
                    active_task.cancel()
                # If we cancelled a worker job but have no local active_task
                # (e.g., after WS reconnect), still notify the client so the
                # UI stops showing "running" state.
                if cancelled_jobs and (active_task is None or active_task.done()):
                    try:
                        cancel_msg = protocol.agent_cancelled(elapsed=0)
                        await ws.send_json(cancel_msg)
                        db.add_message(
                            session_id=session_id,
                            role="assistant",
                            msg_type="cancelled",
                            content="",
                            metadata=cancel_msg,
                        )
                    except Exception:
                        pass

            elif msg_type == "compact_session":
                if session_id:
                    logger.info("Compact requested — session %s", session_id)
                    await _compact_session(ws, db, session_id, rt)

    except WebSocketDisconnect:
        # Don't cancel the worker job — it continues running and pushes
        # events via HTTP.  On reconnect the WS is re-registered in
        # state.active_ws so future messages are forwarded again.
        logger.info("WebSocket disconnected — session %s (worker jobs continue)", session_id)
    except Exception:
        logger.exception("WebSocket error — session %s", session_id)
        if cancel_event is not None:
            cancel_event.set()
    finally:
        # Deregister this WebSocket so stale sends don't cause errors.
        if session_id:
            from . import state

            if state.active_ws.get(session_id) is ws:
                state.active_ws.pop(session_id, None)


# ---------------------------------------------------------------------------
# Chat execution — direct LLM response (no Qdrant, no tools)
# ---------------------------------------------------------------------------


async def _run_chat(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    overrides: dict | None = None,
    attachments: list | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Run a simple LLM chat — general knowledge, no Qdrant search, no tools.

    This is the default mode when no @qdrant, @agent, or other mode prefix
    is detected.  Uses the answer_generation profile for response quality.
    """
    total_start = time.perf_counter()
    ws_closed = False

    async def send_and_persist(
        msg: dict,
        *,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str = "",
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during send — persisting only")
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # --- Load conversation history BEFORE persisting current query ------
        conversation_history = (
            _build_conversation_history(
                db, session_id, incognito=(overrides or {}).get("incognito", False), query=query, mode="chat"
            )
            or []
        )

        # --- Persist user query -------------------------------------------
        query_metadata = {"type": "query", "text": query}
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata=query_metadata,
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Step 1: Route ------------------------------------------------
        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        from app.config import settings as af_settings

        answer_role = af_settings.ollama.get_role("answer_generation")
        profile_name = answer_role.profile.name
        model_name = answer_role.profile.model

        routed_msg = protocol.agent_routed(
            profile_name,
            "General LLM knowledge (no Qdrant)",
            0.0,
        )
        await send_and_persist(routed_msg, msg_type="routed")

        config_msg = protocol.agent_config(
            profile=profile_name,
            model=model_name,
            tools=0,
            session_id=session_id,
            provider=answer_role.profile.provider,
            mode="chat",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Step 2: Direct LLM call -------------------------------------
        # Use AIClient so ai.provider_override is honoured — _ollama_client_for_profile
        # is hardcoded to the Ollama SDK and would keep hitting Ollama even when the
        # active provider is DeepInfra / Bedrock / OpenRouter.
        from agentforge.client import AIClient as _AIClient

        _chat_ai_client = _AIClient(profile=profile_name)
        # Refresh the display fields from the resolved profile so the UI reports
        # the real model + provider (e.g., DeepInfra / Qwen3.5-397B-A17B) even
        # when the role config (config.yaml) still names the role's parent.
        model_name = _chat_ai_client.profile.model

        # Build messages: system prompt + conversation history + user query
        llm_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are AgentForge, a helpful AI assistant. Answer the user's "
                    "question using your general knowledge. Be clear, concise, "
                    "and helpful. If the user is asking about specific internal "
                    "APIs or documentation, suggest they use @qdrant to search "
                    "the indexed knowledge base."
                ),
            },
        ]

        # Include conversation history for multi-turn context
        for turn in conversation_history:
            llm_messages.append(
                {
                    "role": turn["role"],
                    "content": turn["content"],
                }
            )

        # Build user message — include images and/or document text from attachments
        doc_text = _load_document_attachments(attachments)
        user_content = query
        if doc_text:
            user_content = f"{query}\n\n{doc_text}" if query else doc_text
            logger.info("Injecting document text (%d chars) for session %s", len(doc_text), session_id)

        user_message: dict = {"role": "user", "content": user_content}
        b64_images = _load_image_attachments(attachments)
        if b64_images:
            user_message["images"] = b64_images
            logger.info("Attaching %d image(s) to chat message for session %s", len(b64_images), session_id)
        llm_messages.append(user_message)

        llm_options: dict[str, Any] = {
            "num_predict": answer_role.options.get("num_predict", 2048),
            "temperature": answer_role.options.get("temperature", 0.3),
        }

        # Apply per-session overrides if any
        if overrides:
            if overrides.get("model"):
                model_name = overrides["model"]
            if overrides.get("temperature") is not None:
                llm_options["temperature"] = overrides["temperature"]

        # Notify UI that we're waiting for the LLM response
        if not ws_closed:
            try:
                await ws.send_json(
                    {
                        "type": "agent.progress",
                        "phase": "pipeline_running",
                        "detail": {"step": "generating", "text": "Generating response..."},
                    }
                )
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True

        # --- Stream tokens to the client in real-time ---------------------
        # The sync Ollama client produces a blocking stream.  We run a
        # producer thread that pushes tokens into a queue, then consume
        # them on the async event loop, forwarding each to the client.
        answer_parts: list[str] = []
        token_q: _queue.Queue[str | None] = _queue.Queue()
        _chat_prompt_tokens = 0
        _chat_completion_tokens = 0

        def _stream_producer():
            nonlocal _chat_prompt_tokens, _chat_completion_tokens
            try:
                stream = _chat_ai_client.chat(
                    llm_messages,
                    stream=True,
                    temperature=llm_options.get("temperature"),
                )
                for chunk in stream:
                    if cancel_event and cancel_event.is_set():
                        break
                    token = chunk.get("content", "")
                    if token:
                        token_q.put(token)
                    # Capture token counts from the final chunk (done=True).
                    # Shape varies per provider:
                    #   Ollama raw     → ChatResponse with prompt_eval_count / eval_count
                    #   OpenAI-compat  → dict with an optional "usage" block (only
                    #                    sent when stream_options.include_usage=True —
                    #                    absent today, so counts fall back to 0).
                    if chunk.get("done"):
                        raw = chunk.get("raw")
                        if raw is not None and hasattr(raw, "prompt_eval_count"):
                            _chat_prompt_tokens = getattr(raw, "prompt_eval_count", 0) or 0
                            _chat_completion_tokens = getattr(raw, "eval_count", 0) or 0
                        elif isinstance(raw, dict):
                            usage = raw.get("usage") or {}
                            _chat_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                            _chat_completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            except Exception as e:
                token_q.put(e)
            finally:
                token_q.put(None)  # sentinel

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _stream_producer)

        deadline = loop.time() + (_PIPELINE_TIMEOUT or 600)
        while True:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError()
            if loop.time() >= deadline:
                raise asyncio.TimeoutError()

            # Non-blocking poll of the token queue
            try:
                item = token_q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.02)  # yield to event loop briefly
                continue

            if item is None:
                break  # stream finished
            if isinstance(item, Exception):
                raise item

            answer_parts.append(item)
            if not ws_closed:
                try:
                    await ws.send_json(protocol.result_chunk(item))
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

        answer = "".join(answer_parts).strip()

        # Signal streaming complete, then send the full persisted result
        if not ws_closed:
            try:
                await ws.send_json(protocol.result_done())
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True

        # --- Step 3: Send result ------------------------------------------
        total_elapsed = time.perf_counter() - total_start

        # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
        answer = _strip_wrapping_fence(answer)

        result_msg = protocol.agent_result(
            text=answer or "(no response)",
            elapsed=total_elapsed,
        )
        await send_and_persist(result_msg, msg_type="result", content=answer)

        # --- Step 4: Send summary -----------------------------------------
        summary_msg = protocol.agent_summary(
            iterations=1,
            elapsed=total_elapsed,
            tool_calls=0,
            tools={},
            prompt_tokens=_chat_prompt_tokens,
            completion_tokens=_chat_completion_tokens,
        )
        await send_and_persist(summary_msg, msg_type="summary")
        _persist_token_usage_raw(db, session_id, _chat_prompt_tokens, _chat_completion_tokens)
        await _send_context_usage(ws, db, session_id, model_name)
        _store_last_exchange_from_db(db, session_id, model=model_name)

        # --- Step 5: Update session metadata ------------------------------
        db.update_session(
            session_id,
            profile=profile_name,
            model=model_name,
        )

        # --- Step 6: Auto-title on first query ----------------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(protocol.session_title(session_id, title))
                except (WebSocketDisconnect, RuntimeError):
                    pass

    except asyncio.CancelledError:
        logger.info("Chat run cancelled — session %s", session_id)
        cancel_msg = protocol.agent_cancelled(time.perf_counter() - total_start)
        await send_and_persist(cancel_msg, msg_type="cancelled")
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - total_start
        logger.error("Chat run timed out after %.1fs — session %s", elapsed, session_id)
        error_msg = protocol.agent_error(
            f"Request timed out after {int(elapsed)}s — the model took too long to respond.",
            elapsed,
        )
        await send_and_persist(error_msg, msg_type="error")
    except Exception:
        logger.exception("Chat run failed — session %s", session_id)
        error_msg = protocol.agent_error(
            str(sys.exc_info()[1]),
            time.perf_counter() - total_start,
        )
        await send_and_persist(error_msg, msg_type="error")


# ---------------------------------------------------------------------------
# Scheduler execution — NL → job definition → persist
# ---------------------------------------------------------------------------


def _build_scheduler_system_prompt() -> str:
    """Build the scheduler system prompt with OS/user context injected."""

    home_dir = os.path.expanduser("~")
    os_name = platform.system()
    os_release = platform.release()

    template = _load_prompt("scheduler")
    return template.format(
        os_name=os_name,
        os_release=os_release,
        home_dir=home_dir,
    )


def _extract_json_from_response(text: str) -> dict | None:
    """Extract the first JSON object from an LLM response (possibly fenced)."""

    # Try fenced code block first
    m = _re.search(r"```(?:json)?\s*\n?({[\s\S]*?})\s*\n?```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try bare JSON object
    m = _re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


async def _run_scheduler(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    overrides: dict | None = None,
    broker: "ConfirmationBroker | None" = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run the scheduler mode — LLM decomposes NL into a job definition.

    Flow:
    1. Send the query to the LLM with the scheduler system prompt
    2. Parse the JSON job definition from the response
    3. Vet the command through the safety guard
    4. Create the scheduled job
    5. Send confirmation events to the frontend
    """
    total_start = time.perf_counter()
    ws_closed = False

    async def send_only(msg: dict, **_kwargs: Any) -> None:
        """Send to WebSocket but do NOT persist to SQLite.

        Scheduler mode is stateful — persisting LLM responses about jobs
        into ``chat_messages`` pollutes conversation history and fact
        extraction with ephemeral state that becomes stale.
        """
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during scheduler send")

    try:
        # --- NO conversation history for scheduler mode -------------------
        conversation_history: list[dict[str, str]] = []

        # --- Do NOT persist user query ------------------------------------

        # --- Route --------------------------------------------------------
        profile = "cloud-light"
        reason = "@scheduler mode — job scheduling"
        route_elapsed = 0.0

        routing_msg = protocol.agent_routing()
        await send_only(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed(profile, reason, route_elapsed)
        await send_only(routed_msg, msg_type="routed")

        # --- Resolve model from profile -----------------------------------
        from app.config import settings as af_settings

        answer_role = af_settings.ollama.get_role("answer_generation")
        model_name = answer_role.profile.model

        config_msg = protocol.agent_config(
            profile=profile,
            model=model_name,
            tools=0,
            session_id=session_id,
            provider=answer_role.profile.provider,
            mode="scheduler",
        )
        await send_only(config_msg, msg_type="config")

        # --- Build system prompt ------------------------------------------
        scheduler_prompt = _build_scheduler_system_prompt()

        # Include existing jobs context so the LLM knows what's already scheduled
        from .scheduler_service import get_scheduler_service

        svc = get_scheduler_service()
        existing_jobs = svc.list_jobs()
        if existing_jobs:
            jobs_ctx = "\n\n# Existing Scheduled Jobs\n\n"
            for j in existing_jobs:
                status = "enabled" if j["enabled"] else "disabled"
                last = f", last run: {j['last_run_at']} ({j['last_status']})" if j.get("last_run_at") else ""
                jobs_ctx += f"- **{j['label']}** ({j['cron_human'] or j['cron']}) [{status}]{last}\n"
                jobs_ctx += f"  ID: `{j['id']}` | Command: `{j['command']}`\n"
                # Include recent run history so the LLM can answer history queries
                try:
                    runs = svc.get_job_runs(j["id"], limit=5)
                    if runs:
                        jobs_ctx += "  Recent runs:\n"
                        for r in runs:
                            dur = f"{r['duration_s']:.2f}s" if r.get("duration_s") is not None else "?"
                            out = f" — output: `{r['output'][:80]}`" if r.get("output") else ""
                            err = f" — error: `{r['error'][:80]}`" if r.get("error") else ""
                            jobs_ctx += (
                                f"    • {r['started_at']} → {r['status']} "
                                f"(exit {r.get('exit_code', '?')}, {dur}){out}{err}\n"
                            )
                except Exception:
                    pass  # Don't let run history failure break the prompt
            scheduler_prompt += jobs_ctx
        else:
            # Without an explicit inventory the model invents a plausible job
            # list (it parrots the example commands in scheduler.md). State the
            # empty inventory so it reports "none" instead of hallucinating.
            scheduler_prompt += (
                "\n\n# Existing Scheduled Jobs\n\n"
                "There are currently NO scheduled jobs. Do NOT invent, list, or "
                "reference any jobs, names, or IDs. If asked what is scheduled, "
                "say there are none. If asked to delete jobs, say there is "
                "nothing to delete.\n"
            )

        # --- LLM call -----------------------------------------------------
        # Use AIClient so the profile's fallbacks chain (mistral-large →
        # kimi-k2-thinking → devstral-small) kicks in when the primary
        # model is overloaded (503 / 5xx), AND so ai.provider_override
        # is honoured if the session switched providers. The legacy
        # _ollama_client_for_profile path was hardcoded to the Ollama
        # SDK with no fallback or override awareness — when the primary
        # model had an outage it just surfaced the raw 503 to the user.
        from agentforge.client import AIClient as _AIClient

        _ai_client = _AIClient(profile=profile)
        # Refresh the displayed model name from the resolved profile so
        # the config card reflects what AIClient will actually call.
        model_name = _ai_client.profile.model

        llm_messages: list[dict[str, str]] = [
            {"role": "system", "content": scheduler_prompt},
        ]
        for turn in conversation_history:
            llm_messages.append({"role": turn["role"], "content": turn["content"]})
        llm_messages.append({"role": "user", "content": query})

        # Capture every fallback hop the AIClient takes (e.g., mistral-large
        # 503 → kimi-k2-thinking) so we can surface a badge to the user
        # explaining why the response came from a different model than the
        # config card advertised. Hook fires inside asyncio.to_thread —
        # collect into a list here, emit after chat returns (avoids the
        # thread → event-loop dance).
        fallback_events: list[dict] = []

        def _on_fallback(prev_name: str, next_name: str, exc: BaseException) -> None:
            try:
                from agentforge.backends._retry import classify_model_error

                decision = classify_model_error(exc)
                reason = decision.reason
            except Exception:
                reason = str(exc)[:120] or "unknown"
            # Resolve the two profiles via the client's config so we can
            # show the actual model strings (more informative than profile
            # names alone). Failures here are best-effort — never let the
            # observer break the run.
            prev_model = next_model = ""
            provider = ""
            try:
                _cfg = _ai_client._config
                p_prev = _cfg.get_profile(prev_name)
                p_next = _cfg.get_profile(next_name)
                prev_model = p_prev.model
                next_model = p_next.model
                provider = p_next.provider
            except Exception:
                pass
            fallback_events.append(
                {
                    "prev_profile": prev_name,
                    "prev_model": prev_model,
                    "next_profile": next_name,
                    "next_model": next_model,
                    "reason": reason,
                    "provider": provider,
                }
            )

        response = await _cancellable_wait(
            asyncio.to_thread(
                _ai_client.chat,
                llm_messages,
                temperature=0.2,
                on_fallback=_on_fallback,
            ),
            cancel_event,
            timeout=_PIPELINE_TIMEOUT,
        )

        # Surface any fallback hops to the UI now that we're back on the
        # event loop. Each event becomes one amber badge in the message
        # stream; the user sees the chain that recovered the request.
        for ev in fallback_events:
            await send_only(
                protocol.agent_model_fallback(
                    prev_profile=ev["prev_profile"],
                    prev_model=ev["prev_model"],
                    next_profile=ev["next_profile"],
                    next_model=ev["next_model"],
                    reason=ev["reason"],
                    provider=ev["provider"],
                ),
                msg_type="model_fallback",
            )

        # ChatResponse (not the raw Ollama dict) — same content access
        # pattern as the @chat / @monitor runners.
        answer = (response.content or "").strip()

        # --- Try to extract a job definition from the response ------------
        job_def = _extract_json_from_response(answer)

        if job_def and all(k in job_def for k in ("label", "command", "cron")):
            # This is a job creation request — vet and persist
            guard_result = svc.vet_command(job_def["command"])

            if not guard_result["safe"]:
                # Command rejected by safety guard
                guard_msg = protocol.scheduler_guard_rejected(
                    command=job_def["command"],
                    verdict=guard_result["verdict"],
                )
                await send_only(guard_msg, msg_type="scheduler.guard_rejected")

                rejection_text = (
                    f"The command was rejected by the safety guard "
                    f"(verdict: **{guard_result['verdict']}**).\n\n"
                    f"Command: `{job_def['command']}`\n\n"
                    f"Scheduled jobs cannot run destructive commands. "
                    f"Please rephrase the task or use a safer alternative."
                )
                result_msg = protocol.agent_result(text=rejection_text, elapsed=time.perf_counter() - total_start)
                await send_only(result_msg, msg_type="result", content=rejection_text)
            else:
                # Create the job
                try:
                    created = svc.create_job(
                        label=job_def["label"],
                        command=job_def["command"],
                        cron=job_def["cron"],
                        cron_human=job_def.get("cron_human"),
                        on_failure=job_def.get("on_failure", "notify"),
                        enabled=job_def.get("enabled", True),
                    )

                    # Send job_created protocol event
                    created_msg = protocol.scheduler_job_created(
                        job_id=created["id"],
                        label=created["label"],
                        cron=created["cron"],
                        cron_human=created.get("cron_human", ""),
                        command=created["command"],
                        elapsed=time.perf_counter() - total_start,
                    )
                    await send_only(created_msg, msg_type="scheduler.job_created")

                    # The TUI card (scheduler.job_created) already shows all job
                    # details — no need to also render the verbose LLM answer
                    # with the raw JSON blob as a separate Result card.

                except ValueError as exc:
                    error_text = f"Failed to create scheduled job: {exc}"
                    result_msg = protocol.agent_result(text=error_text, elapsed=time.perf_counter() - total_start)
                    await send_only(result_msg, msg_type="result", content=error_text)
        elif job_def and "action" in job_def:
            # Management action — ask for confirmation via UI dialog before executing
            action = job_def.get("action", "")
            job_ids = job_def.get("job_ids") or ([job_def["job_id"]] if job_def.get("job_id") else [])

            # Build a human-readable confirmation prompt
            action_labels = {
                "disable_jobs": "Disable",
                "enable_jobs": "Enable",
                "delete_jobs": "Delete",
                "update_job": "Update schedule of",
            }
            action_label = action_labels.get(action, action)
            # Resolve job labels for the prompt
            job_names = []
            for jid in job_ids:
                job = svc.get_job(jid)
                job_names.append(f"'{job['label']}'" if job else f"`{jid}`")
            confirm_prompt = f"{action_label} {', '.join(job_names) or 'job'}?"

            # Request confirmation via the UI confirmation dialog
            confirmed = True
            if broker:
                confirmed = await broker.request(confirm_prompt)

            if not confirmed:
                cancel_text = "Action cancelled."
                result_msg = protocol.agent_result(text=cancel_text, elapsed=time.perf_counter() - total_start)
                await send_only(result_msg, msg_type="result", content=cancel_text)
            else:
                # Confirmed — execute the action
                result_lines: list[str] = []
                elapsed = time.perf_counter() - total_start

                if action in ("disable_jobs", "enable_jobs"):
                    enabled = action == "enable_jobs"
                    status_word = "enabled" if enabled else "disabled"
                    for jid in job_ids:
                        updated = svc.update_job(jid, enabled=enabled)
                        if updated:
                            result_lines.append(f"**{updated['label']}** → {status_word}")
                            upd_msg = protocol.scheduler_job_updated(
                                job_id=updated["id"],
                                fields={"enabled": enabled},
                                elapsed=elapsed,
                            )
                            await send_only(upd_msg, msg_type="scheduler.job_updated")
                        else:
                            result_lines.append(f"`{jid}` — not found")

                elif action == "delete_jobs":
                    for jid in job_ids:
                        job = svc.get_job(jid)
                        label = job["label"] if job else jid
                        ok = svc.delete_job(jid)
                        if ok:
                            result_lines.append(f"**{label}** — deleted")
                            del_msg = protocol.scheduler_job_deleted(
                                job_id=jid,
                                label=label,
                                elapsed=elapsed,
                            )
                            await send_only(del_msg, msg_type="scheduler.job_deleted")
                        else:
                            result_lines.append(f"`{jid}` — not found")

                elif action == "update_job":
                    jid = job_def.get("job_id", "")
                    update_fields: dict = {}
                    if "cron" in job_def:
                        update_fields["cron"] = job_def["cron"]
                    if "cron_human" in job_def:
                        update_fields["cron_human"] = job_def["cron_human"]
                    if update_fields:
                        updated = svc.update_job(jid, **update_fields)
                        if updated:
                            result_lines.append(
                                f"**{updated['label']}** → schedule updated to "
                                f"`{updated['cron_human'] or updated['cron']}`"
                            )
                            upd_msg = protocol.scheduler_job_updated(
                                job_id=updated["id"],
                                fields=update_fields,
                                elapsed=elapsed,
                            )
                            await send_only(upd_msg, msg_type="scheduler.job_updated")
                        else:
                            result_lines.append(f"`{jid}` — not found")
                    else:
                        result_lines.append("No updatable fields provided.")

                else:
                    result_lines.append(f"Unknown action: `{action}`")

                result_text = "\n".join(result_lines) if result_lines else "(no jobs affected)"
                result_msg = protocol.agent_result(text=result_text, elapsed=elapsed)
                await send_only(result_msg, msg_type="result", content=result_text)

        else:
            # No JSON — read-only query (list jobs, show history, etc.)
            # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
            answer = _strip_wrapping_fence(answer)
            result_msg = protocol.agent_result(
                text=answer or "(no response)",
                elapsed=time.perf_counter() - total_start,
            )
            await send_only(result_msg, msg_type="result", content=answer)

        # --- Summary ------------------------------------------------------
        total_elapsed = time.perf_counter() - total_start
        summary_msg = protocol.agent_summary(
            iterations=1,
            elapsed=total_elapsed,
            tool_calls=0,
            tools={},
        )
        await send_only(summary_msg)
        # Skip context usage and memory storage — scheduler mode does not
        # persist messages, so there is nothing to measure or store.

        # --- Update session metadata --------------------------------------
        db.update_session(session_id, profile=profile, model=model_name)

        # --- Auto-title ---------------------------------------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(protocol.session_title(session_id, title))
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

    except asyncio.CancelledError:
        logger.info("Scheduler run cancelled — session %s", session_id)
        cancel_msg = protocol.agent_cancelled(time.perf_counter() - total_start)
        await send_only(cancel_msg, msg_type="cancelled")
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - total_start
        logger.error("Scheduler run timed out after %.1fs — session %s", elapsed, session_id)
        error_msg = protocol.agent_error(
            f"Request timed out after {int(elapsed)}s — the model took too long to respond.",
            recoverable=False,
        )
        await send_only(error_msg, msg_type="error")
    except Exception:
        logger.exception("Scheduler run failed — session %s", session_id)
        error_msg = protocol.agent_error(str(sys.exc_info()[1]), recoverable=False)
        await send_only(error_msg, msg_type="error")


# ---------------------------------------------------------------------------
# Monitor execution — NL → monitor job definition → persist
# ---------------------------------------------------------------------------


def _load_monitor_site_configs() -> dict[str, dict]:
    """Load known site configurations from ``config.yaml → tools.monitor.sites``.

    Returns a dict keyed by site name, e.g.,::

        {"funda": {"url_pattern": "funda.nl", "extraction_mode": "rendered", "selectors": {...}}}
    """
    config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("tools", {}).get("monitor", {}).get("sites", {})
    except Exception:
        return {}


def _build_site_selector_hint(query: str, site_configs: dict[str, dict]) -> str:
    """If *query* contains a URL matching a known site, return a prompt hint.

    The hint tells the LLM to use the verified selectors from config instead
    of guessing CSS/XPath selectors for that site.
    """
    if not site_configs:
        return ""

    # Extract URLs from the query (simple heuristic)

    urls_in_query = re.findall(r"https?://[^\s,)\"']+", query)

    matched_sites: list[tuple[str, dict]] = []
    for site_name, site_cfg in site_configs.items():
        url_pattern = site_cfg.get("url_pattern", "")
        if not url_pattern:
            continue
        # Check if any URL in the query matches this site
        for url in urls_in_query:
            if url_pattern in url:
                matched_sites.append((site_name, site_cfg))
                break

    if not matched_sites:
        return ""

    parts: list[str] = []
    for site_name, site_cfg in matched_sites:
        selectors = site_cfg.get("selectors", {})
        if not selectors:
            continue
        mode = site_cfg.get("extraction_mode", "rendered")
        note = site_cfg.get("note", "")

        lines = [f"\n\n# Known Site: {site_name}\n"]
        if note:
            lines.append(f"**Note:** {note}\n")
        lines.append(f"**Extraction mode:** `{mode}` (ALWAYS use this mode for {site_name})\n")
        lines.append(
            f"**IMPORTANT:** Use ONLY the verified XPath selectors below for {site_name}. "
            "Do NOT invent class names or data-testid attributes — they don't exist on this site.\n"
        )
        lines.append("**Verified selectors** — ALWAYS include ALL of them in `structured_selectors`:\n")
        lines.append("| Field | Selector(s) |")
        lines.append("|-------|-------------|")
        for field, sel_value in selectors.items():
            if isinstance(sel_value, list):
                formatted = " → ".join(f"`{s}`" for s in sel_value)
                lines.append(f"| `{field}` | {formatted} (first-match-wins) |")
            else:
                lines.append(f"| `{field}` | `{sel_value}` |")
        lines.append("")
        lines.append(
            "**IMPORTANT:** For known sites, the system automatically injects ALL configured "
            "selectors into `structured_selectors` — you do NOT need to include them manually. "
            "Some fields may have multiple fallback selectors (tried in order, first match wins). "
            "Include ALL fields regardless of what the user specifically asked about.\n"
        )
        parts.append("\n".join(lines))

    return "".join(parts)


def _build_known_sites_section(site_configs: dict[str, dict]) -> str:
    """Build a general 'Known Sites' section listing all configured sites."""
    if not site_configs:
        return ""

    lines = ["\n\n# Known Monitoring Sites\n"]
    lines.append(
        "The following sites have verified selectors configured. When a monitor URL "
        "matches one of these sites, **always use the site-specific selectors** instead "
        "of guessing CSS class names or data attributes.\n"
    )
    for site_name, site_cfg in site_configs.items():
        url_pattern = site_cfg.get("url_pattern", "")
        selectors = site_cfg.get("selectors", {})
        mode = site_cfg.get("extraction_mode", "rendered")
        fields = ", ".join(f"`{k}`" for k in selectors.keys())
        lines.append(f"- **{site_name}** (`{url_pattern}`) — mode: `{mode}`, fields: {fields}")
    lines.append("")
    return "\n".join(lines)


def _match_site_config(url: str, site_configs: dict[str, dict]) -> dict | None:
    """Return the site config whose ``url_pattern`` matches *url*, or ``None``."""
    if not site_configs or not url:
        return None
    for _site_name, site_cfg in site_configs.items():
        pattern = site_cfg.get("url_pattern", "")
        if pattern and pattern in url:
            return site_cfg
    return None


def _normalize_selectors(selectors: dict) -> dict[str, list[str]]:
    """Normalize site-config selectors so every value is a ``list[str]``.

    Accepts two formats per field::

        price: '//xpath'           →  {"price": ["//xpath"]}
        price:
          - '//xpath1'
          - '//xpath2'             →  {"price": ["//xpath1", "//xpath2"]}
    """
    normalized: dict[str, list[str]] = {}
    for field, value in selectors.items():
        if isinstance(value, list):
            normalized[field] = [str(v) for v in value if v]
        else:
            normalized[field] = [str(value)]
    return normalized


def _apply_site_selectors(job_def: dict, site_configs: dict[str, dict]) -> dict:
    """Override *job_def* with ALL configured selectors for the matched site.

    If the job URL matches a known site in ``site_configs``, this function:
    - Sets ``structured_selectors`` to the **full** set from config (not the LLM subset)
    - Forces ``extraction_mode`` to the site's configured mode
    - Normalizes each selector value to a list (first-match-wins order)
    This guarantees every configured field is always tracked, regardless of
    what the LLM chose to include.
    """
    url = job_def.get("url", "")
    site_cfg = _match_site_config(url, site_configs)
    if not site_cfg:
        return job_def

    selectors = site_cfg.get("selectors", {})
    if selectors:
        job_def["structured_selectors"] = _normalize_selectors(selectors)
        logger.info(
            "Site config override: injected all %d selector(s) for URL %s",
            len(selectors),
            url[:80],
        )

    mode = site_cfg.get("extraction_mode")
    if mode:
        job_def["extraction_mode"] = mode

    return job_def


def _build_monitor_system_prompt() -> str:
    """Build the monitor system prompt with OS/user context injected."""

    home_dir = os.path.expanduser("~")
    os_name = platform.system()
    os_release = platform.release()

    template = _load_prompt("monitor")
    prompt = template.format(
        os_name=os_name,
        os_release=os_release,
        home_dir=home_dir,
    )

    # Append a summary of all known monitoring sites
    site_configs = _load_monitor_site_configs()
    prompt += _build_known_sites_section(site_configs)

    return prompt


async def _run_monitor(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    overrides: dict | None = None,
    broker: "ConfirmationBroker | None" = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run the monitor mode — LLM decomposes NL into a monitor job definition.

    Flow:
    1. Send the query to the LLM with the monitor system prompt
    2. Parse the JSON job definition from the response
    3. Create the monitor job (takes initial snapshot)
    4. Send confirmation events to the frontend
    """
    total_start = time.perf_counter()
    ws_closed = False

    async def send_only(msg: dict, *, msg_type: str | None = None, content: str = "", **_extra: Any) -> None:
        """Send to the client and persist the message as ``volatile``."""
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during monitor send")
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type=msg_type or msg.get("type", "unknown"),
            content=content,
            metadata=msg,
            is_volatile=True,
        )

    try:
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata={"type": "query", "text": query},
            is_volatile=True,
        )

        # --- NO conversation history for monitor mode ---------------------
        # Monitor mode relies exclusively on the DB-backed "Existing
        # Monitors" section in the system prompt.  Loading chat history
        # would re-inject stale monitor details from prior exchanges.
        conversation_history: list[dict[str, str]] = []

        # --- Route --------------------------------------------------------
        profile = "cloud-light"
        reason = "@monitor mode — website change monitoring"
        route_elapsed = 0.0

        routing_msg = protocol.agent_routing()
        await send_only(routing_msg)

        routed_msg = protocol.agent_routed(profile, reason, route_elapsed)
        await send_only(routed_msg)

        # --- Resolve model from profile -----------------------------------
        # Use AIClient so ai.provider_override is honoured — _ollama_client_for_profile
        # is hardcoded to the Ollama SDK and returns 404 when the active provider is
        # DeepInfra / OpenRouter (OpenAI-compatible) or Bedrock.
        from agentforge.client import AIClient as _AIClient

        _llm_client = _AIClient(profile=profile)
        model_name = _llm_client.profile.model
        provider_name = _llm_client.profile.provider

        config_msg = protocol.agent_config(
            profile=profile,
            model=model_name,
            tools=0,
            session_id=session_id,
            provider=provider_name,
            mode="monitor",
        )
        await send_only(config_msg)

        # --- Build system prompt ------------------------------------------
        monitor_prompt = _build_monitor_system_prompt()

        # Include existing monitors context so the LLM knows what's already configured.
        # IMPORTANT: This section is the single source of truth.  Conversation memory
        # may contain stale references to monitors that have since been deleted or
        # whose DB was reset.  The LLM MUST trust this section over any memory context.
        from .monitor_service import get_monitor_service

        svc = get_monitor_service()
        existing_jobs = svc.list_jobs()
        if existing_jobs:
            jobs_ctx = "\n\n# Existing Monitors (AUTHORITATIVE — ignore any monitors mentioned in memory that are NOT listed here)\n\n"
            for j in existing_jobs:
                status = "enabled" if j["enabled"] else "disabled"
                mode = j.get("extraction_mode", "text")
                selector = f", selector: `{j['css_selector']}`" if j.get("css_selector") else ""
                struct_sel = ""
                if j.get("structured_selectors"):
                    fields = ", ".join(j["structured_selectors"].keys())
                    struct_sel = f", structured fields: [{fields}]"
                last = f", last check: {j['last_check_at']} ({j['last_status']})" if j.get("last_check_at") else ""
                jobs_ctx += (
                    f"- **{j['label']}** ({j.get('cron_human') or j['cron']}) "
                    f"[{status}, mode={mode}{selector}{struct_sel}]{last}\n"
                )
                jobs_ctx += f"  ID: `{j['id']}` | URL: `{j['url']}`\n"
                # Show latest structured content values if available
                try:
                    latest_snap = svc.db.get_latest_snapshot(j["id"])
                    if latest_snap and getattr(latest_snap, "structured_content", None):
                        sc = latest_snap.structured_content
                        jobs_ctx += (
                            "  Current values: " + ", ".join(f"**{k}**: `{v}`" for k, v in sc.items() if v) + "\n"
                        )
                except Exception:
                    pass
                # Include recent check history
                try:
                    checks = svc.get_job_checks(j["id"], limit=5)
                    if checks:
                        jobs_ctx += "  Recent checks:\n"
                        for c in checks:
                            dur = f"{c['duration_s']:.2f}s" if c.get("duration_s") is not None else "?"
                            summary = f" — {c['diff_summary']}" if c.get("diff_summary") else ""
                            err = f" — error: `{c['error'][:80]}`" if c.get("error") else ""
                            screenshot = ""
                            if c.get("screenshot_path"):
                                screenshot = f"  [screenshot](/uploads/{c['screenshot_path']})"
                            struct_diff = ""
                            if c.get("structured_diff"):
                                parts = []
                                for field, change in c["structured_diff"].items():
                                    parts.append(f"{field}: {change.get('old', '?')} → {change.get('new', '?')}")
                                struct_diff = f"  fields: {'; '.join(parts)}"
                            jobs_ctx += f"    • {c['started_at']} → {c['status']} ({dur}){summary}{err}{struct_diff}{screenshot}\n"
                except Exception:
                    pass  # Don't let check history failure break the prompt
            monitor_prompt += jobs_ctx
        else:
            monitor_prompt += (
                "\n\n# Existing Monitors (AUTHORITATIVE)\n\n"
                "**No monitors are currently configured.**\n"
                "The user has no active, disabled, or pending monitors.\n"
                "If conversation memory mentions monitors that were set up previously, "
                "they have been deleted or the database was reset — do NOT reference them.\n"
                "Help the user create a new monitor if they ask about existing ones.\n"
            )

        # --- Inject site-specific selector hints --------------------------
        # If the user's query contains a URL matching a known site (e.g., funda.nl),
        # append the verified selectors so the LLM uses them instead of guessing.
        site_configs = _load_monitor_site_configs()
        site_hint = _build_site_selector_hint(query, site_configs)
        if site_hint:
            monitor_prompt += site_hint

        # --- LLM call -----------------------------------------------------
        llm_messages: list[dict[str, str]] = [
            {"role": "system", "content": monitor_prompt},
        ]
        for turn in conversation_history:
            llm_messages.append({"role": turn["role"], "content": turn["content"]})
        llm_messages.append({"role": "user", "content": query})

        temperature = 0.2
        if overrides and overrides.get("temperature") is not None:
            temperature = overrides["temperature"]

        response = await _cancellable_wait(
            asyncio.to_thread(
                _llm_client.chat,
                llm_messages,
                stream=False,
                temperature=temperature,
            ),
            cancel_event,
            timeout=_PIPELINE_TIMEOUT,
        )

        answer = (response.content or "").strip()

        # --- Try to extract a job definition from the response ------------
        job_def = _extract_json_from_response(answer)

        if job_def and all(k in job_def for k in ("label", "url", "cron")):
            # Override with full site selectors for known sites
            job_def = _apply_site_selectors(job_def, site_configs)

            # This is a monitor job creation request
            try:
                created = svc.create_job(
                    label=job_def["label"],
                    url=job_def["url"],
                    original_prompt=query,
                    extraction_mode=job_def.get("extraction_mode", "text"),
                    css_selector=job_def.get("css_selector"),
                    structured_selectors=job_def.get("structured_selectors"),
                    cron=job_def["cron"],
                    cron_human=job_def.get("cron_human"),
                    notification_method=job_def.get("notification_method", "terminal-notifier"),
                    webhook_url=job_def.get("webhook_url"),
                    enabled=job_def.get("enabled", True),
                )

                # Send job_created protocol event
                created_msg = protocol.monitor_job_created(
                    job_id=created["id"],
                    label=created["label"],
                    url=created["url"],
                    cron=created["cron"],
                    cron_human=created.get("cron_human", ""),
                    extraction_mode=created.get("extraction_mode", "text"),
                    css_selector=created.get("css_selector"),
                    initial_snapshot=created.get("initial_snapshot"),
                    elapsed=time.perf_counter() - total_start,
                )
                await send_only(created_msg, msg_type="monitor.job_created")

                # The TUI card (monitor.job_created) already shows all job
                # details — no need to also render the verbose LLM answer
                # with the raw JSON blob as a separate Result card.

            except ValueError as exc:
                error_text = f"Failed to create monitor job: {exc}"
                result_msg = protocol.agent_result(text=error_text, elapsed=time.perf_counter() - total_start)
                await send_only(result_msg, msg_type="result", content=error_text)

        elif job_def and "action" in job_def:
            # Management action — ask for confirmation via UI dialog
            action = job_def.get("action", "")
            job_ids = job_def.get("job_ids") or ([job_def["job_id"]] if job_def.get("job_id") else [])

            # Log raw UUIDs for debugging — LLMs often produce unicode dashes
            logger.info("Monitor action=%s, raw job_ids=%r", action, job_ids)

            # Build a human-readable confirmation prompt
            action_labels = {
                "disable_jobs": "Disable",
                "enable_jobs": "Enable",
                "delete_jobs": "Delete",
                "update_job": "Update",
                "check_now": "Run immediate check on",
            }
            action_label = action_labels.get(action, action)
            job_names = []
            for jid in job_ids:
                job = svc.get_job(jid)
                job_names.append(f"'{job['label']}'" if job else f"`{jid}`")
            confirm_prompt = f"{action_label} {', '.join(job_names) or 'monitor'}?"

            # Request confirmation via the UI confirmation dialog
            confirmed = True
            if broker:
                confirmed = await broker.request(confirm_prompt)

            if not confirmed:
                cancel_text = "Action cancelled."
                result_msg = protocol.agent_result(text=cancel_text, elapsed=time.perf_counter() - total_start)
                await send_only(result_msg, msg_type="result", content=cancel_text)
            else:
                # Confirmed — execute the action
                result_lines: list[str] = []
                elapsed = time.perf_counter() - total_start

                if action in ("disable_jobs", "enable_jobs"):
                    enabled = action == "enable_jobs"
                    status_word = "enabled" if enabled else "disabled"
                    for jid in job_ids:
                        updated = svc.update_job(jid, enabled=enabled)
                        if updated:
                            result_lines.append(f"**{updated['label']}** → {status_word}")
                            upd_msg = protocol.monitor_job_updated(
                                job_id=updated["id"],
                                fields={"enabled": enabled},
                                elapsed=elapsed,
                            )
                            await send_only(upd_msg, msg_type="monitor.job_updated")
                        else:
                            result_lines.append(f"`{jid}` — not found")

                elif action == "delete_jobs":
                    for jid in job_ids:
                        job = svc.get_job(jid)
                        label = job["label"] if job else jid
                        ok = svc.delete_job(jid)
                        if ok:
                            result_lines.append(f"**{label}** — deleted")
                            del_msg = protocol.monitor_job_deleted(
                                job_id=jid,
                                label=label,
                                elapsed=elapsed,
                            )
                            await send_only(del_msg, msg_type="monitor.job_deleted")
                        else:
                            result_lines.append(f"`{jid}` — not found")

                elif action == "update_job":
                    jid = job_def.get("job_id", "")
                    update_fields: dict = {}
                    for key in (
                        "cron",
                        "cron_human",
                        "extraction_mode",
                        "css_selector",
                        "structured_selectors",
                        "label",
                        "notification_method",
                        "webhook_url",
                    ):
                        if key in job_def:
                            update_fields[key] = job_def[key]
                    # If structured_selectors is being set, enforce full site config
                    if "structured_selectors" in update_fields:
                        existing_job = svc.get_job(jid)
                        if existing_job:
                            job_url = existing_job.get("url", "")
                            site_cfg = _match_site_config(job_url, site_configs)
                            if site_cfg and site_cfg.get("selectors"):
                                update_fields["structured_selectors"] = _normalize_selectors(site_cfg["selectors"])
                    if update_fields:
                        updated = svc.update_job(jid, **update_fields)
                        if updated:
                            result_lines.append(f"**{updated['label']}** → updated ({', '.join(update_fields.keys())})")
                            upd_msg = protocol.monitor_job_updated(
                                job_id=updated["id"],
                                fields=update_fields,
                                elapsed=elapsed,
                            )
                            await send_only(upd_msg, msg_type="monitor.job_updated")
                        else:
                            result_lines.append(f"`{jid}` — not found")
                    else:
                        result_lines.append("No updatable fields provided.")

                elif action == "check_now":
                    # check_now supports both single job_id and batch job_ids
                    for jid in job_ids:
                        if not jid:
                            continue
                        job = svc.get_job(jid)
                        if not job:
                            result_lines.append(f"Monitor `{jid}` — not found")
                            continue

                        result_lines.append(f"Checking **{job['label']}**…")
                        check_result = svc.check_now(jid)
                        if check_result:
                            status = check_result.get("status", "unknown")
                            error = check_result.get("error", "")
                            summary = check_result.get("diff_summary", "")
                            note = check_result.get("note", "")

                            if status == "dispatched":
                                result_lines.append("  → dispatched to host worker (rendered mode)")
                                if note:
                                    result_lines.append(f"  {note}")
                            elif status == "error":
                                result_lines.append(f"  → **error**: {error}")
                            else:
                                result_lines.append(f"  → **{status}**")
                            if summary:
                                result_lines.append(f"  Changes: {summary}")
                            if check_result.get("lines_added") or check_result.get("lines_removed"):
                                result_lines.append(
                                    f"  +{check_result.get('lines_added', 0)} / "
                                    f"-{check_result.get('lines_removed', 0)} lines"
                                )

                            # Send check completed event (skip for dispatched — result comes later)
                            if status != "dispatched":
                                check_msg = protocol.monitor_check_completed(
                                    job_id=jid,
                                    label=job["label"],
                                    status=status,
                                    diff_summary=summary or None,
                                    lines_added=check_result.get("lines_added", 0),
                                    lines_removed=check_result.get("lines_removed", 0),
                                    elapsed=elapsed,
                                )
                                await send_only(check_msg, msg_type="monitor.check_completed")
                        else:
                            result_lines.append("  → check returned no result")

                else:
                    result_lines.append(f"Unknown action: `{action}`")

                result_text = "\n".join(result_lines) if result_lines else "(no monitors affected)"
                result_msg = protocol.agent_result(text=result_text, elapsed=elapsed)
                await send_only(result_msg, msg_type="result", content=result_text)

        else:
            # No JSON — read-only query (list monitors, show history, etc.)
            # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
            answer = _strip_wrapping_fence(answer)
            result_msg = protocol.agent_result(
                text=answer or "(no response)",
                elapsed=time.perf_counter() - total_start,
            )
            await send_only(result_msg, msg_type="result", content=answer)

        # --- Summary ------------------------------------------------------
        total_elapsed = time.perf_counter() - total_start
        summary_msg = protocol.agent_summary(
            iterations=1,
            elapsed=total_elapsed,
            tool_calls=0,
            tools={},
        )
        await send_only(summary_msg)
        # Skip context usage and memory storage — monitor mode does not
        # persist messages, so there is nothing to measure or store.

        # --- Update session metadata --------------------------------------
        db.update_session(session_id, profile=profile, model=model_name)

        # --- Auto-title ---------------------------------------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(protocol.session_title(session_id, title))
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

    except asyncio.CancelledError:
        logger.info("Monitor run cancelled — session %s", session_id)
        cancel_msg = protocol.agent_cancelled(time.perf_counter() - total_start)
        await send_only(cancel_msg, msg_type="cancelled")
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - total_start
        logger.error("Monitor run timed out after %.1fs — session %s", elapsed, session_id)
        error_msg = protocol.agent_error(
            f"Request timed out after {int(elapsed)}s — the model took too long to respond.",
            recoverable=False,
        )
        await send_only(error_msg, msg_type="error")
    except Exception:
        logger.exception("Monitor run failed — session %s", session_id)
        error_msg = protocol.agent_error(str(sys.exc_info()[1]), recoverable=False)
        await send_only(error_msg, msg_type="error")


# ---------------------------------------------------------------------------
# Search execution (replaces _run_agent from py-mini-ai-framework)
# ---------------------------------------------------------------------------


async def _run_search(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Run the AgentForge search pipeline and stream events to the client.

    Replaces the AgentLoop-based _run_agent. The flow is:
    1. Route (pick the answer_generation role)
    2. Call the shared search pipeline (refine → embed → search → re-rank)
    3. Score-gate check
    4. LLM answer generation via response_refiner
    5. Store in memory + persist to DB
    """
    total_start = time.perf_counter()
    ws_closed = False

    # Helper: send over WS and persist to DB
    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during send — persisting only")
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    # Helper: send a transient pipeline.step event (not persisted to DB).
    # Async version — _run_search runs on the main event loop.  The same
    # helper name is used in _run_discovery (sync variant) for consistency.
    async def _step_event(step: str, status: str, **kwargs: Any) -> None:
        nonlocal ws_closed
        if ws_closed:
            return
        elapsed = time.perf_counter() - total_start
        msg = protocol.pipeline_step(step, status, elapsed, **kwargs)
        try:
            await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            ws_closed = True

    try:
        # --- Load conversation history BEFORE persisting current query ------
        conversation_history = _build_conversation_history(
            db, session_id, incognito=(overrides or {}).get("incognito", False), query=query, mode="search"
        )

        # --- Persist user query -------------------------------------------
        query_metadata = {"type": "query", "text": query}
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata=query_metadata,
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Step 1: Route ------------------------------------------------
        # AgentForge uses role-based routing (answer_generation), not profile routing.
        # We show the routing step to the UI for visual consistency.
        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        from app.config import settings as af_settings

        answer_role = af_settings.ollama.get_role("answer_generation")
        profile_name = answer_role.profile.name
        model_name = answer_role.profile.model

        route_elapsed = 0.0  # no actual routing needed
        routed_msg = protocol.agent_routed(
            profile_name,
            "AgentForge search pipeline (Qdrant + RAG)",
            route_elapsed,
        )
        await send_and_persist(routed_msg, msg_type="routed")

        config_msg = protocol.agent_config(
            profile=profile_name,
            model=model_name,
            tools=0,
            session_id=session_id,
            provider=answer_role.profile.provider,
            mode="search",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="search",
            model=model_name,
            profile=profile_name,
            query=query,
        )

        # --- Step 2: Parse query modifiers and apply sticky filters ---------
        from app.routes.search import SearchRequest, _smart_search_pipeline
        from app.services.code_context_service import enrich_results as enrich_code_context
        from app.services.response_refiner import response_refiner

        # Retry knowledge cache load if initial startup failed (indexer
        # might not have been ready when the web server started).
        if not rt.known_sources:
            logger.info("Knowledge cache empty — retrying load before query parsing")
            rt._load_knowledge_cache()

        parsed_query, parsed_filters, is_sticky = _parse_query(query, rt, session_id)

        # Parse source_names from comma-separated string into a list (if present)
        _raw_source_names = parsed_filters.get("source_names")
        _source_names_list = (
            [s.strip() for s in _raw_source_names.split(",") if s.strip()] if _raw_source_names else None
        )

        # Use a higher default limit for sql-schema queries — the LLM needs
        # more table chunks to write accurate JOINs.  The relationship
        # expansion step will fill in FK-related tables on top of this.
        # Detect sql-schema either from explicit source_type filter or by
        # looking up the source_name in the knowledge cache.
        _is_sql = parsed_filters.get("source_type") == "sql-schema"
        if not _is_sql and parsed_filters.get("source_name"):
            _src_info = rt.known_sources.get(parsed_filters["source_name"].lower(), {})
            _is_sql = _src_info.get("source_type") == "sql-schema"
        if not _is_sql and _source_names_list:
            # Multi-source: check if ANY resolved source is sql-schema
            _is_sql = any(
                rt.known_sources.get(sn.lower(), {}).get("source_type") == "sql-schema" for sn in _source_names_list
            )
        _default_limit = "15" if _is_sql else "10"
        search_req = SearchRequest(
            query=parsed_query,
            limit=int(parsed_filters.get("limit", _default_limit)),
            source_name=parsed_filters.get("source_name"),
            source_names=_source_names_list,
            source_type=parsed_filters.get("source_type"),
            api_name=parsed_filters.get("api_name"),
            chunk_type=parsed_filters.get("chunk_type"),
            domain_group=parsed_filters.get("domain_group"),
            document_name=parsed_filters.get("document_name"),
            score_floor=float(parsed_filters["score_floor"]) if "score_floor" in parsed_filters else None,
            include_examples=True if parsed_filters.get("verbose") == "true" else None,
            brief=parsed_filters.get("brief") == "true",
            session_id=session_id,
        )

        # _smart_search_pipeline does: LLM query refinement → embed → vector
        # search → score floor → re-rank.  We emit a single "searching" step
        # that covers the whole pipeline (we can't inject events into the
        # shared function without modifying the search module).
        await _step_event("searching", "running", detail="Refining query and searching vector database...")

        search_start = time.perf_counter()
        reranked, meta = await asyncio.wait_for(
            _smart_search_pipeline(search_req),
            timeout=_PIPELINE_TIMEOUT,
        )
        search_elapsed = time.perf_counter() - search_start

        logger.info(
            "Search pipeline completed: %d results in %.2fs (refined: %s)",
            len(reranked),
            search_elapsed,
            meta.get("was_refined"),
        )

        # --- Step 3: Score-gate -------------------------------------------
        best_score = max((r.get("score", 0) for r in reranked), default=0)
        general_knowledge = best_score < af_settings.search.relevance_threshold

        if general_knowledge:
            logger.info(
                "Score-gate: best_score=%.3f < threshold=%.2f → general-knowledge mode",
                best_score,
                af_settings.search.relevance_threshold,
            )

        meta["best_score"] = round(best_score, 4)
        meta["relevance_threshold"] = af_settings.search.relevance_threshold
        meta["general_knowledge"] = general_knowledge

        # Strip markdown formatting from refined query — the LLM refiner
        # sometimes adds **bold** or *italic* markers to emphasise terms,
        # which shouldn't appear in the metadata display.
        refined_raw = meta.get("refined_query")
        if refined_raw and isinstance(refined_raw, str):
            meta["refined_query"] = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", refined_raw)

        # --- Step 3b: Send search pipeline metadata to the client ---------
        # Build filters dict from the search request (mirrors TUI display)
        active_filters: dict[str, str] = {}
        if search_req.source_names:
            active_filters["source_name"] = ", ".join(search_req.source_names)
        elif search_req.source_name:
            active_filters["source_name"] = search_req.source_name
        if search_req.source_type:
            active_filters["source_type"] = search_req.source_type
        if search_req.api_name:
            active_filters["api_name"] = search_req.api_name
        if search_req.document_name:
            active_filters["document_name"] = search_req.document_name

        search_meta_msg = protocol.search_meta(
            refined_query=meta.get("refined_query"),
            filters=active_filters,
            result_count=len(reranked),
            dropped_by_floor=meta.get("dropped_by_floor", 0),
            best_score=best_score,
            general_knowledge=general_knowledge,
            intent=meta.get("intent"),
            preferred_methods=meta.get("preferred_methods"),
            demoted_by_method=meta.get("demoted_by_method", 0),
            search_elapsed=search_elapsed,
            is_sticky=is_sticky,
            parsed_query=parsed_query if parsed_query != query else None,
        )
        await send_and_persist(search_meta_msg, msg_type="search_meta")

        await _step_event("searching", "done", result_count=len(reranked), best_score=round(best_score, 3))

        # --- Step 4: Relationship expansion + code context enrichment -----
        refiner_results = reranked[: response_refiner.refiner_max_results]
        if not general_knowledge:
            # Expand sql-schema results with related table chunks
            from app.routes.search import _expand_sql_relationships

            pre_expand = len(refiner_results)
            refiner_results = _expand_sql_relationships(refiner_results)
            expanded_count = len(refiner_results) - pre_expand
            if expanded_count:
                logger.info("Relationship expansion added %d table chunks to refiner context", expanded_count)

            await _step_event("enriching", "running", detail="Enriching code context...")
            refiner_results = await enrich_code_context(refiner_results)
            await _step_event("enriching", "done")

        # --- Step 5: LLM answer generation (streamed) -----------------------
        await _step_event("generating", "running", detail="Generating answer...")

        answer_parts: list[str] = []
        _search_token_usage: dict[str, int] = {}
        gen_deadline = asyncio.get_event_loop().time() + (_PIPELINE_TIMEOUT or 600)
        async for token in response_refiner.refine_stream(
            query,
            refiner_results,
            include_examples=None,
            brief=False,
            conversation_history=conversation_history,
            general_knowledge=general_knowledge,
            token_usage=_search_token_usage,
        ):
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError()
            if asyncio.get_event_loop().time() >= gen_deadline:
                raise asyncio.TimeoutError()
            answer_parts.append(token)
            if not ws_closed:
                try:
                    await ws.send_json(protocol.result_chunk(token))
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

        answer = "".join(answer_parts).strip()

        # Signal streaming complete
        if not ws_closed:
            try:
                await ws.send_json(protocol.result_done())
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True

        await _step_event("generating", "done")

        # --- Step 6: Send result ------------------------------------------
        total_elapsed = time.perf_counter() - total_start

        # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
        answer = _strip_wrapping_fence(answer)

        result_msg = protocol.agent_result(
            text=answer or "(no result)",
            elapsed=total_elapsed,
        )
        await send_and_persist(result_msg, msg_type="result", content=answer)

        # --- Step 7: Send summary -----------------------------------------
        summary_msg = protocol.agent_summary(
            iterations=1,  # single search pass (no tool loop)
            elapsed=total_elapsed,
            tool_calls=0,
            tools={},
        )
        await send_and_persist(summary_msg, msg_type="summary")
        _persist_token_usage_raw(
            db,
            session_id,
            _search_token_usage.get("prompt_tokens", 0),
            _search_token_usage.get("completion_tokens", 0),
        )
        await _send_context_usage(ws, db, session_id, model_name)
        _store_last_exchange_from_db(db, session_id, model=model_name)

        # --- Hooks: run completed -----------------------------------------
        from ._hooks import hooks_run_completed

        await hooks_run_completed(
            session_id,
            query=query,
            mode="search",
            model=model_name,
            profile=profile_name,
            duration_ms=int(total_elapsed * 1000),
            result_text=answer or "",
        )

        # --- Step 8: Update session metadata ------------------------------
        db.update_session(
            session_id,
            profile=profile_name,
            model=model_name,
        )

        # --- Step 9: Auto-generate title on first query -------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(
                        {
                            "type": "session.title",
                            "session_id": session_id,
                            "title": title,
                        }
                    )
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

    except asyncio.CancelledError:
        elapsed = time.perf_counter() - total_start
        logger.info("Search run cancelled for session %s (%.1fs)", session_id, elapsed)
        cancelled_msg = protocol.agent_cancelled(elapsed)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="cancelled",
            content="Cancelled by user",
            metadata=cancelled_msg,
            is_incognito=(overrides or {}).get("incognito", False),
        )
        if not ws_closed:
            try:
                await ws.send_json(cancelled_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_cancelled

        await hooks_run_cancelled(session_id, mode="search", duration_ms=int(elapsed * 1000))

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - total_start
        logger.error("Search run timed out after %.1fs — session %s", elapsed, session_id)
        msg = f"Request timed out after {int(elapsed)}s — the model took too long to respond."
        error_msg = protocol.agent_error(msg, recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=msg,
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(session_id, mode="search", duration_ms=int(elapsed * 1000), error_message=msg)

    except Exception as exc:
        logger.exception("Search run failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc), recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="search",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )


# ---------------------------------------------------------------------------
# Web search execution (@search / @web — searches the internet, not local docs)
# ---------------------------------------------------------------------------


def _build_web_search_system_prompt() -> str:
    """Build the web search system prompt with OS/user context injected."""

    home_dir = os.path.expanduser("~")
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    os_name = platform.system()  # "Linux", "Darwin", "Windows"
    os_release = platform.release()

    template = _load_prompt("web_search")
    return template.format(
        os_name=os_name,
        os_release=os_release,
        username=username,
        home_dir=home_dir,
    )


_WEB_SEARCH_TOOLS = [
    "web_search",
    "web_fetch",  # fast raw-HTML fetch (static sites, docs)
    "web_fetch_rendered",  # headless Chromium — SPAs, JS-rendered pages, analytics detection
    "web_screengrab",  # on-demand full-page screenshot
    "read_file",  # in case search results reference local files
    "write_file",  # save research to a file if asked
    # TMDB tools — structured movie/TV/person lookups
    "movie_search",
    "movie_details",
    "tv_search",
    "tv_details",
    "person_search",
    "person_details",
    "trending_media",
    "multi_search",
]


async def _run_web_search(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run the AgentLoop with web search tools for @search mode.

    Unlike _run_search (RAG pipeline), this routes through the agent with
    web_search + web_fetch tools so the model actually searches the internet.
    """
    total_start = time.perf_counter()
    ws_closed = False

    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)

    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during send — persisting only")
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # --- Load conversation history BEFORE persisting current query ------
        conversation_history = _build_conversation_history(
            db, session_id, incognito=(overrides or {}).get("incognito", False), query=query
        )

        # --- Persist user query -------------------------------------------
        query_metadata = {"type": "query", "text": query, "mode": "web_search"}
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata=query_metadata,
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Route: use web-search profile (large model) for web search ----
        # The "agent" profile uses a small model (devstral-small-2:24b) which
        # struggles with path resolution and complex instructions.  Web search
        # needs a larger model to correctly interpret results and follow through.
        profile = "web-search"
        reason = "@search mode — web search via Ollama Cloud"
        route_elapsed = 0.0

        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed(profile, reason, route_elapsed)
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Build agent with web search tools ----------------------------
        from agentforge.agent import AgentLoop
        from agentforge.client import AIClient

        web_search_prompt = _inject_user_context(_build_web_search_system_prompt(), rt)
        # Inject skill instructions
        web_search_prompt = _inject_skills(web_search_prompt, overrides, condensed=False)
        agent_client = AIClient(profile=profile)

        _agent_event = _make_agent_event_callback(send_sync, db, session_id, total_start)

        agent = AgentLoop(
            agent_client,
            rt.registry,
            system_prompt=web_search_prompt,
            tools=_WEB_SEARCH_TOOLS,
            max_iterations=8,
            verbose=False,
            cancel_event=cancel_event,
            on_event=_agent_event,
            stream_final=False,  # disabled: worker HTTP callbacks deliver chunks out-of-order
            deep_think=bool(agent_client.profile.thinking_budget),
        )

        actual_tool_count = len(_WEB_SEARCH_TOOLS)
        config_msg = protocol.agent_config(
            profile=profile,
            model=agent_client.model,
            tools=actual_tool_count,
            session_id=session_id,
            provider=agent_client.profile.provider,
            mode="web_search",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="web_search",
            model=agent_client.model,
            profile=profile,
            query=query,
        )

        # --- Run agent in thread ------------------------------------------
        from agentforge.context import PipelineContext

        conversation_history = conversation_history or [{"role": "system", "content": web_search_prompt}]
        conversation_history.append({"role": "user", "content": query})
        # Inject uploaded attachments onto the user turn. web_search is a worker
        # mode, so attachments arrive as overrides["_attachments"]; without this an
        # @search query with a file attached loses the file entirely.
        conversation_history = _inject_attachments(agent_client, conversation_history, overrides)
        ctx = PipelineContext(query=query)
        ctx.messages = conversation_history
        # Override the system prompt in messages
        if ctx.messages and ctx.messages[0].get("role") == "system":
            ctx.messages[0] = {"role": "system", "content": web_search_prompt}

        agent_start = time.perf_counter()
        ctx = await _cancellable_wait(
            asyncio.to_thread(agent.run, ctx=ctx),
            cancel_event,
            timeout=_PIPELINE_TIMEOUT,
        )
        _agent_elapsed = time.perf_counter() - agent_start

        # --- Extract tool calls + results from iterations ----------------
        all_tool_calls = _extract_tool_calls_with_results(ctx.metadata.get("agent_iterations", []))

        # --- Inject screenshots the LLM may have summarised away ----------
        ctx.result = _inject_screenshots(ctx.result or "", ctx)
        # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
        ctx.result = _strip_wrapping_fence(ctx.result)

        # --- Emit file.diff events BEFORE the result card ----------------
        # Any verified writes (code_edit / revert_file / write_file) render
        # as a unified-diff card above the agent's natural-language summary.
        await _emit_file_diff_events(all_tool_calls, send_and_persist)
        await _emit_file_compare_events(all_tool_calls, send_and_persist)
        _redact_tool_results(all_tool_calls)

        # --- Send result --------------------------------------------------
        total_elapsed = time.perf_counter() - total_start

        result_msg = protocol.agent_result(
            text=ctx.result or "(no result)",
            elapsed=total_elapsed,
        )
        await send_and_persist(
            result_msg,
            msg_type="result",
            content=ctx.result,
            tool_calls=all_tool_calls if all_tool_calls else None,
        )

        # --- Send summary -------------------------------------------------
        from collections import Counter

        iterations = ctx.metadata.get("agent_iterations", [])
        tool_counter: Counter = Counter()
        for it in iterations:
            for tc in it.tool_calls or []:
                tool_counter[tc["name"]] += 1

        n_tools = sum(tool_counter.values())
        summary_msg = protocol.agent_summary(
            iterations=len(iterations),
            elapsed=total_elapsed,
            tool_calls=n_tools,
            tools=dict(tool_counter),
        )
        await send_and_persist(summary_msg, msg_type="summary")
        _persist_token_usage(db, session_id, ctx)
        await _send_context_usage(ws, db, session_id, agent_client.model)
        _store_last_exchange_from_db(db, session_id, model=agent_client.model)

        # --- Hooks: run completed + tool audit ----------------------------
        from ._hooks import hooks_log_tools, hooks_run_completed

        _tc_names = ",".join(dict(tool_counter).keys())
        await hooks_log_tools(session_id, all_tool_calls, mode="web_search", model=agent_client.model)
        await _fire_post_write_hooks(
            session_id,
            all_tool_calls,
            mode="web_search",
            model=agent_client.model,
        )
        await hooks_run_completed(
            session_id,
            query=query,
            mode="web_search",
            model=agent_client.model,
            profile=profile,
            duration_ms=int(total_elapsed * 1000),
            iterations=len(iterations),
            tool_count=n_tools,
            tools_used=_tc_names,
            result_text=ctx.result or "",
        )

        # --- Update session metadata --------------------------------------
        db.update_session(session_id, profile=profile, model=agent_client.model)

        # --- Auto-title on first query ------------------------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(
                        {
                            "type": "session.title",
                            "session_id": session_id,
                            "title": title,
                        }
                    )
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

        # --- Errors if any ------------------------------------------------
        if ctx.errors:
            for err in ctx.errors:
                error_msg = protocol.agent_error(str(err), recoverable=False)
                await send_and_persist(error_msg, msg_type="error", content=str(err))

    except Exception as exc:
        logger.exception("Web search agent failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc), recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="web_search",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run


# ---------------------------------------------------------------------------
# Log analysis execution (@logs / @log — diagnose errors, explain log messages)
# ---------------------------------------------------------------------------

_LOG_ANALYSIS_SYSTEM_PROMPT = _load_prompt("logs")

_LOG_ANALYSIS_TOOLS = [
    "shell",  # journalctl, docker logs, tail, grep, etc.
    "ssh",  # remote log access (myserver, staging, etc.)
    "read_file",  # read log files directly
    "analyze_logs",  # structured log parsing (errors, patterns, health)
    "write_file",  # save analysis reports if asked
    "web_search",  # look up unfamiliar errors
    "web_fetch",  # read full docs/solutions for errors
    "web_fetch_rendered",  # headless Chromium for JS-rendered docs/dashboards
]


async def _run_log_analysis(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run the AgentLoop with log analysis tools for @logs mode.

    Reads log files/commands, diagnoses errors, cross-references with
    web search, and proposes fixes.
    """
    total_start = time.perf_counter()
    ws_closed = False

    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)

    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during send — persisting only")
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # --- Load conversation history BEFORE persisting current query ------
        conversation_history = _build_conversation_history(
            db, session_id, incognito=(overrides or {}).get("incognito", False), query=query
        )

        # --- Persist user query -------------------------------------------
        query_metadata = {"type": "query", "text": query, "mode": "logs"}
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata=query_metadata,
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Route: fixed to log-analyzer profile for tool calling ---------
        profile = "log-analyzer"
        reason = "@logs mode — log analysis with web search"
        route_elapsed = 0.0

        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed(profile, reason, route_elapsed)
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Build agent with log analysis tools --------------------------
        from agentforge.agent import AgentLoop
        from agentforge.client import AIClient

        agent_client = AIClient(profile=profile)

        _agent_event = _make_agent_event_callback(send_sync, db, session_id, total_start)

        log_prompt = _inject_user_context(_LOG_ANALYSIS_SYSTEM_PROMPT, rt)
        log_prompt = _inject_skills(log_prompt, overrides, condensed=False)

        agent = AgentLoop(
            agent_client,
            rt.registry,
            system_prompt=log_prompt,
            tools=_LOG_ANALYSIS_TOOLS,
            max_iterations=12,
            verbose=False,
            cancel_event=cancel_event,
            iter_timeout=600,
            max_tool_output=12_000,
            on_event=_agent_event,
            stream_final=False,  # disabled: worker HTTP callbacks deliver chunks out-of-order
            deep_think=bool(agent_client.profile.thinking_budget),
        )

        actual_tool_count = len(_LOG_ANALYSIS_TOOLS)
        config_msg = protocol.agent_config(
            profile=profile,
            model=agent_client.model,
            tools=actual_tool_count,
            session_id=session_id,
            provider=agent_client.profile.provider,
            mode="logs",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="logs",
            model=agent_client.model,
            profile=profile,
            query=query,
        )

        # --- Run agent in thread ------------------------------------------
        from agentforge.context import PipelineContext

        conversation_history = conversation_history or [{"role": "system", "content": _LOG_ANALYSIS_SYSTEM_PROMPT}]
        conversation_history.append({"role": "user", "content": query})
        # Inject uploaded attachments onto the user turn (worker mode — see _run_web_search).
        conversation_history = _inject_attachments(agent_client, conversation_history, overrides)
        ctx = PipelineContext(query=query)
        ctx.messages = conversation_history
        # Override the system prompt in messages
        if ctx.messages and ctx.messages[0].get("role") == "system":
            ctx.messages[0] = {"role": "system", "content": _LOG_ANALYSIS_SYSTEM_PROMPT}

        agent_start = time.perf_counter()
        ctx = await _cancellable_wait(
            asyncio.to_thread(agent.run, ctx=ctx),
            cancel_event,
            timeout=_PIPELINE_TIMEOUT,
        )
        _agent_elapsed = time.perf_counter() - agent_start

        # --- Extract tool calls + results from iterations ----------------
        all_tool_calls = _extract_tool_calls_with_results(ctx.metadata.get("agent_iterations", []))

        # --- Inject screenshots the LLM may have summarised away ----------
        ctx.result = _inject_screenshots(ctx.result or "", ctx)
        # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
        ctx.result = _strip_wrapping_fence(ctx.result)

        # --- Emit file.diff events BEFORE the result card ----------------
        # Any verified writes (code_edit / revert_file / write_file) render
        # as a unified-diff card above the agent's natural-language summary.
        await _emit_file_diff_events(all_tool_calls, send_and_persist)
        await _emit_file_compare_events(all_tool_calls, send_and_persist)
        _redact_tool_results(all_tool_calls)

        # --- Send result --------------------------------------------------
        total_elapsed = time.perf_counter() - total_start

        result_msg = protocol.agent_result(
            text=ctx.result or "(no result)",
            elapsed=total_elapsed,
        )
        await send_and_persist(
            result_msg,
            msg_type="result",
            content=ctx.result,
            tool_calls=all_tool_calls if all_tool_calls else None,
        )

        # --- Send summary -------------------------------------------------
        from collections import Counter

        iterations = ctx.metadata.get("agent_iterations", [])
        tool_counter: Counter = Counter()
        for it in iterations:
            for tc in it.tool_calls or []:
                tool_counter[tc["name"]] += 1

        n_tools = sum(tool_counter.values())
        summary_msg = protocol.agent_summary(
            iterations=len(iterations),
            elapsed=total_elapsed,
            tool_calls=n_tools,
            tools=dict(tool_counter),
        )
        await send_and_persist(summary_msg, msg_type="summary")
        _persist_token_usage(db, session_id, ctx)
        await _send_context_usage(ws, db, session_id, agent_client.model)
        _store_last_exchange_from_db(db, session_id, model=agent_client.model)

        # --- Hooks: run completed + tool audit ----------------------------
        from ._hooks import hooks_log_tools, hooks_run_completed

        _tc_names = ",".join(dict(tool_counter).keys())
        await hooks_log_tools(session_id, all_tool_calls, mode="logs", model=agent_client.model)
        await _fire_post_write_hooks(
            session_id,
            all_tool_calls,
            mode="logs",
            model=agent_client.model,
        )
        await hooks_run_completed(
            session_id,
            query=query,
            mode="logs",
            model=agent_client.model,
            profile=profile,
            duration_ms=int(total_elapsed * 1000),
            iterations=len(iterations),
            tool_count=n_tools,
            tools_used=_tc_names,
            result_text=ctx.result or "",
        )

        # --- Update session metadata --------------------------------------
        db.update_session(session_id, profile=profile, model=agent_client.model)

        # --- Auto-title on first query ------------------------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(
                        {
                            "type": "session.title",
                            "session_id": session_id,
                            "title": title,
                        }
                    )
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

        # --- Errors if any ------------------------------------------------
        if ctx.errors:
            for err in ctx.errors:
                error_msg = protocol.agent_error(str(err), recoverable=False)
                await send_and_persist(error_msg, msg_type="error", content=str(err))

    except Exception as exc:
        logger.exception("Log analysis agent failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc), recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="logs",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run


# ---------------------------------------------------------------------------
# Pipeline mode — factory for the search_knowledge_base tool
# ---------------------------------------------------------------------------


def _make_search_kb_tool(rt: "SearchRuntime"):
    """Return an async search_knowledge_base function bound to this runtime.

    Called once from SearchRuntime._init_agent_tools().  The returned
    function is registered with the ToolRegistry so the @pipeline agent
    can call it as a normal tool.
    """

    async def search_knowledge_base(query: str, source: str = "") -> str:
        """Search the indexed knowledge base (APIs, schemas, docs, code) via RAG.

        Use this whenever you need domain knowledge before acting: e.g., fetching
        table schemas before writing SQL, looking up endpoint signatures before
        building a request, or validating that a field name actually exists.

        Pair with save_result to store large result sets and keep this context
        window lean:
            search_knowledge_base("user table columns", source="appdb")
            → save_result("appdb_user_schema", <result>)
            → later steps call load_result("appdb_user_schema")
        """
        from app.routes.search import SearchRequest, _smart_search_pipeline

        # Retry knowledge cache if still empty (startup race condition)
        if not rt.known_sources:
            rt._load_knowledge_cache()

        # Resolve source alias (e.g., "myalias" → "appdb")
        source_name: str | None = None
        if source:
            info = rt.known_sources.get(source.lower())
            source_name = info["source_name"] if info else source

        req = SearchRequest(
            query=query,
            limit=12,
            source_name=source_name,
        )

        try:
            reranked, meta = await _smart_search_pipeline(req)
        except Exception as exc:
            return f"search_knowledge_base failed: {exc}"

        if not reranked:
            return f"No results found for: {query!r}" + (f" (source: {source_name})" if source_name else "")

        header = f"Found {len(reranked)} results for: {query!r}"
        if source_name:
            header += f"  [source: {source_name}]"
        if meta.get("refined_query") and meta["refined_query"] != query:
            header += f"\n  (refined to: {meta['refined_query']!r})"

        lines = [header, ""]
        for i, r in enumerate(reranked[:12], 1):
            score = r.get("score", 0)
            src = r.get("source_name", "")
            chunk_type = r.get("chunk_type", "")
            name = r.get("name", r.get("chunk_id", ""))
            content = (r.get("content") or r.get("description") or "")[:600]
            lines.append(f"[{i}] score={score:.2f} | {src}/{chunk_type}: {name}")
            if content:
                lines.append(content)
            lines.append("")

        return "\n".join(lines)

    return search_knowledge_base


# ---------------------------------------------------------------------------
# Pipeline runner — @pipeline mode
# ---------------------------------------------------------------------------


async def _run_pipeline(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: "ChatDatabase",
    broker: "ConfirmationBroker",
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run an @pipeline query — merged tool registry + RAG search + result cache.

    @pipeline gives the agent access to ALL available tools simultaneously:
      • All @agent tools (shell, file ops, git, Docker, SSH, …)
      • search_knowledge_base  — RAG lookup via Qdrant, same pipeline as @qdrant
      • save_result / load_result — Redis-backed intermediate result caching

    This removes the rigid mode silos: a single natural-language pipeline
    description (e.g., "find SQL in PHP files → validate against Qdrant schema
    → run valid queries → save .sql output") is decomposed and executed end-to-end
    by the agent without any @prefix switching.

    The system prompt is augmented with pipeline-specific guidance so the agent
    knows to use save_result for large intermediate data and search_knowledge_base
    for domain lookups, rather than re-passing everything through the context window.
    """
    # Read the pipeline LLM profile from framework-config (pipeline.profile).
    # Default: "thinker" (mistral-large-3:675b-cloud, 8k tokens). Note: the
    # iteration cap is not read here — pipeline.max_iterations in YAML is not wired up.
    pipeline_llm_profile = "thinker"
    try:
        from agentforge.config import get_config as get_fw_config

        fw_cfg = get_fw_config(_fw_config_path)
        pipeline_llm_profile = fw_cfg.get("pipeline.profile", "thinker")
    except Exception:
        logger.debug("Could not read pipeline.profile from framework-config — using 'thinker'")

    logger.info("@pipeline — LLM: %s, tools: pipeline (36, no shell)", pipeline_llm_profile)

    await _run_agent(
        ws,
        query,
        session_id,
        rt,
        db,
        broker,
        loop,
        overrides=overrides,
        cancel_event=cancel_event,
        forced=True,
        _profile_override="pipeline",
        _llm_profile_override=pipeline_llm_profile,
        _system_prompt_suffix=_PIPELINE_SYSTEM_PROMPT_SUFFIX,
        secret_broker=secret_broker,
    )


_PIPELINE_SYSTEM_PROMPT_SUFFIX = """
## @pipeline mode

You have an extended tool set for multi-step workflows:
- **read_file(path)** — returns the **complete** file contents as a string (use this to read any file)
- **grep_text(pattern, path)** — content search across files (use for finding files by pattern)
- **find_files(pattern, directory)** — locate files by name/glob
- **search_knowledge_base(query, source)** — RAG lookup over indexed schemas, APIs, docs, code
- **execute_sql(database, query)** — runs SQL against a named database connection
- **save_result(key, value)** — stores a large intermediate result under a short key
- **load_result(key)** — retrieves a value previously stored with save_result
- **write_file, code_edit, git_*, docker_*, ssh, web_search, web_fetch, …** — standard agent tools

Note: `shell` is not available in @pipeline mode — use the dedicated tools above instead.

### Source name mapping
`#hashtag` tokens in the query resolve to specific names for each tool:

| hashtag    | search_knowledge_base `source=` | execute_sql `database=` | SQL schema name  | DB engine  |
|------------|----------------------------------|-------------------------|------------------|------------|
| `#mydb`    | `'mydb'`                         | `'mydb'`                | `'myschema'`     | MySQL      |
| `#appdb`   | `'appdb'`                        | `'appdb'`               | `'public'`       | PostgreSQL |
| `#myapi`   | `'myapi'`                        | —                       | —                | —          |

Always use the **database** column value with `execute_sql`, not the raw hashtag name.

### Workflow C — Querying a live database (ALWAYS start here when exploring data)

When a task requires finding tables, running COUNT(*), or doing any data analysis on a database
you haven't already inspected in this session, **discover the schema first — never guess table names**.

**Step 1 — Discover actual tables**
Use `execute_sql` to list tables that actually exist in the target schema:

- MySQL/mydb: `execute_sql(database='mydb', query="SELECT table_name FROM information_schema.tables WHERE table_schema = 'myschema' ORDER BY table_name")`
- PostgreSQL/appdb: `execute_sql(database='appdb', query="SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name")`

Run these discovery queries **in parallel** if you need both databases.

**Step 2 — Filter to relevant tables**
Scan the returned table list in context — pick only tables matching the user's criteria
(e.g., names containing `user`, `contact`, `address`, `customer`, etc.).

**Step 3 — Query only discovered tables**
Call `execute_sql` for each table that actually appeared in Step 1.
Run multiple COUNT(*) queries in parallel for speed.

**Step 4 — Save and report**
Use `save_result` for large intermediate results, then `write_file` for final reports.

**Rule: Do NOT run queries against table names you invented.** Always verify existence via
`information_schema.tables` first. A single discovery query costs one iteration; guessing
wrong table names wastes many iterations on errors.

---

### Workflow A — Running queries from a .sql file (pre-validated)

When the user asks you to read and/or run queries from a **`.sql` file**, use this short workflow:

**Step 1 — Read the .sql file**
Call `read_file(path='<full path to .sql file>')` to get its contents.

**Step 2 — Execute each query**
Call `execute_sql(database='<name>', query='...')` for each SQL statement found in the file.

**Step 3 — Summarise**
Report how many queries ran, their row counts, and any errors.

**Important:** SQL files often contain header comments like `-- extracted from /path/to/source.php`
or `-- validated against #schema on <date>`. These are **metadata only** — do NOT follow those
file paths or re-run the extraction/validation workflow. The queries in the .sql file are already
ready to execute.

---

### Workflow B — Extracting + validating queries from source code

Use this when the user asks you to **find** SQL queries inside a source file (PHP, Python, etc.)
and validate or run them.

**Step 1 — Read the source file**
Call `read_file(path='<full path>')` to get the complete file contents as a string.
Reason over the full text in your context to extract SQL queries, API calls, table references, etc.
`read_file` returns everything — including multiline queries and dynamically constructed strings.

**Step 2 — Fetch the reference schema**
Call `search_knowledge_base(query='tables and columns', source='<name>')` to retrieve
the indexed schema for the target database.
If the result is large, call `save_result('schema', <result>)` and retrieve it later with `load_result`.

**Step 3 — Validate**
Cross-reference the extracted items against the schema.
Keep only items whose table and column names actually exist in the schema.

**Step 4 — Execute**
Call `execute_sql(database='<name>', query='...')` for each valid item.

**Step 5 — Write output and summarise**
Call `write_file(path='<requested path>', content='...')` with the results.
Report: items found, items valid, items executed successfully, output file location.

---

### Workflow D — Git commit history and hotspot analysis

Use `git_log` and `git_show` to analyse commit history. **Do NOT use `git_diff` for
per-commit file inspection** — `git_diff` only shows uncommitted working-tree changes,
it does not accept a commit hash, and it does not write any files to `/tmp/`.

**For hotspot analysis (which files change most often):**

Step 1 — Fetch commits with file lists in a single call:
```
git_log(path='<repo>', count=40, include_files=True)
```
This returns each commit followed by the files it changed. Parse the text in context:
lines starting with `---` are commit headers; other non-empty lines are file paths.

Step 2 — Count file occurrences across all commits to find hotspots.
Reason over the returned text directly — do NOT call `jq_query` or write temp files.

Step 3 — If you need the full diff for a specific commit, call:
```
git_show(path='<repo>', commit='<hash>', name_only=False)
```
Use `name_only=True` if you only need the file list for that commit (faster).

Step 4 — Save the ranked hotspot list with `save_result`, write the final report
with `write_file`.

**Key rules:**
- `git_diff` = working-tree only, no commit param, no file output → use `git_show` instead
- `git_log(include_files=True)` = most efficient way to get all commit file lists at once
- Parse `git_log` / `git_show` text directly in context — never pipe through `jq_query`
""".strip()


# ---------------------------------------------------------------------------
# Agent execution (ported from py-mini-ai-framework for tool-based queries)
# ---------------------------------------------------------------------------


async def _run_agent(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    forced: bool = False,
    _profile_override: str | None = None,
    _llm_profile_override: str | None = None,
    _system_prompt_suffix: str | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run the AgentLoop with system tools and stream events to the client.

    Used for operational queries (Docker, SSH, system commands, file ops).
    When ``forced=True`` (user used @tooling/@agent prefix), skip the
    ProfileRouter and use the 'agent' profile directly — the user explicitly
    chose tool mode and shouldn't be downgraded to a weaker model.

    ``_profile_override`` lets callers (e.g., _run_pipeline) force a specific
    profile name (e.g., "pipeline") which selects the matching tool subset from
    rt.agent_profiles without going through the ProfileRouter.

    ``_system_prompt_suffix`` is appended to the agent system prompt when set,
    allowing callers to inject mode-specific guidance (e.g., @pipeline workflow
    instructions) without modifying the shared prompt template.

    The flow mirrors py-mini-ai-framework's _run_agent():
    1. Route to a profile
    2. Build AgentLoop with appropriate tool subset
    3. Run agent in thread, streaming tool calls via AgentBridge
    4. Persist result + summary to DB
    """
    total_start = time.perf_counter()
    ws_closed = False

    # Set the session id in THIS coroutine's context so the copy that
    # asyncio.to_thread(agent.run, ...) makes carries it into the agent thread,
    # where saq_dispatch_tool reads it to route a tool's sudo prompt back here.
    # (The earlier set in _process_query / _execute_agent_job doesn't reliably
    # survive into this runner's context.)
    set_request_session_id(session_id)

    # Thread-safe sender (guards against closed socket)
    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    # Bridge: wires registry callbacks to WebSocket events
    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)  # always wire callbacks early

    # Helper: send over WS (if still open) and persist to DB
    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during send — persisting only")
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # --- Load conversation history BEFORE persisting current query ------
        conversation_history = _build_conversation_history(
            db, session_id, incognito=(overrides or {}).get("incognito", False), query=query
        )

        # --- Persist user query -------------------------------------------
        query_metadata = {"type": "query", "text": query}
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata=query_metadata,
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Step 1: Route ------------------------------------------------
        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        # Two separate concepts share the name "profile":
        #   llm_profile  — the AIClient / LLM model profile (must exist in
        #                   framework-config.yaml, e.g., "agent", "thinker")
        #   tool_profile — the key into rt.agent_profiles{} that selects
        #                   which tool subset to load (e.g., "pipeline")
        # For most modes they are the same string.  @pipeline is the
        # exception: it uses the "agent" LLM but the "pipeline" tool subset.
        llm_profile = "agent"
        tool_profile = _profile_override or "agent"
        reason = "Tool-based query (system operations)"
        route_elapsed = 0.0

        if _profile_override:
            # _run_pipeline passes _profile_override="pipeline" (tool set) and
            # _llm_profile_override="thinker" (LLM model) so the two are fully
            # decoupled: 34-tool pipeline set (no shell) + large model for better planning.
            llm_profile = _llm_profile_override or "agent"
            reason = f"Forced '{_profile_override}' tool set · LLM: {llm_profile}"
            logger.info(
                "Skipping ProfileRouter — forced tool set (llm=%s, tools=%s)",
                llm_profile,
                _profile_override,
            )
        elif forced:
            reason = "Explicit @tooling/@agent prefix — using agent profile"
            logger.info("Skipping ProfileRouter — forced agent mode")
        else:
            try:
                from agentforge.client import AIClient
                from agentforge.router import ProfileRouter

                router_client = AIClient(profile="tool")
                prof_router = ProfileRouter(router_client)
                route_start = time.perf_counter()
                route = await asyncio.to_thread(prof_router.select, query)
                route_elapsed = time.perf_counter() - route_start
                llm_profile = route.profile
                tool_profile = route.profile
                reason = route.reason
            except Exception:
                logger.debug("ProfileRouter unavailable — defaulting to 'agent'")

        routed_msg = protocol.agent_routed(llm_profile, reason, route_elapsed)
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Step 2: Build agent ------------------------------------------
        from agentforge.agent import AgentLoop
        from agentforge.client import AIClient

        agent_client = AIClient(profile=llm_profile)

        # Select tool subset using tool_profile (may differ from llm_profile
        # in @pipeline mode where tool_profile="pipeline", llm_profile="agent").
        tool_subset = rt.agent_profiles.get(tool_profile)
        if tool_profile in ("fast", "default"):
            max_iters = 5
        elif tool_profile == "pipeline":
            max_iters = 20  # pipeline queries are multi-step; give more headroom
        else:
            max_iters = 10

        # Build system prompt — append mode-specific suffix if provided
        # (e.g., @pipeline workflow guidance injected by _run_pipeline).
        system_prompt = rt.agent_system_prompt
        if _system_prompt_suffix:
            system_prompt = system_prompt + "\n\n" + _system_prompt_suffix

        # Inject skill instructions (full on first turn)
        system_prompt = _inject_skills(system_prompt, overrides, condensed=False)

        # Build condensed prompt for iteration 2+ (if enabled)
        _condensed_prompt: str | None = None
        if rt.af_settings.agent.condense_tool_prompt:
            _condensed_prompt = rt.agent_system_prompt_condensed
            if _system_prompt_suffix:
                _condensed_prompt = _condensed_prompt + "\n\n" + _system_prompt_suffix
            _condensed_prompt = _inject_skills(_condensed_prompt, overrides, condensed=True)

        # --- Agent event callback: streams progress to WS ---
        _agent_event = _make_agent_event_callback(send_sync, db, session_id, total_start)

        agent = AgentLoop(
            agent_client,
            rt.registry,
            system_prompt=system_prompt,
            system_prompt_condensed=_condensed_prompt,
            tools=tool_subset,
            max_iterations=max_iters,
            verbose=False,
            cancel_event=cancel_event,
            iter_timeout=600,
            on_event=_agent_event,
            stream_final=False,  # disabled: worker HTTP callbacks deliver chunks out-of-order
            deep_think=bool(agent_client.profile.thinking_budget),
            max_tool_output=rt.af_settings.agent.max_tool_output,
            read_only=bool((overrides or {}).get("read_only")),
        )

        actual_tool_count = len(tool_subset) if tool_subset else rt.tool_count
        config_msg = protocol.agent_config(
            profile=tool_profile,  # show the tool-set name (e.g., "pipeline") in the UI
            model=agent_client.model,
            tools=actual_tool_count,
            session_id=session_id,
            provider=agent_client.profile.provider,
            mode="agent",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="agent",
            model=agent_client.model,
            profile=tool_profile,
            query=query,
        )

        # --- Step 2b: Try parallel planning (optional) --------------------
        # For queries that involve multiple independent tasks (e.g., "reinstall
        # deps in project-a and project-b"), ask a fast model to decompose
        # into parallel groups.  Falls back to sequential AgentLoop if the
        # planner says parallel=false or planning fails.
        parallel_plan = None
        try:
            from agentforge.config import get_config as get_fw_config
            from agentforge.parallel import ParallelAgentRunner

            fw_cfg = get_fw_config(_fw_config_path)
            parallel_enabled = fw_cfg.get("parallel.enabled", True)

            if not parallel_enabled:
                raise RuntimeError("parallel disabled via config")

            # @pipeline mode must use the sequential AgentLoop with its filtered
            # tool subset.  ParallelAgentRunner hardcodes shell execution and
            # bypasses tool_subset entirely — disallow it here.
            if tool_profile == "pipeline":
                raise RuntimeError("parallel disabled for @pipeline mode (uses sequential AgentLoop + tool_subset)")

            planner_profile = fw_cfg.get("parallel.planner_profile", "fast")
            planner_client = AIClient(profile=planner_profile)
            parallel_runner = ParallelAgentRunner(
                planner_client,
                rt.registry,
                cancel_event=cancel_event,
            )
            plan_start = time.perf_counter()
            parallel_plan = await asyncio.to_thread(
                parallel_runner.plan,
                query,
                conversation_history,
            )
            plan_elapsed = time.perf_counter() - plan_start

            if parallel_plan and parallel_plan.get("parallel"):
                groups = parallel_plan.get("groups", [])
                logger.info(
                    "[Agent] Parallel plan: %d groups (%.2fs)",
                    len(groups),
                    plan_elapsed,
                )
                # Notify client of the plan
                plan_msg = protocol.parallel_plan(groups, plan_elapsed)
                await send_and_persist(plan_msg, msg_type="parallel.plan")
            else:
                parallel_plan = None  # not parallelisable → sequential
                logger.debug(
                    "[Agent] Query not parallelisable (%.2fs) — sequential mode",
                    plan_elapsed,
                )
        except ImportError:
            logger.debug("ParallelAgentRunner not available — sequential only")
        except RuntimeError as exc:
            logger.debug("Parallel skipped: %s", exc)
        except Exception as exc:
            logger.warning("Parallel planning failed: %s — falling back to sequential", exc)
            parallel_plan = None

        # --- Step 3: Execute (parallel or sequential) ---------------------
        if parallel_plan and parallel_plan.get("parallel"):
            # ---- PARALLEL PATH ----
            # Emit tool.call events so the existing UI renders command cards.
            _parallel_call_buffer: list[dict] = []

            def _group_event_callback(
                group_idx: int,
                label: str,
                event: str,
                data: dict,
            ) -> None:
                """Stream parallel group events to the client over WS."""
                # Send the native parallel event
                msg = protocol.parallel_group_event(group_idx, label, event, data)
                send_sync(msg)

                # Also emit tool.call for each command so the UI shows them
                if event == "command":
                    tc_msg = protocol.tool_call(
                        "shell",
                        {"command": data.get("command", ""), "cwd": data.get("cwd", "")},
                    )
                    send_sync(tc_msg)
                    _parallel_call_buffer.append(
                        {
                            "name": "shell",
                            "args": {"command": data.get("command", ""), "cwd": data.get("cwd", "")},
                        }
                    )

            # Temporarily mute the registry's built-in tool_call/tools_complete
            # handlers so they don't duplicate the events we send manually
            # from _group_event_callback above.
            _saved_tc = getattr(rt.registry, "_on_tool_call", None)
            _saved_tcc = getattr(rt.registry, "_on_tools_complete", None)
            rt.registry._on_tool_call = None
            rt.registry._on_tools_complete = None

            exec_start = time.perf_counter()
            try:
                ctx = await asyncio.to_thread(
                    parallel_runner.execute,
                    parallel_plan,
                    on_group_event=_group_event_callback,
                )
            finally:
                # Restore handlers so the sequential path still works
                rt.registry._on_tool_call = _saved_tc
                rt.registry._on_tools_complete = _saved_tcc
            _exec_elapsed = time.perf_counter() - exec_start

            # Flush tool calls so the UI knows the batch is complete
            send_sync(protocol.tool_calls_flush())

            # --- Synthesis: send raw results through LLM for interpretation ---
            raw_output = ctx.result or ""
            try:
                synth_messages = list(conversation_history or [])
                synth_messages.append({"role": "user", "content": query})
                synth_messages.append(
                    {
                        "role": "assistant",
                        "content": ("I ran the following commands and collected their output:\n\n" + raw_output),
                    }
                )
                synth_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Based on the command outputs above, provide a clear, "
                            "concise answer to the original question. "
                            "Include the raw output for reference."
                        ),
                    }
                )

                synth_response = await asyncio.to_thread(
                    agent_client.chat,
                    synth_messages,
                    temperature=0.3,
                )
                if synth_response and synth_response.content:
                    ctx.result = _strip_wrapping_fence(synth_response.content.strip())
                    logger.info("[Agent] Parallel synthesis complete")
            except Exception as exc:
                logger.warning("[Agent] Parallel synthesis failed: %s — using raw output", exc)
                # ctx.result already holds the raw formatted output as fallback

            # Persist tool_calls record for sticky-mode detection
            group_count = len(parallel_plan.get("groups", []))
            db.add_message(
                session_id=session_id,
                role="assistant",
                msg_type="tool_calls",
                content=None,
                metadata={
                    "type": "tool_calls",
                    "parallel": True,
                    "groups": group_count,
                    "calls": _parallel_call_buffer
                    or [
                        {"name": "shell", "args": {"parallel_group": g.get("label", "")}}
                        for g in parallel_plan.get("groups", [])
                    ],
                },
                is_incognito=(overrides or {}).get("incognito", False),
            )
        else:
            # ---- SEQUENTIAL PATH (original AgentLoop) ----
            bridge.setup_registry(rt.registry)

            # Build conversation context with history
            from agentforge.context import PipelineContext

            # Extract attachments from worker overrides
            raw_atts = (overrides or {}).pop("_attachments", None)
            _att_images = None
            _att_documents = None
            user_content = query  # what the model sees; may get document text appended
            if raw_atts:
                from agentforge.attachments import Attachment

                agent_attachments = [Attachment(path=a["path"], name=a["name"]) for a in raw_atts if a.get("path")]
                # Apply attachments via provider-aware logic. Keep `query` pristine
                # (it feeds ctx.query + title generation); only the message the model
                # receives gets the appended document text / images.
                _temp_msgs = agent_client._apply_attachments([{"role": "user", "content": query}], agent_attachments)
                if _temp_msgs:
                    user_content = _temp_msgs[0].get("content", query)
                    _att_images = _temp_msgs[0].get("images")
                    _att_documents = _temp_msgs[0].get("documents")

            history_messages = conversation_history or []
            history_messages.append({"role": "user", "content": user_content})
            # Inject attachment data onto the user message
            if raw_atts:
                user_msg = history_messages[-1]
                if _att_images:
                    user_msg["images"] = _att_images
                if _att_documents:
                    user_msg["documents"] = _att_documents

            ctx = PipelineContext(query=query)
            ctx.messages = history_messages

            agent_start = time.perf_counter()
            ctx = await _cancellable_wait(
                asyncio.to_thread(agent.run, ctx=ctx),
                cancel_event,
                timeout=_PIPELINE_TIMEOUT,
            )
            _agent_elapsed = time.perf_counter() - agent_start

        # --- Extract tool calls + results from iterations ----------------
        all_tool_calls = []
        if not (parallel_plan and parallel_plan.get("parallel")):
            all_tool_calls = _extract_tool_calls_with_results(ctx.metadata.get("agent_iterations", []))

        # --- Emit file.diff events BEFORE the result card ----------------
        # Any verified writes (code_edit / revert_file / write_file) render
        # as a unified-diff card above the agent's natural-language summary.
        await _emit_file_diff_events(all_tool_calls, send_and_persist)
        await _emit_file_compare_events(all_tool_calls, send_and_persist)
        _redact_tool_results(all_tool_calls)

        # --- Step 4: Send result ------------------------------------------
        total_elapsed = time.perf_counter() - total_start

        # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
        ctx.result = _strip_wrapping_fence(ctx.result or "")

        result_msg = protocol.agent_result(
            text=ctx.result or "(no result)",
            elapsed=total_elapsed,
        )
        await send_and_persist(
            result_msg,
            msg_type="result",
            content=ctx.result,
            tool_calls=all_tool_calls if all_tool_calls else None,
        )

        # --- Step 5: Send summary -----------------------------------------
        if parallel_plan and parallel_plan.get("parallel"):
            # Parallel summary — count shell calls across groups
            parallel_results = ctx.metadata.get("parallel_results", [])
            total_cmds = sum(len(gr.outputs) for gr in parallel_results)
            group_count = len(parallel_results)

            summary_msg = protocol.agent_summary(
                iterations=group_count,
                elapsed=total_elapsed,
                tool_calls=total_cmds,
                tools={"shell": total_cmds},
            )
        else:
            # Sequential summary
            iterations = ctx.metadata.get("agent_iterations", [])
            tool_counter: Counter = Counter()
            for it in iterations:
                for tc in it.tool_calls or []:
                    tool_counter[tc["name"]] += 1

            n_tools = sum(tool_counter.values())

            summary_msg = protocol.agent_summary(
                iterations=len(iterations),
                elapsed=total_elapsed,
                tool_calls=n_tools,
                tools=dict(tool_counter),
            )
        await send_and_persist(summary_msg, msg_type="summary")
        _persist_token_usage(db, session_id, ctx)
        await _send_context_usage(ws, db, session_id, agent_client.model)
        _store_last_exchange_from_db(db, session_id, model=agent_client.model)

        # --- Hooks: run completed + tool audit ----------------------------
        from ._hooks import hooks_log_tools, hooks_run_completed

        _tc_names = (
            ",".join(dict(tool_counter).keys()) if not (parallel_plan and parallel_plan.get("parallel")) else "shell"
        )
        await hooks_log_tools(session_id, all_tool_calls, mode="agent", model=agent_client.model)
        # Post-write verification — catches silent write failures
        await _fire_post_write_hooks(
            session_id,
            all_tool_calls,
            mode="agent",
            model=agent_client.model,
        )
        await hooks_run_completed(
            session_id,
            query=query,
            mode="agent",
            model=agent_client.model,
            profile=tool_profile,
            duration_ms=int(total_elapsed * 1000),
            iterations=len(ctx.metadata.get("agent_iterations", [])),
            tool_count=len(all_tool_calls),
            tools_used=_tc_names,
            result_text=ctx.result or "",
        )

        # --- Step 6: Update session metadata ------------------------------
        db.update_session(
            session_id,
            profile=tool_profile,  # store the tool-set profile (e.g., "pipeline")
            model=agent_client.model,
        )

        # --- Step 7: Auto-generate title on first query -------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(
                        {
                            "type": "session.title",
                            "session_id": session_id,
                            "title": title,
                        }
                    )
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

    except asyncio.CancelledError:
        elapsed = time.perf_counter() - total_start
        logger.info("Agent run cancelled for session %s (%.1fs)", session_id, elapsed)
        cancelled_msg = protocol.agent_cancelled(elapsed)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="agent.cancelled",
            content="Cancelled by user",
            metadata=cancelled_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(cancelled_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_cancelled

        await hooks_run_cancelled(session_id, mode="agent", duration_ms=int(elapsed * 1000))

    except Exception as exc:
        logger.exception("Agent run failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc), recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="agent",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run


# ---------------------------------------------------------------------------
# SQL execution mode — RAG schema lookup → agent generates & runs SQL
# ---------------------------------------------------------------------------

_SQL_SYSTEM_PROMPT_TEMPLATE = """\
You are AgentForge, an AI assistant for your team.
The user wants you to query an internal database. Below is the schema context
retrieved from the knowledge base — it shows the relevant tables, columns,
relationships, and allowed joins.

TARGET DATABASE: {target_database}
AVAILABLE DATABASES: {available_databases}

INSTRUCTIONS:
1. **Start with sql_extract_schema** — ALWAYS call sql_extract_schema(database)
   first to get the complete database schema (all tables, columns, types,
   primary keys, foreign keys, indexes, views). This returns a compact summary
   that fits in context, and caches the full JSON in Redis for follow-up queries.
   Do NOT query information_schema manually — execute_sql truncates results
   to 100 rows, which loses most of the schema for large databases.
2. Study the schema from sql_extract_schema carefully. Identify which tables
   and columns are needed to answer the user's question.
3. Write a SQL query and execute it using the execute_sql tool.
   - Use the target database above as the 'database' parameter.
   - Use the correct SQL dialect for the target database engine.
4. After getting results, provide a clear, natural-language answer.
   Do NOT just dump the raw table — summarise the results conversationally.
   Example: "There were 47 new sales orders created in the past week."
5. If the query fails, analyse the error and try a corrected query.

{dialect_hint}

SCHEMA CONTEXT (from RAG — may be partial; prefer sql_extract_schema for full schema):
{schema_context}
"""


async def _run_sql(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run SQL execution mode: RAG → agent generates SQL → execute_sql tool.

    Flow:
    1. Parse #hashtag sources to determine target database(s)
    2. RAG search for schema context (tables, columns, relationships)
    3. Build a system prompt with schema context + VALID JOINS
    4. Run AgentLoop with execute_sql tool
    5. Agent generates SQL, calls tool, summarises results
    """
    total_start = time.perf_counter()
    ws_closed = False

    # Thread-safe sender
    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    # Bridge: wires registry callbacks to WebSocket events
    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)

    # Helper: send over WS and persist to DB
    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during send — persisting only")
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    # Helper: send transient step event (not persisted)
    async def _step_event(step: str, status: str, **kwargs: Any) -> None:
        nonlocal ws_closed
        if ws_closed:
            return
        elapsed = time.perf_counter() - total_start
        msg = protocol.pipeline_step(step, status, elapsed, **kwargs)
        try:
            await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            ws_closed = True

    try:
        # --- Load conversation history ------------------------------------
        conversation_history = _build_conversation_history(
            db, session_id, incognito=(overrides or {}).get("incognito", False), query=query
        )

        # --- Persist user query -------------------------------------------
        query_metadata = {"type": "query", "text": query}
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata=query_metadata,
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Step 1: Route + announce mode --------------------------------
        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed(
            "sql",
            "SQL execution mode — querying database",
            0.0,
        )
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Step 2: Parse query (hashtags → source filters) --------------
        await _step_event("parsing", "running", detail="Parsing query and source filters")

        # Reuse the same hashtag/source parsing as _run_search
        parsed_filters: dict[str, str] = {}
        hashtag_sources: list[str] = []
        has_explicit_source = False
        query_parts: list[str] = []

        for part in query.split():
            if part.startswith("#") and len(part) > 1:
                tag = part[1:].lower().rstrip("?!.,;:")
                if not tag:
                    query_parts.append(part)
                    continue
                has_explicit_source = True
                if tag in rt.SOURCE_TYPE_ALIASES:
                    parsed_filters["source_type"] = rt.SOURCE_TYPE_ALIASES[tag]
                elif tag in rt.SOURCE_ALIASES:
                    alias_val = rt.SOURCE_ALIASES[tag]
                    canonicals = alias_val if isinstance(alias_val, list) else [alias_val]
                    for canonical in canonicals:
                        resolved = rt.known_sources.get(canonical, {}).get("source_name", canonical)
                        hashtag_sources.append(resolved)
                elif tag in rt.known_sources:
                    resolved = rt.known_sources[tag]["source_name"]
                    hashtag_sources.append(resolved)
                else:
                    hashtag_sources.append(tag)
            else:
                query_parts.append(part)

        clean_query = " ".join(query_parts).strip()
        if not clean_query:
            clean_query = query

        # Build source filter
        _source_names_list: list[str] = hashtag_sources
        if _source_names_list:
            if len(_source_names_list) == 1:
                parsed_filters["source_name"] = _source_names_list[0]
            # For multiple sources, we pass them as a list in the search request

        # Default to sql-schema source type if not set
        if "source_type" not in parsed_filters:
            parsed_filters["source_type"] = "sql-schema"

        # --- Resolve target SQL database from hashtag ----------------------
        # The hashtag resolves source names for Qdrant, but we also need to
        # map to a sql_databases key for the execute_sql tool.
        target_db = ""
        try:
            from app.services.db_service import db_service as _db_svc

            sql_db_keys = list(_db_svc.available_databases)  # e.g., ["mydb", "appdb"]
            # Try matching the raw hashtag tag(s) against sql_databases keys and names
            for part in query.split():
                if part.startswith("#") and len(part) > 1:
                    tag = part[1:].lower().rstrip("?!.,;:")
                    # Direct key match (e.g., #mydb, #appdb)
                    if tag in sql_db_keys:
                        target_db = tag
                        break
                    # Partial/alias match (e.g., #myapi → appdb, #mydb → mydb)
                    for key in sql_db_keys:
                        if key.startswith(tag) or tag.startswith(key.replace("db", "")):
                            target_db = key
                            break
                    if target_db:
                        break
        except ImportError:
            pass

        # Sticky database: a follow-up @sql query with no #db hashtag stays on
        # the last database used this session (mirrors sticky sources in
        # _run_search).
        if has_explicit_source and target_db:
            _session_sql_db[session_id] = target_db
        elif not has_explicit_source:
            target_db = target_db or _session_sql_db.get(session_id, "")

        await _step_event("parsing", "done", sources=_source_names_list or [])

        # --- Step 3: RAG search for schema context ------------------------
        await _step_event("searching", "running", detail="Searching for schema context")

        from app.routes.search import SearchRequest, _expand_sql_relationships, _smart_search_pipeline

        search_req = SearchRequest(
            query=clean_query,
            limit=15,  # more context for SQL
            filters=parsed_filters,
            source_names=_source_names_list if len(_source_names_list) > 1 else None,
            session_id=session_id,
        )

        reranked, meta = await asyncio.wait_for(
            _smart_search_pipeline(search_req),
            timeout=_PIPELINE_TIMEOUT,
        )

        await _step_event(
            "searching",
            "done",
            results=len(reranked),
            refined_query=meta.get("refined_query", clean_query),
        )

        if not reranked and not target_db:
            # No RAG results AND no hashtag-resolved database — can't proceed
            error_msg = protocol.agent_error(
                "No schema information found for your query. Try specifying a database with #mydb or #appdb.",
                recoverable=True,
            )
            await send_and_persist(error_msg, msg_type="error", content=error_msg["message"])
            return

        # --- Step 4: Build schema context ---------------------------------
        await _step_event("context", "running", detail="Building schema context with relationships")

        schema_context = ""
        dialect_hint = ""

        if reranked:
            # Normal path: RAG returned schema chunks — build context from them
            from app.services.response_refiner import (
                _build_context,
                _sql_dialect_addendum,
                response_refiner,
            )

            # Expand SQL relationships (fetch FK-related tables)
            refiner_results = reranked[: response_refiner.refiner_max_results]
            refiner_results = _expand_sql_relationships(refiner_results)

            schema_context = _build_context(
                refiner_results,
                max_chars=response_refiner._refiner_max_context_chars,
            )
            dialect_hint = _sql_dialect_addendum(refiner_results)
        else:
            # Fallback: no RAG results but target_db was resolved from hashtag.
            # The agent will use sql_extract_schema to discover the schema.
            logger.info(
                "SQL mode: no RAG results but target_db=%s resolved from hashtag — "
                "agent will use sql_extract_schema for schema discovery",
                target_db,
            )
            schema_context = (
                "(No pre-indexed schema context available. Use sql_extract_schema to discover the full schema.)"
            )

        # Determine available databases for the prompt
        try:
            from app.services.db_service import db_service

            available_dbs = ", ".join(db_service.available_databases_display) or "(none configured)"
        except ImportError:
            available_dbs = "(database service not available)"

        # Format target DB display
        if target_db:
            try:
                entry = _db_svc._configs.get(target_db)
                target_db_display = f"{target_db} ({entry.name}, {entry.engine})" if entry and entry.name else target_db
            except Exception:
                target_db_display = target_db
        else:
            target_db_display = "(auto-detect from query context)"

        sql_system_prompt = _SQL_SYSTEM_PROMPT_TEMPLATE.format(
            target_database=target_db_display,
            available_databases=available_dbs,
            dialect_hint=dialect_hint,
            schema_context=schema_context,
        )

        await _step_event("context", "done", schema_chunks=len(reranked))

        # --- Step 5: Run agent with execute_sql tool ----------------------
        await _step_event("agent", "running", detail="Generating and executing SQL")

        from agentforge.agent import AgentLoop
        from agentforge.client import AIClient

        # Use cloud-heavy for SQL — small models (devstral) are weak at
        # tool calling with complex schema context.
        agent_client = AIClient(profile="cloud-heavy")

        # Expose execute_sql + schema extraction for this mode
        tool_subset = ["sql_extract_schema", "execute_sql"]

        _agent_event = _make_agent_event_callback(send_sync, db, session_id, total_start)

        agent = AgentLoop(
            agent_client,
            rt.registry,
            system_prompt=sql_system_prompt,
            tools=tool_subset,
            max_iterations=8,  # schema discovery + generate SQL + retries
            verbose=False,
            cancel_event=cancel_event,
            on_event=_agent_event,
            max_tool_output=32_000,  # sql_extract_schema returns ~12-26K for large DBs
            stream_final=False,  # disabled: worker HTTP callbacks deliver chunks out-of-order
            deep_think=bool(agent_client.profile.thinking_budget),
        )

        config_msg = protocol.agent_config(
            profile="sql",
            model=agent_client.model,
            tools=2,  # sql_extract_schema + execute_sql
            session_id=session_id,
            provider=agent_client.profile.provider,
            mode="sql",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="sql",
            model=agent_client.model,
            profile="sql",
            query=query,
        )

        # Build conversation context
        from agentforge.context import PipelineContext

        history_messages = conversation_history or []
        history_messages.append({"role": "user", "content": query})
        # Inject uploaded attachments onto the user turn (worker mode — see _run_web_search).
        history_messages = _inject_attachments(agent_client, history_messages, overrides)

        ctx = PipelineContext(query=query)
        ctx.messages = history_messages

        bridge.setup_registry(rt.registry)

        agent_start = time.perf_counter()
        ctx = await _cancellable_wait(
            asyncio.to_thread(agent.run, ctx=ctx),
            cancel_event,
            timeout=_PIPELINE_TIMEOUT,
        )
        _agent_elapsed = time.perf_counter() - agent_start

        # --- Step 6: Extract tool calls + send result ---------------------
        all_tool_calls = _extract_tool_calls_with_results(ctx.metadata.get("agent_iterations", []))

        # Emit file.diff events BEFORE the result card so verified writes
        # render as a unified-diff card above the agent's natural-language
        # summary.
        await _emit_file_diff_events(all_tool_calls, send_and_persist)
        await _emit_file_compare_events(all_tool_calls, send_and_persist)
        _redact_tool_results(all_tool_calls)

        total_elapsed = time.perf_counter() - total_start

        # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
        ctx.result = _strip_wrapping_fence(ctx.result or "")

        result_msg = protocol.agent_result(
            text=ctx.result or "(no result)",
            elapsed=total_elapsed,
        )
        await send_and_persist(
            result_msg,
            msg_type="result",
            content=ctx.result,
            tool_calls=all_tool_calls if all_tool_calls else None,
        )

        # --- Step 7: Summary ----------------------------------------------
        iterations = ctx.metadata.get("agent_iterations", [])
        tool_counter: Counter = Counter()
        for it in iterations:
            for tc in it.tool_calls or []:
                tool_counter[tc["name"]] += 1

        summary_msg = protocol.agent_summary(
            iterations=len(iterations),
            elapsed=total_elapsed,
            tool_calls=sum(tool_counter.values()),
            tools=dict(tool_counter),
        )
        await send_and_persist(summary_msg, msg_type="summary")
        _persist_token_usage(db, session_id, ctx)
        await _send_context_usage(ws, db, session_id, agent_client.model)
        _store_last_exchange_from_db(db, session_id, model=agent_client.model)

        # --- Hooks: run completed + tool audit ----------------------------
        from ._hooks import hooks_log_tools, hooks_run_completed

        _tc_names = ",".join(dict(tool_counter).keys())
        await hooks_log_tools(session_id, all_tool_calls, mode="sql", model=agent_client.model)
        await _fire_post_write_hooks(
            session_id,
            all_tool_calls,
            mode="sql",
            model=agent_client.model,
        )
        await hooks_run_completed(
            session_id,
            query=query,
            mode="sql",
            model=agent_client.model,
            profile="sql",
            duration_ms=int(total_elapsed * 1000),
            iterations=len(iterations),
            tool_count=sum(tool_counter.values()),
            tools_used=_tc_names,
            result_text=ctx.result or "",
        )

        # --- Step 8: Session metadata + title -----------------------------
        db.update_session(session_id, profile="sql", model=agent_client.model)

        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(
                        {
                            "type": "session.title",
                            "session_id": session_id,
                            "title": title,
                        }
                    )
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

    except asyncio.CancelledError:
        elapsed = time.perf_counter() - total_start
        logger.info("SQL run cancelled for session %s (%.1fs)", session_id, elapsed)
        cancelled_msg = protocol.agent_cancelled(elapsed)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="agent.cancelled",
            content="Cancelled by user",
            metadata=cancelled_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(cancelled_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_cancelled

        await hooks_run_cancelled(session_id, mode="sql", duration_ms=int(elapsed * 1000))

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - total_start
        logger.error("SQL run timed out after %.1fs — session %s", elapsed, session_id)
        msg = f"Request timed out after {int(elapsed)}s — the model took too long to respond."
        error_msg = protocol.agent_error(msg, recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=msg,
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(session_id, mode="sql", duration_ms=int(elapsed * 1000), error_message=msg)

    except Exception as exc:
        logger.exception("SQL run failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc), recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="sql",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run


# ---------------------------------------------------------------------------
# Discovery handler — multi-phase investigative agent
# ---------------------------------------------------------------------------


async def _run_discovery(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run the DiscoveryRunner: scope → investigate → synthesise → present plan.

    Phase 4 (execution) happens on a follow-up "yes" from the user, which
    re-enters the agent path with the approved plan.
    """
    total_start = time.perf_counter()
    ws_closed = False

    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)

    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # --- Load conversation history ------------------------------------
        conversation_history = _build_conversation_history(
            db, session_id, incognito=(overrides or {}).get("incognito", False), query=query
        )

        # --- Persist user query -------------------------------------------
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata={"type": "query", "text": query},
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Auto-title (early — discovery runs are long, WS may close) --
        try:
            session = db.get_session(session_id)
            if session and session.title == "New chat":
                title = await asyncio.to_thread(_generate_title, query)
                db.update_session(session_id, title=title)
                if not ws_closed:
                    await ws.send_json(protocol.session_title(session_id, title))
        except Exception:
            logger.debug("Discovery auto-title failed", exc_info=True)

        # --- Step 1: Routing info ----------------------------------------
        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed("discovery", "Multi-phase investigation", 0.0)
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="discover",
            model="(planner + workers)",
            profile="discovery",
            query=query,
        )

        # --- Step 2: Import and configure ---------------------------------
        from agentforge.client import AIClient
        from agentforge.config import get_config as get_fw_config
        from agentforge.discovery import DiscoveryRunner

        fw_cfg = get_fw_config(_fw_config_path)
        planner_profile = fw_cfg.get("discovery.planner_profile", "default")
        worker_profile = fw_cfg.get("discovery.worker_profile", "fast")

        planner_client = AIClient(profile=planner_profile)
        worker_client = AIClient(profile=worker_profile)

        # Config message — emitted AFTER planner_client so the provider field
        # reflects the per-session override (deepinfra/bedrock/...) rather than
        # the protocol's "ollama" default.
        config_msg = protocol.agent_config(
            profile="discovery",
            model="(planner + workers)",
            tools=rt.tool_count,
            session_id=session_id,
            mode="discover",
            provider=planner_client.profile.provider,
        )
        await send_and_persist(config_msg, msg_type="config")

        runner = DiscoveryRunner(
            planner_client=planner_client,
            worker_client=worker_client,
            registry=rt.registry,
            cancel_event=cancel_event,
        )

        # Helper: send transient pipeline.step event (discovery phases).
        # Sync version of the _step_event helper in _run_search — discovery
        # runs inside a thread pool so we use the sync send_sync() transport.
        def _step_event(step: str, status: str, **kwargs: Any) -> None:
            if ws_closed:
                return
            elapsed = time.perf_counter() - total_start
            msg = protocol.pipeline_step(step, status, elapsed, **kwargs)
            send_sync(msg)

        # --- Phase 1: Scoping --------------------------------------------
        logger.info("[Discovery] Phase 1: Scoping — %r", query[:80])

        # Mute registry handlers during discovery (we emit our own events)
        _saved_tc = getattr(rt.registry, "_on_tool_call", None)
        _saved_tcc = getattr(rt.registry, "_on_tools_complete", None)
        rt.registry._on_tool_call = None
        rt.registry._on_tools_complete = None

        try:
            _step_event("scoping", "running", detail="Identifying investigation areas...")

            scope_start = time.perf_counter()
            areas = await _cancellable_wait(
                asyncio.to_thread(runner.scope, query + _attachment_text_block(overrides), conversation_history),
                cancel_event,
                timeout=_PIPELINE_TIMEOUT,
            )
            scope_elapsed = time.perf_counter() - scope_start

            if not areas:
                raise RuntimeError("Scoping produced no investigation areas")

            # Send scope to client
            scope_data = [
                {
                    "id": a.id,
                    "label": a.label,
                    "description": a.description,
                    "priority": a.priority,
                    "probe_commands": len(a.probe_commands),
                }
                for a in areas
            ]
            scope_msg = protocol.discovery_scope(scope_data, scope_elapsed)
            scope_msg["max_rounds"] = runner._max_rounds
            await send_and_persist(scope_msg, msg_type="discovery.scope")

            logger.info(
                "[Discovery] Phase 1 done — %d areas in %.1fs",
                len(areas),
                scope_elapsed,
            )

            _step_event("scoping", "done", area_count=len(areas))

            # --- Phase 2: Investigation -----------------------------------
            logger.info("[Discovery] Phase 2: Investigation — %d areas", len(areas))

            # Emit tool.call events for each probe command (UI compatibility)
            _tool_call_buffer: list[dict] = []

            def _area_event_callback(
                area_id: str,
                area_label: str,
                event: str,
                data: dict,
            ) -> None:
                """Stream area events to the client."""
                msg = protocol.discovery_area_event(area_id, area_label, event, data)
                send_sync(msg)

                # Also emit tool.call for commands (backward compat with Tool Calls UI)
                if event == "command":
                    tc_msg = protocol.tool_call(
                        "shell",
                        {"command": data.get("command", ""), "cwd": data.get("cwd", "")},
                    )
                    send_sync(tc_msg)
                    _tool_call_buffer.append(
                        {
                            "name": "shell",
                            "args": {"command": data.get("command", ""), "cwd": data.get("cwd", "")},
                        }
                    )

            _step_event("investigating", "running", detail=f"Probing {len(areas)} areas...", area_count=len(areas))

            invest_start = time.perf_counter()
            findings = await _cancellable_wait(
                asyncio.to_thread(
                    runner.investigate,
                    areas,
                    on_area_event=_area_event_callback,
                ),
                cancel_event,
                timeout=_PIPELINE_TIMEOUT,
            )
            invest_elapsed = time.perf_counter() - invest_start

            # Flush tool calls
            send_sync(protocol.tool_calls_flush())

            logger.info(
                "[Discovery] Phase 2 done — %d findings in %.1fs",
                len(findings),
                invest_elapsed,
            )

            _step_event("investigating", "done", finding_count=len(findings))

            # --- Phase 3: Synthesis ----------------------------------------
            logger.info("[Discovery] Phase 3: Synthesis")

            _step_event("synthesising", "running", detail="Synthesising findings into plan...")

            synth_start = time.perf_counter()
            plan = await _cancellable_wait(
                asyncio.to_thread(runner.synthesise, query, findings),
                cancel_event,
                timeout=_PIPELINE_TIMEOUT,
            )
            synth_elapsed = time.perf_counter() - synth_start

            _step_event("synthesising", "done", recommendation_count=len(plan.recommendations))

            logger.info(
                "[Discovery] Phase 3 done — %d recommendations in %.1fs",
                len(plan.recommendations),
                synth_elapsed,
            )

            # Send plan to client
            plan_msg = protocol.discovery_plan(
                summary=plan.summary,
                total_reclaimable=plan.total_reclaimable,
                recommendations=plan.recommendations,
                elapsed=synth_elapsed,
            )
            await send_and_persist(plan_msg, msg_type="discovery.plan")

            # --- Format and send result ------------------------------------
            total_elapsed = time.perf_counter() - total_start

            # Build a rich text result for the chat
            result_parts = [
                "## Discovery Report\n",
                f"**{plan.summary}**\n",
                f"Total reclaimable: **{plan.total_reclaimable}**\n",
            ]

            if plan.recommendations:
                result_parts.append("\n### Recommendations\n")
                for i, rec in enumerate(plan.recommendations, 1):
                    risk_icon = {"safe": "\u2705", "caution": "\u26a0\ufe0f", "danger": "\U0001f534"}.get(
                        rec.get("risk", ""), "\u2753"
                    )
                    sudo = " (sudo)" if rec.get("needs_sudo") else ""
                    result_parts.append(
                        f"{i}. {risk_icon} **{rec.get('area', '')}** — "
                        f"{rec.get('action', '')} — **{rec.get('size', '?')}**{sudo}"
                    )
                    for cmd in rec.get("commands", []):
                        result_parts.append(f"   `{cmd.get('command', '')}`")

            result_parts.append(
                f"\n---\n*Investigated {len(areas)} areas in {_fmt_elapsed(total_elapsed)} "
                f"(scope: {_fmt_elapsed(scope_elapsed)}, investigate: {_fmt_elapsed(invest_elapsed)}, "
                f"synthesise: {_fmt_elapsed(synth_elapsed)})*"
            )
            result_parts.append(
                "\n\n**Reply 'yes' to execute the safe recommendations, "
                "or specify which items to run (e.g., '1, 3, 5').**"
            )

            result_text = "\n".join(result_parts)

            result_msg = protocol.agent_result(result_text, total_elapsed)
            await send_and_persist(result_msg, content=result_text, msg_type="result")

            # Summary
            summary_msg = protocol.agent_summary(
                iterations=0,
                tool_calls=len(_tool_call_buffer),
                elapsed=total_elapsed,
                tools={"shell": len(_tool_call_buffer)},
            )
            await send_and_persist(summary_msg, msg_type="summary")
            _persist_token_usage_raw(
                db,
                session_id,
                runner.token_usage.get("prompt_tokens", 0),
                runner.token_usage.get("completion_tokens", 0),
            )
            await _send_context_usage(ws, db, session_id, planner_client.model)
            _store_last_exchange_from_db(db, session_id, model=planner_client.model)

            # --- Hooks: run completed + tool audit ----------------------------
            from ._hooks import hooks_run_completed

            await hooks_run_completed(
                session_id,
                query=query,
                mode="discover",
                model=planner_client.model,
                profile="discovery",
                duration_ms=int(total_elapsed * 1000),
                tool_count=len(_tool_call_buffer),
                tools_used="shell",
                result_text=result_text,
            )

            # Persist tool calls
            if _tool_call_buffer:
                db.add_message(
                    session_id=session_id,
                    role="assistant",
                    msg_type="tool_calls",
                    content=None,
                    metadata={"type": "tool_calls", "calls": _tool_call_buffer},
                )

            # Persist the plan in metadata for Phase 4 retrieval
            db.add_message(
                session_id=session_id,
                role="assistant",
                msg_type="discovery.plan",
                content=None,
                metadata={
                    "type": "discovery.plan",
                    "plan": {
                        "summary": plan.summary,
                        "total_reclaimable": plan.total_reclaimable,
                        "recommendations": plan.recommendations,
                    },
                },
            )

        finally:
            # Restore registry handlers
            rt.registry._on_tool_call = _saved_tc
            rt.registry._on_tools_complete = _saved_tcc

    except asyncio.CancelledError:
        elapsed = time.perf_counter() - total_start
        logger.info("Discovery run cancelled for session %s (%.1fs)", session_id, elapsed)
        cancelled_msg = protocol.agent_cancelled(elapsed)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="agent.cancelled",
            content="Cancelled by user",
            metadata=cancelled_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(cancelled_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_cancelled

        await hooks_run_cancelled(session_id, mode="discover", duration_ms=int(elapsed * 1000))

    except Exception as exc:
        logger.exception("Discovery run failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc), recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="discover",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run


# ---------------------------------------------------------------------------
# Custom agent execution — configuration-driven agent mode
# ---------------------------------------------------------------------------


async def _run_custom_agent(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None,
    cancel_event: threading.Event | None,
    agent_cfg: dict,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run the AgentLoop for a user-defined custom agent.

    The agent's profile, tool allowlist, system prompt, and iteration limit
    are all driven by *agent_cfg* (loaded from custom_agents.yaml at startup).
    This is structurally identical to _run_log_analysis / _run_web_search —
    the only difference is that everything is parameterised.
    """
    agent_id = agent_cfg["id"]
    profile: str = agent_cfg.get("profile", "agent")
    tools_cfg = agent_cfg.get("tools", [])
    max_iterations: int = int(agent_cfg.get("max_iterations", 10))
    iter_timeout: int = int(agent_cfg.get("iter_timeout", 600))
    description: str = agent_cfg.get("description", agent_id)
    system_prompt: str = agent_cfg.get("prompt_text", "")
    # When True: messages are persisted with is_incognito=True so within-session
    # follow-ups work, but cross-session semantic memory and fact extraction are
    # skipped.  This prevents stale data (old file listings, transfer states)
    # from leaking into future sessions while still enabling multi-turn context.
    no_history: bool = bool(agent_cfg.get("no_history", False))
    # Default fallback prompt if none was configured
    if not system_prompt:
        system_prompt = (
            f"You are a specialised assistant: {description}. Use the tools available to you to help the user."
        )

    # Inject skill instructions (full on first turn)
    system_prompt = _inject_skills(system_prompt, overrides, condensed=False)
    # Inject user context into the LOCAL system_prompt — conversation_history and
    # ctx.messages[0] are built from this var, and ctx.messages is what the agent
    # actually runs on. Injecting only into the AgentLoop arg below was a no-op
    # (ctx.messages[0] gets overwritten with the raw system_prompt).
    system_prompt = _inject_user_context(system_prompt, rt)

    # tool list: None → full registry; list → explicit allowlist
    tool_list: list[str] | None = None if tools_cfg == "all" else (tools_cfg or None)

    total_start = time.perf_counter()
    ws_closed = False

    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)

    # no_history agents always persist as incognito so within-session
    # follow-ups work, but data never leaks cross-session.
    _is_incognito = no_history or (overrides or {}).get("incognito", False)

    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
                logger.warning("WebSocket closed during send — persisting only")
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=_is_incognito,
        )

    try:
        # --- Load conversation history BEFORE persisting current query ------
        conversation_history = _build_conversation_history(
            db,
            session_id,
            incognito=_is_incognito,
            query=query,
            mode=f"custom:{agent_id}",
        )

        # --- Persist user query -------------------------------------------
        query_metadata = {"type": "query", "text": query, "mode": f"custom:{agent_id}"}
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata=query_metadata,
            is_incognito=_is_incognito,
        )

        # --- Route -------------------------------------------------------
        reason = f"@{agent_id} — {description}"
        route_elapsed = 0.0

        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed(profile, reason, route_elapsed)
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Build agent -------------------------------------------------
        from agentforge.agent import AgentLoop
        from agentforge.client import AIClient

        agent_client = AIClient(profile=profile)

        _agent_event = _make_agent_event_callback(send_sync, db, session_id, total_start)

        # Debug: verify connector tools exist in registry
        if tool_list:
            for tn in tool_list:
                logger.info("Connector agent tool check: %s in registry = %s", tn, tn in rt.registry)

        agent = AgentLoop(
            agent_client,
            rt.registry,
            system_prompt=system_prompt,  # already has skills + user_context injected above
            tools=tool_list,
            max_iterations=max_iterations,
            verbose=False,
            cancel_event=cancel_event,
            iter_timeout=iter_timeout,
            max_tool_output=12_000,
            on_event=_agent_event,
            stream_final=False,  # disabled: worker HTTP callbacks deliver chunks out-of-order
            deep_think=bool(agent_client.profile.thinking_budget),
            read_only=bool((overrides or {}).get("read_only")),
        )

        actual_tool_count = len(tool_list) if tool_list else rt.tool_count
        config_msg = protocol.agent_config(
            profile=profile,
            model=agent_client.model,
            tools=actual_tool_count,
            session_id=session_id,
            provider=agent_client.profile.provider,
            mode=f"custom:{agent_id}",
            no_history=no_history,
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode=f"custom:{agent_id}",
            model=agent_client.model,
            profile=profile,
            query=query,
        )

        # --- Run agent in thread -----------------------------------------
        from agentforge.context import PipelineContext

        conversation_history = conversation_history or [{"role": "system", "content": system_prompt}]
        conversation_history.append({"role": "user", "content": query})
        # Inject uploaded attachments onto the user turn (custom agents are a worker
        # mode — see _run_web_search).
        conversation_history = _inject_attachments(agent_client, conversation_history, overrides)
        ctx = PipelineContext(query=query)
        ctx.messages = conversation_history
        # Ensure the system prompt is up-to-date in the messages list
        if ctx.messages and ctx.messages[0].get("role") == "system":
            ctx.messages[0] = {"role": "system", "content": system_prompt}

        agent_start = time.perf_counter()
        ctx = await _cancellable_wait(
            asyncio.to_thread(agent.run, ctx=ctx),
            cancel_event,
            timeout=_PIPELINE_TIMEOUT,
        )
        _agent_elapsed = time.perf_counter() - agent_start

        # --- Extract tool calls + results from iterations ----------------
        all_tool_calls = _extract_tool_calls_with_results(ctx.metadata.get("agent_iterations", []))

        # --- Emit file.diff events BEFORE the result card ----------------
        # Any verified writes (code_edit / revert_file / write_file) render
        # as a unified-diff card above the agent's natural-language summary.
        await _emit_file_diff_events(all_tool_calls, send_and_persist)
        await _emit_file_compare_events(all_tool_calls, send_and_persist)
        _redact_tool_results(all_tool_calls)

        # --- Send result -------------------------------------------------
        total_elapsed = time.perf_counter() - total_start

        # Strip any outer ```markdown ... ``` wrapper the LLM may have added.
        ctx.result = _strip_wrapping_fence(ctx.result or "")

        result_msg = protocol.agent_result(
            text=ctx.result or "(no result)",
            elapsed=total_elapsed,
        )
        await send_and_persist(
            result_msg,
            msg_type="result",
            content=ctx.result,
            tool_calls=all_tool_calls if all_tool_calls else None,
        )

        # --- Send summary ------------------------------------------------
        iterations = ctx.metadata.get("agent_iterations", [])
        tool_counter: Counter = Counter()
        for it in iterations:
            for tc in it.tool_calls or []:
                tool_counter[tc["name"]] += 1

        n_tools = sum(tool_counter.values())
        summary_msg = protocol.agent_summary(
            iterations=len(iterations),
            elapsed=total_elapsed,
            tool_calls=n_tools,
            tools=dict(tool_counter),
        )
        await send_and_persist(summary_msg, msg_type="summary")
        _persist_token_usage(db, session_id, ctx)
        await _send_context_usage(ws, db, session_id, agent_client.model)
        # Cross-session memory + fact extraction: skip for no_history agents
        # (volatile-state modes like @cloud, @gitlab where old data goes stale)
        if not no_history:
            _store_last_exchange_from_db(db, session_id, model=agent_client.model)

        # --- Hooks: run completed + tool audit ----------------------------
        from ._hooks import hooks_log_tools, hooks_run_completed

        _tc_names = ",".join(dict(tool_counter).keys())
        _mode_label = f"custom:{agent_id}"
        await hooks_log_tools(session_id, all_tool_calls, mode=_mode_label, model=agent_client.model)
        await _fire_post_write_hooks(
            session_id,
            all_tool_calls,
            mode=_mode_label,
            model=agent_client.model,
        )
        await hooks_run_completed(
            session_id,
            query=query,
            mode=_mode_label,
            model=agent_client.model,
            profile=profile,
            duration_ms=int(total_elapsed * 1000),
            iterations=len(iterations),
            tool_count=n_tools,
            tools_used=_tc_names,
            result_text=ctx.result or "",
        )

        # --- Update session metadata -------------------------------------
        db.update_session(session_id, profile=profile, model=agent_client.model)

        # --- Auto-title on first query -----------------------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(
                        {
                            "type": "session.title",
                            "session_id": session_id,
                            "title": title,
                        }
                    )
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

        # --- Errors if any -----------------------------------------------
        if ctx.errors:
            for err in ctx.errors:
                error_msg = protocol.agent_error(str(err), recoverable=False)
                await send_and_persist(error_msg, msg_type="error", content=str(err))

    except asyncio.CancelledError:
        logger.info("Custom agent '%s' run cancelled — session %s", agent_id, session_id)
        cancelled_msg = protocol.agent_cancelled(time.perf_counter() - total_start)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="cancelled",
            content=None,
            metadata=cancelled_msg,
            is_incognito=_is_incognito,
        )
        if not ws_closed:
            try:
                await ws.send_json(cancelled_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_cancelled

        await hooks_run_cancelled(
            session_id, mode=f"custom:{agent_id}", duration_ms=int((time.perf_counter() - total_start) * 1000)
        )

    except Exception as exc:
        logger.exception("Custom agent '%s' failed for session %s", agent_id, session_id)
        error_msg = protocol.agent_error(str(exc), recoverable=False)
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
            is_incognito=_is_incognito,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode=f"custom:{agent_id}",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run


# ---------------------------------------------------------------------------
# _run_review — parallel multi-agent code review
# ---------------------------------------------------------------------------

# Sub-agent definitions: (id, label, prompt_file, description)
_REVIEW_SUB_AGENTS = [
    (
        "error_handling",
        "Error Handling",
        "error_handling.md",
        "Silent failures, swallowed exceptions, missing error propagation",
    ),
    (
        "type_design",
        "Type Design",
        "type_design.md",
        "Type safety, broad types, missing annotations, signature consistency",
    ),
    (
        "test_coverage",
        "Test Coverage",
        "test_coverage.md",
        "Untested code paths, missing edge cases, assertion quality",
    ),
    ("code_quality", "Code Quality", "code_quality.md", "Dead code, DRY violations, complexity, naming, architecture"),
]

_REVIEW_TOOLS = [
    "read_file",
    "find_files",
    "grep_text",
    "git_diff",
    "git_log",
    "git_status",
    "git_blame",
    "shell",
]


def _load_review_prompt(filename: str) -> str:
    """Load a sub-agent prompt from the agentforge prompts/review/ directory."""
    prompt_dir = _PROMPTS_DIR / "review"
    path = prompt_dir / filename
    if path.exists():
        return path.read_text()
    logger.warning("Review prompt not found: %s", path)
    return "You are a code review specialist. Review the code thoroughly."


def _extract_review_target(query: str) -> tuple[str, str]:
    """Extract the target path and clean query from a review prompt.

    Handles patterns like:
        @review Review changes in /path/to/project
        @review /path/to/project check for bugs
        Review all unpushed changes in /home/user/project for branch main
    """
    # Strip @review prefix
    clean = re.sub(r"^@review\s*", "", query, flags=re.IGNORECASE).strip()

    # Look for absolute paths
    path_match = re.search(r"(/[^\s]+)", clean)
    target = ""
    if path_match:
        candidate = path_match.group(1).rstrip(".,;:!?")
        # Only accept if it looks like a real directory path
        if "/" in candidate and len(candidate) > 3:
            target = candidate
            # Remove the path from the clean query
            clean = clean.replace(path_match.group(0), "").strip()

    # Also check for ~/paths
    if not target:
        home_match = re.search(r"(~/[^\s]+)", clean)
        if home_match:
            target = os.path.expanduser(home_match.group(1).rstrip(".,;:!?"))
            clean = clean.replace(home_match.group(0), "").strip()

    if not clean:
        clean = "Review all current and unpushed changes"

    return target, clean


# ---------------------------------------------------------------------------
# @coding / @code — map-reduce code transformer (Phase 1 stub)
# ---------------------------------------------------------------------------


async def _run_coding(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Runner for ``@coding`` / ``@code`` mode (Phase 3 dry-run).

    Pipeline: extract params → discover (``code_find``) → narrow
    (``code_narrow``) → transform (``code_transform`` burst) → verify
    (``code_verify``) → emit ``file.diff`` preview cards. No writes to
    disk — Phase 4 adds confirm + apply, Phase 5 adds the LLM planner.
    """
    total_start = time.perf_counter()
    ws_closed = False

    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # Persist user query
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata={"type": "query", "text": query},
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Auto-title (early — burst runs are long, WS may close) --
        try:
            session = db.get_session(session_id)
            if session and session.title == "New chat":
                title = await asyncio.to_thread(_generate_title, query)
                db.update_session(session_id, title=title)
                if not ws_closed:
                    await ws.send_json(protocol.session_title(session_id, title))
        except Exception:
            logger.debug("Coding auto-title failed", exc_info=True)

        # --- Undo branch (handled before normal pipeline) --------------
        # Matches `undo <burst-id>` or `revert <burst-id>`. Keeps the
        # rollback UX inside the same mode so users don't have to learn
        # a separate @coding-undo alias.
        undo_match = re.match(r"^\s*(?:undo|revert)\s+([A-Za-z0-9]{6,32})\s*$", query)
        if undo_match:
            burst_id = undo_match.group(1)
            await send_and_persist(protocol.agent_routing(), msg_type="routing")
            await send_and_persist(
                protocol.agent_routed(
                    "coding",
                    f"@coding undo {burst_id}",
                    0.0,
                ),
                msg_type="routed",
            )
            from agentforge.tools.coding_tools import code_undo

            result = await asyncio.to_thread(
                code_undo,
                session_id,
                burst_id,
            )
            reverted = result.get("reverted", []) or []
            failed = result.get("failed", []) or []
            note = result.get("note")
            lines = [f"**coding mode — undo `{burst_id}`**", ""]
            if note:
                lines.append(note)
            else:
                lines.append(f"- Reverted: **{len(reverted)}** file(s)")
                for r in reverted:
                    lines.append(f"  - `{r['path']}`")
                if failed:
                    lines.append(f"- Failed: **{len(failed)}**")
                    for f in failed:
                        lines.append(f"  - `{f.get('snapshot_id', '?')}`: {f.get('reason', 'unknown')}")
            undo_text = "\n".join(lines)
            await send_and_persist(
                protocol.agent_result(text=undo_text, elapsed=time.perf_counter() - total_start),
                msg_type="result",
                content=undo_text,
            )
            await send_and_persist(
                protocol.agent_summary(
                    iterations=1,
                    elapsed=time.perf_counter() - total_start,
                    tool_calls=len(reverted) + len(failed),
                    tools={"code_undo": 1},
                ),
                msg_type="summary",
            )
            return

        # --- Route -----------------------------------------------------
        await send_and_persist(protocol.agent_routing(), msg_type="routing")

        # @coding runs three distinct LLM jobs: planner (JSON plan),
        # extractor (NL → params fallback), and per-file bursts.
        # Each can use its own profile so planning gets a reasoning
        # model while bursts stay on a code specialist.
        from agentforge.config import get_config as _get_fw_config

        _fw = _get_fw_config()
        planner_profile = str(_fw.get("coding.planner_profile", "agent-heavy"))
        extractor_profile = str(_fw.get("coding.extractor_profile", "cloud-light"))
        burst_profile = str(_fw.get("coding.burst_profile", "coding"))

        # The route + config cards display the BURST profile — that's
        # the one that produces the actual diffs and does the bulk of
        # the work. Planner / extractor profiles are operational and
        # logged for diagnostics but don't clutter the UI.
        profile_name = burst_profile
        reason = f"@coding mode — planner={planner_profile} · extractor={extractor_profile} · bursts={burst_profile}"
        await send_and_persist(
            protocol.agent_routed(profile_name, reason, 0.0),
            msg_type="routed",
        )

        # --- Resolve profile / provider for the config card -----------
        from agentforge.client import AIClient as _AIClient

        try:
            client = _AIClient(profile=profile_name)
            model_name = client.profile.model
            provider_name = client.profile.provider
        except Exception as exc:
            logger.warning(
                "coding burst profile %r not resolvable (%s) — using placeholders",
                profile_name,
                exc,
            )
            model_name = f"({profile_name} unresolved)"
            provider_name = "unknown"

        logger.info(
            "[coding] stage profiles — planner=%r extractor=%r burst=%r (burst model=%r provider=%r)",
            planner_profile,
            extractor_profile,
            burst_profile,
            model_name,
            provider_name,
        )

        config_msg = protocol.agent_config(
            profile=profile_name,
            model=model_name,
            tools=5,  # code_find / code_narrow / code_transform / code_verify / code_apply
            session_id=session_id,
            provider=provider_name,
            mode="coding",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Build or generate a plan --------------------------------
        # Two paths: `coding.auto_planner: true` runs an LLM planner that
        # can reshape the approach; `false` always uses the fixed
        # four-step template with an LLM param extractor. A planner
        # failure (unparseable JSON, validation error, LLM exception)
        # falls back to the template automatically — the runner stays
        # usable even when the planner misbehaves.
        from agentforge.coding.driver import Plan, run_plan
        from agentforge.coding.template import build_fixed_plan, extract_params

        try:
            from agentforge.config import get_config as _get_fw_config

            auto_planner = bool(_get_fw_config().get("coding.auto_planner", True))
        except Exception:
            auto_planner = True

        plan: Plan | None = None
        raw_plan: dict | None = None
        plan_source = "template"

        if auto_planner:
            from agentforge.coding.planner import plan_from_prompt

            # Append uploaded document text so the planner accounts for the file
            # (coding is a worker mode — attachments arrive via overrides). The
            # template/extractor fallback path is left bare on purpose: param
            # extraction expects a concise prompt, not a document dump.
            planner_result = await asyncio.to_thread(
                plan_from_prompt,
                query + _attachment_text_block(overrides),
                profile=planner_profile,
            )
            if planner_result.plan is not None:
                plan = planner_result.plan
                raw_plan = planner_result.raw_plan
                plan_source = "planner"
                # The planner prompt doesn't (and shouldn't) know about
                # operator-level profile routing — inject the configured
                # burst_profile into any code_transform step that didn't
                # specify one itself.
                for step in plan.steps:
                    if step.tool == "code_transform" and not step.args.get("profile"):
                        step.args["profile"] = burst_profile
                # Move any code_verify steps to the end of the plan.
                # Verify is a post-write re-check; running it before
                # code_transform would scan unchanged files and produce
                # meaningless "all target sites addressed" results.
                # Planners occasionally emit the wrong order — fix it
                # deterministically here instead of hoping the prompt
                # is clear enough.
                non_verify = [s for s in plan.steps if s.tool != "code_verify"]
                verify_steps = [s for s in plan.steps if s.tool == "code_verify"]
                if verify_steps and plan.steps != non_verify + verify_steps:
                    logger.info(
                        "[coding] planner emitted %d code_verify step(s) out of order — moving to end of plan",
                        len(verify_steps),
                    )
                    plan.steps = non_verify + verify_steps
                    if raw_plan is not None:
                        raw_plan = {
                            **raw_plan,
                            "steps": [
                                {
                                    "tool": s.tool,
                                    "args": s.args,
                                    **({"assign": s.assign} if s.assign else {}),
                                }
                                for s in plan.steps
                            ],
                        }
            else:
                # Distinguish "planner explicitly refused" from "planner
                # plumbing failed". raw_plan is set ONLY when the LLM
                # produced parseable JSON — including the {"error": "..."}
                # shape the prompt asks for when a request doesn't fit the
                # transform vocabulary. Falling back to the template in that
                # case turns "review this YAML" into a catchall regex burst
                # that produces nonsense diffs. Surface the planner's reason
                # to the user and stop.
                raw = planner_result.raw_plan or {}
                if isinstance(raw, dict) and isinstance(raw.get("error"), str):
                    reason = raw["error"].strip()
                    msg = (
                        "**coding mode** — planner declined this prompt: "
                        f"_{reason}_\n\n"
                        "`@coding` handles structural code transformations "
                        "(find X → replace with Y across files). For review, "
                        "analysis, or open-ended questions about a file, "
                        "use `@chat`, `@docs`, or `@agent` instead."
                    )
                    await send_and_persist(
                        protocol.agent_result(text=msg, elapsed=time.perf_counter() - total_start),
                        msg_type="result",
                        content=msg,
                    )
                    await send_and_persist(
                        protocol.agent_summary(
                            iterations=1,
                            elapsed=time.perf_counter() - total_start,
                            tool_calls=0,
                            tools={},
                        ),
                        msg_type="summary",
                    )
                    return
                logger.info(
                    "[coding] planner unavailable (%s) — falling back to template",
                    planner_result.error,
                )

        if plan is None:
            params = await asyncio.to_thread(
                extract_params,
                query,
                profile=extractor_profile,
            )
            missing = [k for k in ("path", "pattern", "instruction") if not params.get(k)]
            if missing:
                msg = (
                    "**coding mode** — couldn't extract required parameters from "
                    f"your prompt: missing {', '.join(missing)}.\n\n"
                    "Either rephrase with an explicit path + transform "
                    "instruction (e.g., *'in /path/to/repo, find all X and "
                    "replace with Y'*), or fall back to `@agent` / `@pipeline` "
                    "for looser natural-language requests."
                )
                await send_and_persist(
                    protocol.agent_result(text=msg, elapsed=time.perf_counter() - total_start),
                    msg_type="result",
                    content=msg,
                )
                await send_and_persist(
                    protocol.agent_summary(
                        iterations=1,
                        elapsed=time.perf_counter() - total_start,
                        tool_calls=0,
                        tools={},
                    ),
                    msg_type="summary",
                )
                return

            # Reject catchall patterns. When the extractor LLM can't find a
            # specific anchor in the prompt it tends to emit '.*' or '^.*$'
            # — every line of every file matches, and code_transform fans out
            # one burst per (max_sites_per_file) chunk. For a 1356-line file
            # that's 68 LLM calls producing incoherent diffs because each
            # call only sees a 20-line window. Refuse before any work runs.
            pattern_raw = (params.get("pattern") or "").strip()
            pattern_stripped = pattern_raw.strip("^$").strip()
            CATCHALL_PATTERNS = {".", ".*", ".+", ".*?", "(.*)", "(.+)", ".*$", "^.*"}
            if pattern_stripped in CATCHALL_PATTERNS or pattern_stripped == "":
                msg = (
                    "**coding mode** — extracted pattern `"
                    f"{pattern_raw or '(empty)'}` matches everything, which "
                    "would fan out a per-line transform burst across the whole "
                    "file (one LLM call per ~20 lines, producing incoherent "
                    "edits). That's almost never what you want.\n\n"
                    "Either narrow the prompt to a specific construct "
                    "(*'find every `<Card>` with a `data-*` attr and remove "
                    "the attr'*), or use `@chat` / `@docs` / `@agent` for "
                    "review and analysis."
                )
                await send_and_persist(
                    protocol.agent_result(text=msg, elapsed=time.perf_counter() - total_start),
                    msg_type="result",
                    content=msg,
                )
                await send_and_persist(
                    protocol.agent_summary(
                        iterations=1,
                        elapsed=time.perf_counter() - total_start,
                        tool_calls=0,
                        tools={},
                    ),
                    msg_type="summary",
                )
                return

            plan = build_fixed_plan(params, profile=burst_profile)
            raw_plan = {
                "steps": [
                    {
                        "tool": s.tool,
                        "args": s.args,
                        **({"assign": s.assign} if s.assign else {}),
                    }
                    for s in plan.steps
                ]
            }

        # Expose the plan to the UI before we execute it — useful to
        # confirm the planner did a reasonable thing when something
        # weird happens in the results.
        if raw_plan is not None:
            await send_and_persist(
                protocol.coding_plan(
                    plan=raw_plan,
                    source=plan_source,
                    elapsed=time.perf_counter() - total_start,
                ),
                msg_type="coding.plan",
            )

        # --- Run the plan --------------------------------------------
        # Push per-step and per-file progress into the Tool Calls panel
        # so the user can see the burst lighting up file-by-file instead
        # of staring at "Agent is working…" for a minute.
        def send_sync(msg: dict) -> None:
            if ws_closed:
                return
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)
            except Exception:
                logger.debug("[coding] send_sync failed", exc_info=True)

        def _preview_args(step_tool: str, resolved: dict) -> dict:
            """Pick a handful of sensible fields to show in the Tool Calls card."""
            if step_tool == "code_find":
                return {
                    "pattern": resolved.get("pattern", ""),
                    "glob": resolved.get("glob", ""),
                    "path": resolved.get("path", ""),
                }
            if step_tool == "code_narrow":
                return {
                    "predicate_regex": resolved.get("predicate_regex", ""),
                    "invert": resolved.get("invert", False),
                }
            if step_tool == "code_transform":
                hits = resolved.get("hits") or []
                files = sorted({(h.get("file") or "") for h in hits if isinstance(h, dict)})
                return {
                    "instruction": (resolved.get("instruction") or "")[:160],
                    "hits": len(hits),
                    "files": len(files),
                }
            if step_tool == "code_verify":
                return {
                    "reverify_pattern": resolved.get("reverify_pattern") or "(skipped)",
                    "reverify_path": resolved.get("reverify_path", ""),
                }
            return {}

        # Accumulate the live tool calls so they persist with the result row
        # (the agent runners do the same). Without this the Tool Calls panel is
        # broadcast-only and vanishes when the session is reloaded from SQLite.
        coding_tool_calls: list[dict] = []

        def _on_plan_event(kind: str, **data) -> None:
            if kind != "step_start":
                return
            step = data.get("step")
            if step is None:
                return
            args = _preview_args(step.tool, data.get("resolved") or {})
            coding_tool_calls.append({"name": step.tool, "args": args})
            send_sync(protocol.tool_call(step.tool, args))

        def _on_file_progress(kind: str, file_path: str, index: int, total: int, error: str | None) -> None:
            # One tool.call per file "start" so the panel shows each
            # burst landing in order. "done" events aren't emitted — the
            # next file's start (or the final tool.calls.flush) gives
            # visual closure without doubling the panel entries.
            if kind != "start":
                return
            short = file_path.rsplit("/", 1)[-1] if file_path else f"file {index + 1}"
            send_sync(
                protocol.tool_call(
                    "code_transform",
                    {"file": short, "progress": f"{index + 1}/{total}"},
                )
            )

        from agentforge.tools import coding_tools as _coding_tools

        def _wrapped_transform(**kwargs):
            return _coding_tools.code_transform(**kwargs, on_file_progress=_on_file_progress)

        try:
            ctx = await asyncio.to_thread(
                run_plan,
                plan,
                None,
                _on_plan_event,
                {"code_transform": _wrapped_transform},
            )
        finally:
            # Close the live Tool Calls panel whether the plan completed
            # cleanly or raised — leaving it "running" would wedge the
            # UI's pulse indicator until the user reloads.
            send_sync(protocol.tool_calls_flush())

        proposed = ctx.get("proposed", []) or []
        verify = ctx.get("verify", {}) or {}

        # --- Emit one file.diff preview card per proposed change -----
        # Derive the counter from the plan's actual steps — the planner
        # path skips the template's ``params`` dict, so we can't assume
        # it's bound here. Count tool names across the steps and let
        # code_transform's count reflect how many files actually had
        # proposed diffs.
        tool_counter: dict[str, int] = {}
        for step in plan.steps:
            tool_counter[step.tool] = tool_counter.get(step.tool, 0) + 1
        if "code_transform" in tool_counter:
            tool_counter["code_transform"] = max(
                len({p["file"] for p in proposed}),
                1,
            )

        applied_count = 0
        for change in proposed:
            if change.get("error"):
                # Surface per-file errors inline so the user sees which
                # files failed; don't fabricate a diff card for them.
                err_text = f"**{change['file']}** — transform failed: `{change['error']}`"
                await send_and_persist(
                    protocol.agent_result(
                        text=err_text,
                        elapsed=time.perf_counter() - total_start,
                    ),
                    msg_type="result",
                    content=err_text,
                )
                continue

            diff_text = change.get("unified_diff", "")
            if not diff_text:
                continue
            additions = sum(1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
            deletions = sum(1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---"))
            file_diff_msg = protocol.file_diff(
                tool="code_transform",
                path=change["file"],
                pre_hash=change.get("before_hash", ""),
                post_hash="",  # Phase 4 fills this in on apply
                additions=additions,
                deletions=deletions,
                diff_text=diff_text,
                action="edited",  # Phase 4 will distinguish preview vs applied
            )
            await send_and_persist(file_diff_msg, msg_type="file_diff")
            applied_count += 1

        files_touched = len({p["file"] for p in proposed if not p.get("error")})
        files_with_errors = sum(1 for p in proposed if p.get("error"))
        surviving = len(verify.get("surviving_sites", []) or [])

        # --- Confirm + apply ------------------------------------------
        # Dry-run card emission happened inline above. Now ask the user
        # whether to write these diffs to disk. Skip the prompt entirely
        # when there's nothing valid to apply.
        applicable = [p for p in proposed if not p.get("error") and p.get("unified_diff")]

        apply_result: dict | None = None
        confirm_denied = False
        burst_id = uuid.uuid4().hex[:12]

        if applicable and broker is not None:
            confirm_prompt = f"Apply {len(applicable)} file change(s) across {files_touched} file(s)?"
            try:
                confirmed = await broker.request(confirm_prompt)
            except Exception as exc:
                logger.warning("[coding] confirm broker failed: %s", exc)
                confirmed = False

            if confirmed:
                from agentforge.tools.coding_tools import code_apply

                apply_result = await asyncio.to_thread(
                    code_apply,
                    applicable,
                    burst_id=burst_id,
                    session_id=session_id,
                )
                # Emit file.diff cards with the applied action so the UI
                # can distinguish them from the earlier previews. Prefer
                # the combined_diff the apply step produced — it covers
                # every chunk for the file (a dense file can split into
                # multiple chunks, but we write once).
                for entry in apply_result.get("applied", []):
                    diff_text = entry.get("combined_diff") or next(
                        (p["unified_diff"] for p in applicable if p["file"] == entry["file"]),
                        "",
                    )
                    additions = sum(
                        1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
                    )
                    deletions = sum(
                        1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---")
                    )
                    await send_and_persist(
                        protocol.file_diff(
                            tool="code_apply",
                            path=entry["file"],
                            pre_hash=entry["pre_hash"],
                            post_hash=entry["post_hash"],
                            snapshot_id=entry["snapshot_id"],
                            additions=additions,
                            deletions=deletions,
                            diff_text=diff_text,
                            action="written",
                        ),
                        msg_type="file_diff",
                    )
            else:
                confirm_denied = True

        # --- Verify-retry loop ----------------------------------------
        # After apply, re-verify against on-disk state. If the LLM
        # missed sites — common on prompts that span multi-line JSX
        # props where the line-oriented burst sees one line of a
        # multi-line element — retry on the surviving sites only,
        # augment the instruction with the miss list, share the burst_id
        # so all snapshots accrue under one undo handle. Loop lives in
        # framework/coding/retry.py so it can be unit-tested without the
        # WS scaffolding.
        retry_attempts = 0
        retry_surviving: list[dict] = []
        retry_applied: list[dict] = []
        if apply_result is not None and applicable:
            transform_step = next(
                (s for s in plan.steps if s.tool == "code_transform"),
                None,
            )
            verify_step = next(
                (s for s in plan.steps if s.tool == "code_verify"),
                None,
            )
            if transform_step and verify_step:
                try:
                    from agentforge.config import get_config as _gc

                    _cfg = _gc()
                    max_retries = int(_cfg.get("coding.transform_retry_max", 2) or 2)
                    retry_profile = str(_cfg.get("coding.transform_retry_profile", "") or burst_profile)
                except Exception:
                    max_retries = 2
                    retry_profile = burst_profile

                from agentforge.coding.retry import run_verify_retry
                from agentforge.tools.coding_tools import (
                    code_apply as _code_apply,
                )
                from agentforge.tools.coding_tools import (
                    code_transform as _code_transform,
                )
                from agentforge.tools.coding_tools import (
                    code_verify as _code_verify,
                )

                retry_result = await asyncio.to_thread(
                    run_verify_retry,
                    applied=list(apply_result.get("applied", []) or []),
                    instruction=transform_step.args.get("instruction", ""),
                    reverify_pattern=verify_step.args.get("reverify_pattern"),
                    reverify_path=verify_step.args.get("reverify_path", ""),
                    reverify_glob=verify_step.args.get("reverify_glob", ""),
                    burst_id=burst_id,
                    session_id=session_id,
                    max_retries=max_retries,
                    base_profile=burst_profile,
                    retry_profile=retry_profile,
                    transform_fn=_code_transform,
                    apply_fn=_code_apply,
                    verify_fn=_code_verify,
                )
                retry_attempts = retry_result.attempts
                retry_surviving = retry_result.surviving_sites
                retry_applied = retry_result.additional_applied

                # Emit file.diff cards for any retry writes so they show
                # up in the UI just like the first-pass writes.
                for entry in retry_applied:
                    diff_text = entry.get("combined_diff") or ""
                    additions = sum(
                        1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
                    )
                    deletions = sum(
                        1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---")
                    )
                    await send_and_persist(
                        protocol.file_diff(
                            tool="code_apply",
                            path=entry["file"],
                            pre_hash=entry["pre_hash"],
                            post_hash=entry["post_hash"],
                            snapshot_id=entry["snapshot_id"],
                            additions=additions,
                            deletions=deletions,
                            diff_text=diff_text,
                            action="written",
                        ),
                        msg_type="file_diff",
                    )

        # --- Summary + final result card ------------------------------
        total_elapsed = time.perf_counter() - total_start

        lines = [
            f"**coding mode** ({total_elapsed:.1f}s)",
            "",
            f"- Files touched: **{files_touched}**",
            f"- Preview cards emitted: **{applied_count}**",
        ]
        if files_with_errors:
            lines.append(f"- Files with transform errors: **{files_with_errors}**")
        if surviving:
            lines.append(
                f"- Verifier: **{surviving} site(s) would survive** the proposed "
                "changes — the transform may have missed some matches."
            )
        else:
            lines.append("- Verifier: all target sites addressed.")

        if apply_result is not None:
            applied_entries = apply_result.get("applied", []) or []
            failed_entries = apply_result.get("failed", []) or []
            a = len(applied_entries)
            f = len(failed_entries)
            partial_entries = [e for e in applied_entries if e.get("skipped_hunks")]
            lines.append("")
            if partial_entries:
                lines.append(f"- **Applied: {a}** (of which **{len(partial_entries)}** partial) · Failed: {f}")
            else:
                lines.append(f"- **Applied: {a}** · Failed: {f}")
            for entry in failed_entries:
                lines.append(f"  - `{entry.get('file', '?')}`: {entry.get('reason', '')}")
            for entry in partial_entries:
                skipped = entry.get("skipped_hunks", []) or []
                lines.append(f"  - `{entry.get('file', '?')}`: partial apply — **{len(skipped)}** hunk(s) skipped:")
                for s in skipped:
                    reason = s.get("reason", "").replace("\n", " ")
                    lines.append(f"    - hunk #{s.get('hunk_idx', '?') + 1}: {reason}")
            if retry_attempts > 0:
                extra = len(retry_applied)
                lines.append(
                    f"- Verify-retry: **{retry_attempts}** retry pass(es), **{extra}** additional file write(s)"
                )
            if retry_surviving:
                lines.append(
                    f"- Verifier: **{len(retry_surviving)}** site(s) still "
                    "match after retries — surfacing as dead-letter:"
                )
                for s in retry_surviving[:10]:
                    lines.append(
                        f"  - `{s.get('file', '?')}:{s.get('line', '?')}`: {(s.get('text') or '').strip()[:120]}"
                    )
                if len(retry_surviving) > 10:
                    lines.append(f"  - ... and {len(retry_surviving) - 10} more")
            if a > 0:
                lines.append(f"- Undo this run with: `@coding undo {burst_id}`")
        elif confirm_denied:
            lines.append("")
            lines.append("_User declined — no files written._")
        elif not applicable:
            lines.append("")
            lines.append("_No applicable changes — nothing to write._")
        else:
            lines.append("")
            lines.append("_No confirmation broker wired — changes left unapplied._")

        result_text = "\n".join(lines)

        await send_and_persist(
            protocol.agent_result(text=result_text, elapsed=total_elapsed),
            msg_type="result",
            content=result_text,
            tool_calls=coding_tool_calls or None,
        )
        if apply_result is not None and apply_result.get("applied"):
            tool_counter["code_apply"] = len(apply_result["applied"])
        await send_and_persist(
            protocol.agent_summary(
                iterations=1,
                elapsed=total_elapsed,
                tool_calls=sum(tool_counter.values()),
                tools=tool_counter,
            ),
            msg_type="summary",
        )

    except asyncio.CancelledError:
        logger.info("Coding run cancelled — session %s", session_id)
        # send_and_persist (not ws.send_json) so the agent.cancelled card
        # actually reaches the browser in worker mode — HttpCallbackSocket
        # filters out non-broadcast types, so raw ws.send_json would drop
        # the card and leave the UI stuck on "Agent is working…".
        try:
            await send_and_persist(
                protocol.agent_cancelled(time.perf_counter() - total_start),
                msg_type="cancelled",
            )
        except Exception:
            logger.debug("[coding] failed to emit cancelled card", exc_info=True)
    except Exception as exc:
        logger.exception("Coding run failed — session %s", session_id)
        try:
            await send_and_persist(
                protocol.agent_error(str(exc), recoverable=False),
                msg_type="error",
                content=str(exc),
            )
            # Also emit a terminal summary so the UI can clear the
            # spinner — otherwise the pulsating dot stays on forever.
            await send_and_persist(
                protocol.agent_summary(
                    iterations=1,
                    elapsed=time.perf_counter() - total_start,
                    tool_calls=0,
                    tools={},
                ),
                msg_type="summary",
            )
        except Exception:
            logger.debug("[coding] failed to emit error card", exc_info=True)


async def _run_review(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run parallel multi-agent code review.

    Launches 4 specialised sub-agents concurrently — each reviews the same
    code changes through a different lens.  Results are collected and merged
    into a single structured report.

    Sub-agents:
        1. Error Handling — swallowed exceptions, silent failures
        2. Type Design   — type safety, broad types, missing annotations
        3. Test Coverage  — untested paths, missing edge cases
        4. Code Quality   — dead code, DRY, complexity, naming
    """
    total_start = time.perf_counter()
    ws_closed = False

    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)

    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # --- Persist user query -------------------------------------------
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata={"type": "query", "text": query},
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Routing messages ---------------------------------------------
        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed(
            "review",
            "Parallel code review — 4 specialised sub-agents",
            0.0,
        )
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Parse target path from query ---------------------------------
        target_path, clean_query = _extract_review_target(query)

        # --- Resolve cloud-heavy ahead of the config message so the UI (and
        # the audit log) report the actual model + provider that's going to
        # run — not the literal role name. Without this, the agent.config
        # event shows "Provider: ollama" (protocol default) even when
        # provider_override has rewired cloud-heavy to DeepInfra / Bedrock /
        # OpenRouter via the override map.
        from agentforge.config import get_config as _get_framework_config

        _resolved_profile = _get_framework_config().get_profile("cloud-heavy")

        # --- Config message -----------------------------------------------
        config_msg = protocol.agent_config(
            profile="review",
            model=_resolved_profile.model,
            tools=len(_REVIEW_SUB_AGENTS),
            session_id=session_id,
            provider=_resolved_profile.provider,
            mode="review",
        )
        await send_and_persist(config_msg, msg_type="config")

        # --- Hooks: run started -------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="review",
            model=_resolved_profile.model,
            profile="review",
            query=query[:100],
        )

        # --- Pre-flight: gather git context once, share with all sub-agents --
        # Each sub-agent independently ran git status/diff before — wasteful.
        # Run the orientation commands once here and inject results so agents
        # skip straight to their speciality without duplicating setup work.
        import subprocess

        def _run_git(cmd: str) -> str:
            """Run a git command in target_path and return stdout+stderr."""
            try:
                cwd = target_path if target_path and os.path.isdir(target_path) else None
                r = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                out = r.stdout.strip()
                err = r.stderr.strip()
                return (out + ("\n" + err if err else "")).strip() or "(empty)"
            except Exception as exc:
                return f"(could not run: {exc})"

        git_status = _run_git("git status")
        git_branch = _run_git("git branch --show-current")
        git_log = _run_git("git log @{u}..HEAD --oneline 2>/dev/null || git log -10 --oneline")
        changed_files = _run_git(
            "git diff --name-only @{u}..HEAD 2>/dev/null || git diff --name-only HEAD~1..HEAD 2>/dev/null || git status --short"
        )

        # --- Build context preamble for all sub-agents --------------------
        context_preamble = f"""## Review Target

Target directory: `{target_path or "(current working directory)"}`
Current branch: `{git_branch}`
User instruction: {clean_query}

## Pre-gathered Git Context (do NOT re-run these — results already below)

### git status
```
{git_status}
```

### Changed files vs remote
```
{changed_files}
```

### Unpushed commits
```
{git_log}
```

## Your Task

Focus your review on the changed files listed above. Use `read_file`, `grep_text`, and `git_diff` (or `git_blame`) to read and analyse the actual code. Do NOT re-run `git status` or `git branch` — the output is already provided above.
"""

        # Append uploaded document text so every review sub-agent sees it
        # (review is a worker mode — attachments arrive via overrides).
        context_preamble += _attachment_text_block(overrides)

        # --- Run 4 sub-agents in parallel ---------------------------------
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from agentforge.agent import AgentLoop
        from agentforge.client import AIClient

        agent_client = AIClient(profile="cloud-heavy")

        sub_results: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}

        def _run_sub_agent(
            agent_id: str, label: str, prompt_file: str, description: str
        ) -> tuple[str, str, list, dict]:
            """Run a single sub-agent in its own thread. Returns (agent_id, result_text, tool_calls, token_usage)."""
            try:
                prompt_text = _load_review_prompt(prompt_file)
                full_prompt = f"{prompt_text}\n\n{context_preamble}"

                sub_agent = AgentLoop(
                    agent_client,
                    rt.registry,
                    system_prompt=full_prompt,
                    tools=_REVIEW_TOOLS,
                    max_iterations=15,
                    verbose=False,
                    cancel_event=cancel_event,
                    deep_think=bool(agent_client.profile.thinking_budget),
                )
                ctx = sub_agent.run(clean_query)
                # AgentLoop.run() returns a PipelineContext — extract .result string
                sub_tokens: dict[str, int] = {}
                if ctx is not None:
                    result_text = ctx.result if hasattr(ctx, "result") and isinstance(ctx.result, str) else str(ctx)
                    sub_tokens = (ctx.metadata or {}).get("token_usage", {})
                else:
                    result_text = ""
                tool_calls = []
                for it in sub_agent._iterations if hasattr(sub_agent, "_iterations") else []:
                    for tc in it.get("tool_calls", []):
                        tool_calls.append({"name": tc.get("name", "?"), "args": tc.get("args", {})})

                return agent_id, result_text or "(no findings)", tool_calls, sub_tokens
            except Exception as e:
                logger.exception("Review sub-agent '%s' failed", agent_id)
                return agent_id, f"ERROR: {e}", [], {}

        # Send progress: starting sub-agents
        progress_msg = {
            "type": "review.progress",
            "phase": "starting",
            "sub_agents": [{"id": sa[0], "label": sa[1], "description": sa[3]} for sa in _REVIEW_SUB_AGENTS],
        }
        if not ws_closed:
            try:
                await ws.send_json(progress_msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True

        # Launch all sub-agents in parallel
        from app.config import settings as _af_settings

        _review_subagent_timeout = _af_settings.review.subagent_timeout_seconds
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="review") as executor:
            futures = {
                executor.submit(_run_sub_agent, sa_id, sa_label, sa_file, sa_desc): sa_id
                for sa_id, sa_label, sa_file, sa_desc in _REVIEW_SUB_AGENTS
            }

            _review_prompt_tokens = 0
            _review_completion_tokens = 0
            for future in as_completed(futures):
                agent_id = futures[future]
                try:
                    aid, result_text, tool_calls, sub_tokens = future.result(timeout=_review_subagent_timeout)
                    sub_results[aid] = {
                        "text": result_text,
                        "tool_calls": tool_calls,
                    }
                    _review_prompt_tokens += sub_tokens.get("prompt_tokens", 0)
                    _review_completion_tokens += sub_tokens.get("completion_tokens", 0)
                    # Send progress: sub-agent completed
                    if not ws_closed:
                        try:
                            await ws.send_json(
                                {
                                    "type": "review.progress",
                                    "phase": "completed",
                                    "agent_id": aid,
                                    "findings_preview": result_text[:200],
                                }
                            )
                        except (WebSocketDisconnect, RuntimeError):
                            ws_closed = True
                except Exception as e:
                    errors[agent_id] = str(e)

        # --- Build combined report ----------------------------------------
        elapsed = time.perf_counter() - total_start
        total_tools = sum(len(sr["tool_calls"]) for sr in sub_results.values())

        report_parts = []
        report_parts.append("# Code Review Report")
        if target_path:
            report_parts.append(f"\n**Target**: `{target_path}`")
        report_parts.append(
            f"**Duration**: {elapsed:.1f}s | **Sub-agents**: {len(sub_results)}/{len(_REVIEW_SUB_AGENTS)} | **Tool calls**: {total_tools}"
        )
        report_parts.append("")

        all_tool_calls = []
        for sa_id, sa_label, sa_file, sa_desc in _REVIEW_SUB_AGENTS:
            sr = sub_results.get(sa_id)
            if sr:
                report_parts.append(f"---\n\n## {sa_label}")
                report_parts.append(f"*{sa_desc}*\n")
                report_parts.append(sr["text"])
                report_parts.append("")
                all_tool_calls.extend(sr["tool_calls"])
            elif sa_id in errors:
                report_parts.append(f"---\n\n## {sa_label}")
                report_parts.append(f"⚠️ Sub-agent failed: {errors[sa_id]}\n")

        combined_report = "\n".join(report_parts)

        # --- Send result + summary ----------------------------------------
        result_msg = protocol.agent_result(combined_report, elapsed)
        await send_and_persist(
            result_msg,
            msg_type="result",
            content=combined_report,
            tool_calls=all_tool_calls[:50],  # cap persisted tool calls
        )

        summary_data = {
            "type": "agent.summary",
            "iterations": sum(1 for _ in sub_results.values()),
            "elapsed": round(elapsed, 2),
            "tool_calls": total_tools,
            "tools": ", ".join(sorted({tc["name"] for tc in all_tool_calls})) if all_tool_calls else "",
        }
        await send_and_persist(summary_data, msg_type="summary")

        # --- Hooks: run completed -----------------------------------------
        from ._hooks import hooks_run_completed

        await hooks_run_completed(
            session_id,
            query=query,
            mode="review",
            model=_resolved_profile.model,
            profile="review",
            duration_ms=int(elapsed * 1000),
            iterations=len(sub_results),
            tool_count=total_tools,
            result_text=combined_report[:2000],
        )

        # --- Token + context usage ----------------------------------------
        # Pass the real model string so context-window sizing uses the right
        # entry in _MODEL_CONTEXT_SIZES (e.g., 131K for gpt-oss-120b, not the
        # unknown "cloud-heavy" role name which would fall back to default).
        _persist_token_usage_raw(db, session_id, _review_prompt_tokens, _review_completion_tokens)
        await _send_context_usage(ws, db, session_id, _resolved_profile.model)

    except asyncio.CancelledError:
        elapsed_ms = int((time.perf_counter() - total_start) * 1000)
        cancel_msg = protocol.agent_cancelled(elapsed_ms / 1000)
        await send_and_persist(cancel_msg, msg_type="cancelled")
        from ._hooks import hooks_run_cancelled

        await hooks_run_cancelled(session_id, mode="review", duration_ms=elapsed_ms)

    except Exception as exc:
        logger.exception("Review mode failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc))
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="review",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run


# ---------------------------------------------------------------------------
# _run_research — parallel multi-agent web research
# ---------------------------------------------------------------------------

# Tools available to each research sub-agent
_RESEARCH_TOOLS = [
    "web_search",
    "web_fetch",  # fast raw-HTML fetch (static sites, docs)
    "web_fetch_rendered",  # headless Firefox via sidecar — JS-rendered / bot-protected
    "web_screengrab",  # on-demand full-page screenshot
    "write_file",  # save findings to a file if asked
]

# Complexity → profile mapping
_RESEARCH_PROFILES = {
    "simple": "cloud-light",
    "medium": "cloud-heavy",
    "complex": "cloud-heavy",
}

# Max concurrent sub-agents — IO-bound so higher than review's 4
_RESEARCH_MAX_WORKERS = 8


async def _run_research(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker: "SecretBroker | None" = None,
) -> None:
    """Run parallel multi-agent web research.

    Phase A — Planning: decompose the query into independent sub-investigations.
    Phase B — Parallel execution: each sub-agent searches and fetches web content.
    Phase C — Aggregation: merge all findings into a structured report.
    """
    total_start = time.perf_counter()
    ws_closed = False

    def send_sync(msg: dict) -> None:
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)

    async def send_and_persist(
        msg: dict,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
        db.add_message(
            session_id=session_id,
            role=role,
            msg_type=msg_type or msg["type"],
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    try:
        # --- Persist user query -----------------------------------------------
        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata={"type": "query", "text": query, "mode": "research"},
            is_incognito=(overrides or {}).get("incognito", False),
        )

        # --- Routing messages -------------------------------------------------
        routing_msg = protocol.agent_routing()
        await send_and_persist(routing_msg, msg_type="routing")

        routed_msg = protocol.agent_routed(
            "research",
            "@research mode — parallel web research",
            0.0,
        )
        await send_and_persist(routed_msg, msg_type="routed")

        # --- Auto-title on first query ----------------------------------------
        session = db.get_session(session_id)
        if session and session.title == "New chat":
            title = await asyncio.to_thread(_generate_title, query)
            db.update_session(session_id, title=title)
            if not ws_closed:
                try:
                    await ws.send_json(protocol.session_title(session_id, title))
                except (WebSocketDisconnect, RuntimeError):
                    pass

        # --- Hooks: run started -----------------------------------------------
        from ._hooks import hooks_run_started

        await hooks_run_started(
            session_id,
            mode="research",
            model="cloud-heavy",
            profile="research",
            query=query[:100],
        )

        # =====================================================================
        # Phase A — Planning: decompose query into sub-investigations
        # =====================================================================
        from agentforge.agent import AgentLoop
        from agentforge.client import AIClient

        planner_prompt = _load_prompt("research_planner")
        # Use cloud-heavy for planning — reliable structured JSON with 3-8 sub-agents.
        # The planner call happens only once per research run so the extra cost is justified.
        planner_client = AIClient(profile="cloud-heavy")

        def _call_planner(client: "AIClient", prompt_text: str, user_query: str) -> list[dict]:
            """Call the planner and return parsed sub_agent specs, or [] on failure."""
            from agentforge.backends._retry import retry_call

            def _inner():
                return client.chat(
                    messages=[
                        {"role": "system", "content": prompt_text},
                        {"role": "user", "content": user_query},
                    ],
                    temperature=0.3,
                )

            resp = retry_call(_inner, max_attempts=3, context="research-planner")
            raw = (resp.content or "").strip()
            # Strip code fences if model wraps output in them despite instructions
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            plan = json.loads(raw)
            return plan.get("sub_agents", [])

        # Append uploaded document text so the planner decomposes with the file in
        # mind (research is a worker mode — attachments arrive via overrides).
        planner_query = query + _attachment_text_block(overrides)

        planner_start = time.perf_counter()
        sub_agent_specs: list[dict] = []

        try:
            sub_agent_specs = _call_planner(planner_client, planner_prompt, planner_query)
        except (json.JSONDecodeError, KeyError, Exception) as exc:
            logger.warning("Research planner attempt 1 failed: %s", exc)

        # If the planner returned a degenerate plan (0–2 sub-agents), retry once
        # with a reinforced instruction appended to the user message.
        if len(sub_agent_specs) < 3:
            logger.info(
                "Research planner returned %d sub-agents — retrying with stronger instruction",
                len(sub_agent_specs),
            )
            retry_query = (
                f"{planner_query}\n\n"
                "IMPORTANT: You MUST return at least 3 independent sub_agents covering different "
                "angles/sources. Do NOT return a single generic 'Web Research' agent."
            )
            try:
                sub_agent_specs = _call_planner(planner_client, planner_prompt, retry_query)
            except (json.JSONDecodeError, KeyError, Exception) as exc2:
                logger.warning("Research planner retry also failed: %s", exc2)

        # Hard fallback: still degenerate after retry — build a minimal 3-agent plan
        if len(sub_agent_specs) < 3:
            logger.warning("Research planner degenerate after retry — using built-in fallback plan")
            sub_agent_specs = [
                {
                    "id": "overview_search",
                    "label": "Overview & Official Sources",
                    "strategy": f"Search for official documentation, release notes, and authoritative sources for: {query}",
                    "sources_hint": [f"{query} official documentation", f"{query} release notes"],
                    "complexity": "medium",
                    "needs_sidecar": False,
                },
                {
                    "id": "community_discussion",
                    "label": "Community & Forum Discussion",
                    "strategy": f"Search Reddit, Stack Overflow, and Hacker News for community discussion, real-world experiences, and known issues related to: {query}",
                    "sources_hint": [f"site:reddit.com {query}", f"site:stackoverflow.com {query}"],
                    "complexity": "medium",
                    "needs_sidecar": False,
                },
                {
                    "id": "recent_articles",
                    "label": "Recent Articles & Blog Posts",
                    "strategy": f"Find recent blog posts, tutorials, and technical articles from 2024-2025 covering: {query}",
                    "sources_hint": [f"{query} 2025", f"{query} guide tutorial"],
                    "complexity": "medium",
                    "needs_sidecar": False,
                },
            ]

        planner_elapsed = time.perf_counter() - planner_start

        # Cap at 8 sub-agents to keep resource usage reasonable
        sub_agent_specs = sub_agent_specs[:8]

        # Send research.plan to client
        plan_msg = {
            "type": "research.plan",
            "sub_agents": [
                {
                    "id": sa["id"],
                    "label": sa["label"],
                    "strategy": sa["strategy"],
                    "complexity": sa.get("complexity", "medium"),
                }
                for sa in sub_agent_specs
            ],
            "planner_elapsed": round(planner_elapsed, 2),
        }
        await send_and_persist(plan_msg, msg_type="research.plan", content=json.dumps(plan_msg))

        # --- Config message ---------------------------------------------------
        # Provider mirrors whatever the planner_client resolved to under the
        # active per-session override. Sub-agents use the same profile chain,
        # so this is representative of the run as a whole.
        config_msg = protocol.agent_config(
            profile="research",
            model="mixed (per sub-agent complexity)",
            tools=len(_RESEARCH_TOOLS),
            session_id=session_id,
            mode="research",
            provider=planner_client.profile.provider,
        )
        await send_and_persist(config_msg, msg_type="config")

        # =====================================================================
        # Phase B — Parallel execution: one AgentLoop per sub-investigation
        # =====================================================================
        import queue as _queue
        from concurrent.futures import ThreadPoolExecutor

        research_agent_template = _load_prompt("research_agent")

        sub_results: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}

        # Thread-safe queue for tool activity events from sub-agent threads.
        # Drained by the main async loop to push WS messages.
        activity_q: _queue.Queue = _queue.Queue()
        agent_activities: dict[str, list[dict]] = {}

        def _make_sub_agent_event_handler(agent_id: str):
            """Create an on_event callback for a research sub-agent that pushes
            tool_exec events onto the activity queue for WS forwarding."""

            def _handler(kind: str, data: dict):
                if kind == "tool_exec":
                    activity_q.put(
                        {
                            "type": "research.activity",
                            "agent_id": agent_id,
                            "tool": data.get("name", "?"),
                            "status": data.get("status", "running"),
                            "args_preview": _format_tool_args_preview(data.get("args", {}))
                            if data.get("status") == "running"
                            else None,
                            "elapsed": data.get("tool_elapsed") if data.get("status") == "done" else None,
                        }
                    )

            return _handler

        def _format_tool_args_preview(args: dict) -> str:
            """Build a compact one-liner from tool args for UI display."""
            if not args:
                return ""
            # For web_search show query, for web_fetch show url
            if "query" in args:
                return str(args["query"])[:120]
            if "url" in args:
                return str(args["url"])[:120]
            # Generic: first value
            first_val = next(iter(args.values()), "")
            return str(first_val)[:120]

        def _run_research_sub_agent(spec: dict) -> tuple[str, str, list, dict]:
            """Run a single research sub-agent. Returns (id, findings, tool_calls, token_usage)."""
            agent_id = spec["id"]
            try:
                complexity = spec.get("complexity", "medium")
                profile_name = _RESEARCH_PROFILES.get(complexity, "cloud-heavy")

                # Build sub-agent system prompt from template
                sources_hint_str = (
                    "\n".join(f"- {s}" for s in spec.get("sources_hint", []))
                    or "(none — use web_search to find sources)"
                )
                sub_prompt = research_agent_template.format(
                    strategy=spec["strategy"],
                    sources_hint=sources_hint_str,
                )
                sub_prompt = _inject_user_context(sub_prompt, rt)

                # Select tool subset: include web_fetch_rendered only if sidecar needed
                if spec.get("needs_sidecar", False):
                    tools = _RESEARCH_TOOLS
                else:
                    tools = [t for t in _RESEARCH_TOOLS if t != "web_fetch_rendered"]

                sub_client = AIClient(profile=profile_name)
                sub_agent = AgentLoop(
                    sub_client,
                    rt.registry,
                    system_prompt=sub_prompt,
                    tools=tools,
                    max_iterations=12,
                    verbose=False,
                    cancel_event=cancel_event,
                    on_event=_make_sub_agent_event_handler(agent_id),
                    # Cap each tool result at 5 KB to prevent context blowup
                    # from large web_fetch responses causing Ollama 500 errors.
                    max_tool_output=5_000,
                    deep_think=bool(sub_client.profile.thinking_budget),
                )

                ctx = sub_agent.run(spec["strategy"])
                sub_tokens: dict[str, int] = {}
                if ctx is not None:
                    result_text = ctx.result if hasattr(ctx, "result") and isinstance(ctx.result, str) else str(ctx)
                    sub_tokens = (ctx.metadata or {}).get("token_usage", {})
                else:
                    result_text = ""

                tool_calls = []
                for it in sub_agent._iterations if hasattr(sub_agent, "_iterations") else []:
                    for tc in it.get("tool_calls", []):
                        tool_calls.append({"name": tc.get("name", "?"), "args": tc.get("args", {})})

                return agent_id, result_text or "(no findings)", tool_calls, sub_tokens
            except Exception as e:
                logger.exception("Research sub-agent '%s' failed", agent_id)
                return agent_id, f"ERROR: {e}", [], {}

        # Send progress: starting sub-agents
        progress_start = {
            "type": "research.progress",
            "phase": "starting",
            "sub_agents": [{"id": sa["id"], "label": sa["label"]} for sa in sub_agent_specs],
        }
        if not ws_closed:
            try:
                await ws.send_json(progress_start)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True

        async def _drain_activity_queue():
            """Drain all pending activity events from the queue and send via WS."""
            nonlocal ws_closed
            while not activity_q.empty():
                try:
                    evt = activity_q.get_nowait()
                except _queue.Empty:
                    break
                # Accumulate for persistence so "details" survives reload
                aid = evt.get("agent_id")
                if aid:
                    agent_activities.setdefault(aid, []).append(
                        {
                            "tool": evt.get("tool", "?"),
                            "status": evt.get("status", "done"),
                            "args_preview": evt.get("args_preview"),
                            "elapsed": evt.get("elapsed"),
                        }
                    )
                if not ws_closed:
                    try:
                        await ws.send_json(evt)
                    except (WebSocketDisconnect, RuntimeError):
                        ws_closed = True

        # Launch all sub-agents in parallel
        with ThreadPoolExecutor(max_workers=_RESEARCH_MAX_WORKERS, thread_name_prefix="research") as executor:
            thread_futures = {executor.submit(_run_research_sub_agent, spec): spec["id"] for spec in sub_agent_specs}

            # Wrap thread futures as asyncio futures so we can await them
            # without blocking the event loop (allows activity pump to run).
            pending_async = {asyncio.wrap_future(tf): agent_id for tf, agent_id in thread_futures.items()}

            _research_prompt_tokens = 0
            _research_completion_tokens = 0
            while pending_async:
                # Wait for the next sub-agent to complete OR drain activity every 200ms
                done, _ = await asyncio.wait(
                    pending_async.keys(),
                    timeout=0.2,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Always drain activity events
                await _drain_activity_queue()

                for af in done:
                    agent_id = pending_async.pop(af)
                    try:
                        aid, result_text, tool_calls, sub_tokens = af.result()
                        sub_results[aid] = {
                            "text": result_text,
                            "tool_calls": tool_calls,
                            "label": next((sa["label"] for sa in sub_agent_specs if sa["id"] == aid), aid),
                        }
                        _research_prompt_tokens += sub_tokens.get("prompt_tokens", 0)
                        _research_completion_tokens += sub_tokens.get("completion_tokens", 0)
                        # Send progress: sub-agent completed
                        if not ws_closed:
                            try:
                                await ws.send_json(
                                    {
                                        "type": "research.progress",
                                        "phase": "completed",
                                        "agent_id": aid,
                                        "label": sub_results[aid]["label"],
                                        "findings_preview": result_text[:300],
                                        "tool_count": len(tool_calls),
                                    }
                                )
                            except (WebSocketDisconnect, RuntimeError):
                                ws_closed = True
                    except Exception as e:
                        errors[agent_id] = str(e)

            # Final drain
            await _drain_activity_queue()

        # Persist final progress state back to the research.plan DB message
        # so the UI can restore checkmarks/errors after page reload.
        final_progress: dict[str, dict] = {}
        for spec in sub_agent_specs:
            aid = spec["id"]
            sr = sub_results.get(aid)
            if sr:
                final_progress[aid] = {
                    "status": "completed",
                    "tool_count": len(sr["tool_calls"]),
                    "activity": agent_activities.get(aid, []),
                }
            elif aid in errors:
                final_progress[aid] = {
                    "status": "error",
                    "tool_count": 0,
                    "activity": agent_activities.get(aid, []),
                }
            else:
                final_progress[aid] = {
                    "status": "running",
                    "tool_count": 0,
                    "activity": agent_activities.get(aid, []),
                }
        try:
            db.update_message_metadata(
                session_id,
                "research.plan",
                {
                    "progress": final_progress,
                    "aggregation": {
                        "status": "completed",
                        "sources_count": len(sub_results),
                    },
                },
            )
        except Exception:
            logger.debug("Failed to persist research progress to DB", exc_info=True)

        # =====================================================================
        # Phase C — Aggregation: merge findings into a single report
        # =====================================================================
        aggregation_start = time.perf_counter()

        # Build the findings block for the aggregator
        findings_parts = []
        for spec in sub_agent_specs:
            sr = sub_results.get(spec["id"])
            if sr:
                findings_parts.append(f"### {sr['label']}\n\n{sr['text']}")
            elif spec["id"] in errors:
                findings_parts.append(f"### {spec['label']}\n\n⚠️ Sub-agent failed: {errors[spec['id']]}")

        findings_text = "\n\n---\n\n".join(findings_parts)

        # If only one sub-agent (fallback), skip aggregation — use its output directly
        if len(sub_agent_specs) <= 1 and len(sub_results) == 1:
            combined_report = next(iter(sub_results.values()))["text"]
        else:
            # Notify client that aggregation is starting
            if not ws_closed:
                try:
                    await ws.send_json(
                        {
                            "type": "research.progress",
                            "phase": "aggregating",
                            "sub_agents_completed": len(sub_results),
                            "sub_agents_failed": len(errors),
                        }
                    )
                except (WebSocketDisconnect, RuntimeError):
                    ws_closed = True

            aggregator_template = _load_prompt("research_aggregator")
            aggregator_prompt = aggregator_template.format(
                query=query,
                findings=findings_text,
            )

            from agentforge.backends._retry import retry_call

            aggregator_client = AIClient(profile="cloud-heavy")

            def _agg_call():
                return aggregator_client.chat(
                    messages=[
                        {"role": "system", "content": aggregator_prompt},
                        {"role": "user", "content": "Produce the merged research report now."},
                    ],
                    temperature=0.3,
                )

            agg_resp = retry_call(_agg_call, max_attempts=3, context="research-aggregator")
            _research_prompt_tokens += getattr(agg_resp, "prompt_tokens", 0) or 0
            _research_completion_tokens += getattr(agg_resp, "completion_tokens", 0) or 0
            combined_report = _strip_wrapping_fence((agg_resp.content or "").strip())
            if not combined_report:
                # Fallback: concatenate sub-agent findings
                combined_report = f"# Research Results\n\n{findings_text}"

        aggregation_elapsed = time.perf_counter() - aggregation_start

        # Notify client that aggregation is done
        if not ws_closed:
            try:
                await ws.send_json(
                    {
                        "type": "research.progress",
                        "phase": "aggregated",
                        "aggregation_elapsed": round(aggregation_elapsed, 2),
                    }
                )
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True

        # --- Send result + summary --------------------------------------------
        elapsed = time.perf_counter() - total_start
        all_tool_calls = []
        for sr in sub_results.values():
            all_tool_calls.extend(sr["tool_calls"])
        total_tools = len(all_tool_calls)

        result_msg = protocol.agent_result(combined_report, elapsed)
        await send_and_persist(
            result_msg,
            msg_type="result",
            content=combined_report,
            tool_calls=all_tool_calls[:50],
        )

        summary_data = {
            "type": "agent.summary",
            "iterations": len(sub_results),
            "elapsed": round(elapsed, 2),
            "tool_calls": total_tools,
            "tools": ", ".join(sorted({tc["name"] for tc in all_tool_calls})) if all_tool_calls else "",
            "research_stats": {
                "sub_agents_planned": len(sub_agent_specs),
                "sub_agents_completed": len(sub_results),
                "sub_agents_failed": len(errors),
                "planner_elapsed": round(planner_elapsed, 2),
                "aggregation_elapsed": round(aggregation_elapsed, 2),
            },
        }
        await send_and_persist(summary_data, msg_type="summary")

        # --- Hooks: run completed ---------------------------------------------
        from ._hooks import hooks_run_completed

        await hooks_run_completed(
            session_id,
            query=query,
            mode="research",
            model="cloud-heavy",
            profile="research",
            duration_ms=int(elapsed * 1000),
            iterations=len(sub_results),
            tool_count=total_tools,
            result_text=combined_report[:2000],
        )

        # --- Token + context usage --------------------------------------------
        _persist_token_usage_raw(db, session_id, _research_prompt_tokens, _research_completion_tokens)
        await _send_context_usage(ws, db, session_id, "cloud-heavy")

    except asyncio.CancelledError:
        elapsed_ms = int((time.perf_counter() - total_start) * 1000)
        cancel_msg = protocol.agent_cancelled(elapsed_ms / 1000)
        await send_and_persist(cancel_msg, msg_type="cancelled")
        from ._hooks import hooks_run_cancelled

        await hooks_run_cancelled(session_id, mode="research", duration_ms=elapsed_ms)

    except Exception as exc:
        logger.exception("Research mode failed for session %s", session_id)
        error_msg = protocol.agent_error(str(exc))
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type="error",
            content=str(exc),
            metadata=error_msg,
        )
        if not ws_closed:
            try:
                await ws.send_json(error_msg)
            except (WebSocketDisconnect, RuntimeError):
                pass
        from ._hooks import hooks_run_error

        await hooks_run_error(
            session_id,
            mode="research",
            duration_ms=int((time.perf_counter() - total_start) * 1000),
            error_message=str(exc),
        )
    finally:
        bridge.close()  # reset the in-process sudo provider + cache after the run
