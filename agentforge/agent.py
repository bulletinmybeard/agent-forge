"""AgentLoop — iterative think/act/observe cycle with tool calling.

The agent repeatedly asks the model what to do, executes the requested tools,
feeds the results back, and loops until the model produces a final text answer
(no more tool calls) or the iteration limit is reached.

Usage::

    from agentforge.agent import AgentLoop
    from agentforge.client import AIClient
    from agentforge.tools import ToolRegistry
    from agentforge.builtin_tools import register_builtin_tools

    client = AIClient()
    registry = ToolRegistry()
    register_builtin_tools(registry)

    agent = AgentLoop(client, registry, max_iterations=10)
    result = agent.run("What files are in /tmp and how large is each?")
    print(result.result)
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import difflib
import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chalkbox.logging.bridge import get_logger

from agentforge.tools._file_snapshots import save_snapshot
from agentforge.tools.routing import dispatch_mode, my_role

from .client import AIClient
from .config import get_config
from .context import PipelineContext
from .tools import ToolRegistry

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Verbatim output detection
# ---------------------------------------------------------------------------
# Tool names whose output should be presented verbatim when the user's query
# indicates they want to *see* the raw content (not a summary).
_FILE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "cat_file",
        "view_file",
        "read",
    }
)
# Tools whose output should be presented verbatim when diffing/comparing.
_DIFF_TOOLS: frozenset[str] = frozenset(
    {
        "diff_files",
        "compare_files",
    }
)

# Regex that matches queries asking to SEE file content rather than analyse it.
# Deliberately excludes "analyse", "explain", "summarise", etc.
_VERBATIM_RE = re.compile(
    r"\b(?:"
    r"show\s+(?:me\s+)?(?:the\s+)?(?:full|complete|entire|raw|whole)?\s*(?:file\s+)?content"
    r"|(?:full|complete|entire|raw|whole)\s+(?:file\s+)?content"
    r"|print\s+(?:the\s+)?(?:full|complete|entire|raw|whole)?\s*(?:file|content)"
    r"|cat\s+(?:the\s+)?(?:file|content)"
    r"|output\s+(?:the\s+)?(?:full|complete|entire|raw|whole)?\s*(?:file|content)"
    r"|display\s+(?:the\s+)?(?:full|complete|entire|raw|whole)?\s*(?:file|content)"
    r"|dump\s+(?:the\s+)?(?:file|content)"
    r"|verbatim"
    r")\b",
    re.IGNORECASE,
)

# Regex for queries that want to see actual diff output, not a summary.
_DIFF_VERBATIM_RE = re.compile(
    r"\b(?:"
    r"show\s+(?:me\s+)?(?:the\s+)?(?:diff|difference)"
    r"|(?:diff|compare)\b"
    r")\b",
    re.IGNORECASE,
)


def _wants_verbatim_output(query: str) -> bool:
    """Return *True* if the user's query signals they want raw file content."""
    return bool(_VERBATIM_RE.search(query))


def _wants_diff_verbatim(query: str) -> bool:
    """Return *True* if the user's query signals they want to see a diff."""
    return bool(_DIFF_VERBATIM_RE.search(query))


# Past-tense / result-claim markers that, combined with naming an offered tool,
# signal the model is reporting tool execution it never performed.
_FABRICATED_RESULT_RE = re.compile(
    r"(?:\bdownloaded\b|\bsaved\b|\bwritten\b|\bcreated\b|\bdeleted\b|\bexecuted\b|"
    r"\bsuccessfully\b|✅|✓|\bdone\b\s*[:|]|status\s*[:|])",
    re.IGNORECASE,
)


def _looks_like_fabricated_tool_use(text: str, offered_tools: list[str] | None) -> bool:
    """Heuristic: did a text-only answer narrate tool calls it never made?

    Fires on either of two shapes (used only to gate a one-time corrective
    nudge when NO real tool ran this run, so a rare false positive is harmless):

    1. **Pseudo-invocation** — an offered tool name at the *start of a line*
       followed by argument-like text (e.g., a ```download_file https://…```
       code block). This is what a model emits when it writes the call as text
       instead of actually invoking it, whether or not it claims success.
    2. **Fabricated result** — names an offered tool AND carries a result/
       success claim (``Downloaded``, ``✅``, a status table, …).

    A start-of-line match avoids tripping on legitimate inline how-to prose like
    "use the ``download_file`` tool", where the name isn't followed by raw args.
    """
    if not text or not offered_tools:
        return False
    alt = "|".join(re.escape(name) for name in offered_tools)
    # (1) line-leading pseudo-invocation: "<tool> <arg…>"
    if re.search(rf"^\s*(?:{alt})\s+\S", text, re.MULTILINE):
        return True
    # (2) names a tool + claims a result it never produced
    names_a_tool = re.search(rf"\b(?:{alt})\b", text) is not None
    return names_a_tool and bool(_FABRICATED_RESULT_RE.search(text))


# Default system prompt that teaches the model the think/act/observe pattern
DEFAULT_AGENT_SYSTEM_PROMPT = """\
You are a helpful AI assistant with access to tools.

IMPORTANT RULES:
1. For pure general-knowledge questions that require no local state (math, \
definitions, history, language questions), respond IMMEDIATELY with a direct \
text answer and do NOT call any tools.
2. Use tools WHENEVER the request involves the local filesystem, running \
commands, reading or writing files, reverting edits, or accessing external \
resources. Examples that ALWAYS require a tool call: "edit X", "revert X", \
"put the deleted lines back", "undo my last change", "show me file X", \
"list files in …", "what's in ~/.zshrc", "run …", "check …".
3. NEVER fabricate a tool result. You MUST NOT pretend that a file was \
edited, reverted, written, or read unless you actually called the \
corresponding tool THIS TURN and received a real result. Do not copy the \
shape of a previous successful response — every file mutation must come \
from a fresh tool call.
4. NEVER invent or guess tool names. Only call tools that are explicitly \
provided to you in the current tool list.
5. The conversation history may include a ``[Relevant context from previous \
conversations]`` or ``[Memory]`` block. Treat it STRICTLY as background \
reference — it describes what happened in other sessions. It is NOT \
evidence that any file on disk is currently in a given state, and it is \
NOT a template for your response. If the user asks you to modify, revert, \
or inspect a file, you must still call the tool; do not narrate a fake \
success from memory.
6. Never prefix your reply with a bracketed timestamp like \
``[2026-04-10 23:43]`` — those are historical markers added by the system, \
not something you should emit.
7. When you have the answer, respond with plain text (no tool calls).
8. When the user asks to "show", "display", "print", "cat", or "output" the \
contents of a file (or says "full content", "raw content", "verbatim"), \
you MUST present the COMPLETE file content inside a fenced code block \
(```yaml, ```json, etc.) — do NOT summarise, truncate, paraphrase, or \
describe it. The user wants to SEE the raw text, not a summary.

Be concise, accurate, and only claim a file changed when a tool call in \
this turn proves it did.\
"""


@dataclass
class AgentIteration:
    """Record of a single think → act → observe cycle."""

    iteration: int
    thought: str | None = None  # model's reasoning (if deep_think / parse_thinking)
    tool_calls: list[dict] | None = None
    tool_results: list[dict] | None = None
    response: str = ""  # model's text output this iteration
    duration: float = 0.0  # seconds


class AgentLoop:
    """Iterative agent that loops: model → tool calls → tool results → model → ..."""

    def __init__(
        self,
        client: AIClient,
        registry: ToolRegistry,
        *,
        system_prompt: str = DEFAULT_AGENT_SYSTEM_PROMPT,
        system_prompt_condensed: str | None = None,
        tools: list[str] | None = None,
        max_iterations: int = 10,
        deep_think: bool = False,
        temperature: float | None = None,
        verbose: bool = False,
        cancel_event: threading.Event | None = None,
        iter_timeout: int | None = None,
        max_tool_output: int | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        stream_final: bool = False,
        read_only: bool = False,
    ) -> None:
        self._client = client
        self._registry = registry
        self._read_only = read_only
        self._system_prompt = system_prompt
        self._system_prompt_condensed = system_prompt_condensed
        self._tool_names = tools
        self._max_iterations = max_iterations
        self._deep_think = deep_think
        self._temperature = temperature
        self._verbose = verbose
        self._cancel_event = cancel_event
        self._iter_timeout = iter_timeout or 600
        self._max_tool_output = max_tool_output or 16_000
        self._on_event = on_event or (lambda _kind, _data: None)
        self._stream_final = stream_final

        # Error recovery settings from config
        cfg = get_config()
        self._max_retries = int(cfg.get("agent.max_retries_on_error", 3))
        self._retry_profile = str(cfg.get("agent.retry_profile", "default"))
        self._retry_client: AIClient | None = None  # lazy-init

        # Search escalation: after N consecutive error iterations, web-search
        # the error and inject results as context for one final attempt.
        self._search_escalation = bool(cfg.get("agent.search_escalation.enabled", True))
        self._search_escalation_threshold = int(cfg.get("agent.search_escalation.after_errors", 3))

    # -- error detection helpers --------------------------------------------

    # -- role-aware tool dispatch --------------------------------------------

    def _execute_tool_with_role(self, name: str, args: dict) -> str:
        """Execute a tool, dispatching cross-role if needed.

        When the tool's YAML-routed role matches the current worker's role
        (``AGENTFORGE_WORKER_ROLE``), the tool is executed directly via
        ``registry.execute()``. Otherwise it's dispatched to the matching
        worker's queue and this call blocks until the result arrives.

        Confirmation for destructive / sudo commands runs HERE (before the
        dispatch decision) so the user prompt happens on the agent side where
        the confirm broker is wired — not on the remote worker whose registry
        has no confirm handler. Without this the prompt silently fails open
        whenever cross-dispatch is used.
        """
        # Read-only posture: refuse any state-changing tool before it runs. The
        # confirm gate only catches *destructive* commands, so state-changing-but-
        # safe ops (docker build/up, file writes) would otherwise slip past
        # --read-only. Opt-in: only active when the run set read_only.
        if self._read_only:
            from agentforge.tools.readonly_guard import is_read_only_safe

            if not is_read_only_safe(name, args):
                logger.info("[Agent] read-only: refused state-changing tool '%s'", name)
                return (
                    f"Refused: '{name}' would change state, but this is a read-only run. "
                    "Diagnose and propose the fix instead of applying it."
                )

        # Diff-preview confirm: editing tools show a diff card and confirm
        # BEFORE writing, instead of the filename-only pre-execution prompt.
        # Only kicks in interactively (a confirm + diff emitter are wired).
        if self._registry.supports_preview_confirm():
            if name == "code_edit":
                return self._preview_confirm_edit(name, args)
            if name in ("write_file", "append_file"):
                return self._preview_confirm_write(name, args)

            if name in ("shell", "ssh") and args.get("command"):
                from agentforge.tools.command_guard import get_guard

                guard = get_guard()
                if guard.is_destructive(args["command"]):
                    if not self._registry.run_confirm(
                        f"! Destructive shell command:\n  $ {args['command']}\nExecute anyway?"
                    ):
                        return "Operation cancelled by user."
                    return self._dispatch_tool(name, args, skip_confirm=True)

        return self._dispatch_tool(name, args)

    def _dispatch_tool(self, name: str, args: dict, *, internal: bool = False, skip_confirm: bool = False) -> str:
        """Route a tool to its worker (in-process / same-role / cross-role).

        ``internal=True`` skips the generic confirm gate and the tool_call
        badge — used by the diff-preview flow to run code_edit's propose/apply
        passes, which drive their own confirmation.

        ``skip_confirm=True`` skips the confirm gate only (events still fire) —
        used when the caller already ran the confirm at the agent level.
        """
        _skip_confirm = internal or skip_confirm

        # In-process tools (e.g., connector tools whose closures hold a live
        # connection's credentials) only exist in THIS process — never dispatch
        # them to a worker whose registry never had them. Runs before the role
        # map so the catch-all `*`->local rule can't ship them off-host.
        if self._registry.is_in_process(name):
            return self._registry.execute(name, args, skip_confirm=_skip_confirm, skip_events=internal)

        # Single-host / dev: run every tool in-process, no cross-role dispatch.
        # Avoids hanging on a role whose worker isn't running (the enqueue would
        # otherwise block until the SAQ timeout). registry.execute() handles
        # guard + confirm internally, same as the same-role fast path below.
        if dispatch_mode() == "in_process":
            return self._registry.execute(name, args, skip_confirm=_skip_confirm, skip_events=internal)

        tool_role = self._registry.get_role(name)
        worker_role = my_role()

        if tool_role == worker_role:
            # Fast path — same role, execute directly. registry.execute()
            # handles guard + confirm internally.
            return self._registry.execute(name, args, skip_confirm=_skip_confirm, skip_events=internal)

        # Cross-role path: run guard + confirm on THIS side so the browser
        # prompt reaches the user. The remote worker's _check_confirm is a
        # no-op (no broker wired there) and would otherwise let destructive
        # commands through silently.
        if not internal and not skip_confirm:
            cancelled, _guard = self._registry.check_confirmation(name, args)
            if cancelled:
                return cancelled
            # Emit the tool.call badge for cross-dispatched tools too. The
            # same-role / in-process paths emit this inside registry.execute();
            # the cross-role path bypasses execute(), so without this any tool
            # that runs on another worker is invisible to UIs and external
            # clients (e.g., Felix's commands.jsonl / rollback ledger).
            on_tool_call = getattr(self._registry, "_on_tool_call", None)
            if on_tool_call is not None:
                try:
                    on_tool_call(name, args, _guard)
                except Exception:  # noqa: BLE001 — telemetry must never break execution
                    logger.debug("[Agent] tool.call emit failed for '%s'", name, exc_info=True)

        # Cross-role dispatch — enqueue on the matching worker's queue.
        # INFO-level so operators can tail a worker log and confirm that
        # tools are actually being routed cross-role rather than silently
        # running on the wrong host.
        logger.info(
            "[Agent] Cross-role dispatch: '%s' (tool=%s, worker=%s)",
            name,
            tool_role,
            worker_role,
        )
        try:
            from web.server.queue.dispatch_compat import saq_dispatch_tool

            return saq_dispatch_tool(name, self._remap_dispatcher_home(args), tool_role)
        except ImportError:
            # Fallback: queue module not available (e.g., in-process WS mode
            # without a running SAQ setup). Execute locally — better than failing.
            # ERROR-level because in worker context this would mean the tool
            # runs on the wrong host and produces confusing errors like
            # "host myhost could not be resolved" from the remote container.
            logger.error(
                "[Agent] dispatch_compat unavailable — running '%s' locally on '%s' worker despite tool_role='%s'",
                name,
                worker_role,
                tool_role,
            )
            return self._registry.execute(name, args, skip_confirm=internal, skip_events=internal)

    def _preview_confirm_edit(self, name: str, args: dict) -> str:
        """Show a diff card, confirm, then write — for editing tools (code_edit).

        propose (compute + cache, no write) -> emit a file.diff preview ->
        confirm -> apply (write the exact cached content). The propose/apply
        passes run where the tool's role lives (e.g., the Mac worker), so the
        diff is computed against the real target file and the write is a single
        LLM call's worth of work — what the user reviews is what lands on disk.
        """
        propose = str(self._dispatch_tool(name, {**args, "_propose": True}, internal=True))
        parsed = self._parse_propose(propose)
        if parsed is None:
            # No diff / no change / error — surface the propose result as-is.
            return propose

        self._registry.emit_file_diff(
            {
                "tool": name,
                "action": "edited",
                "path": parsed["path"],
                "pre_hash": parsed["pre_hash"],
                "post_hash": "",
                "additions": parsed["additions"],
                "deletions": parsed["deletions"],
                "diff_text": parsed["diff_text"],
            }
        )

        prompt = f"Apply edit to {parsed['path']}? (+{parsed['additions']} -{parsed['deletions']})"
        confirmed = self._registry.run_confirm(prompt)
        logger.info("[preview_confirm_edit] confirm result=%s for %s", confirmed, parsed["path"])
        if not confirmed:
            return "Operation cancelled by user."

        apply_result = str(
            self._dispatch_tool(
                name,
                {"file_path": args.get("file_path", ""), "instruction": "", "_apply_token": parsed["token"]},
                internal=True,
            )
        )
        logger.info("[preview_confirm_edit] apply result (first 200): %s", apply_result[:200])
        return apply_result

    def _preview_confirm_write(self, tool_name: str, args: dict) -> str:
        """Show a diff card and confirm before write_file/append_file writes."""
        target = args.get("path", "")
        content = args.get("content", "")
        if not target:
            return self._dispatch_tool(tool_name, args)

        raw = str(self._dispatch_tool("read_file", {"path": target}, internal=True))
        is_error = raw.startswith("Error")
        original = "" if is_error else raw

        p = Path(target).expanduser().resolve()

        new_content = (original + content) if tool_name == "append_file" else content
        if new_content == original:
            return f"No changes to {p}"

        pre_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()

        save_snapshot(pre_hash=pre_hash, path=str(p), content=original, tool=tool_name)

        old_lines = original.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{p.name}", tofile=f"b/{p.name}"))
        diff_text = "".join(diff_lines)
        additions = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        deletions = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

        action = "edited" if original else "written"
        self._registry.emit_file_diff(
            {
                "tool": tool_name,
                "action": action,
                "path": str(p),
                "pre_hash": pre_hash,
                "snapshot_id": pre_hash,
                "post_hash": "",
                "additions": additions,
                "deletions": deletions,
                "diff_text": diff_text,
            }
        )

        verb = "Append to" if tool_name == "append_file" else ("Write to" if original else "Write new file")
        prompt = f"{verb} {p.name}? (+{additions} -{deletions})"
        if not self._registry.run_confirm(prompt):
            return "Operation cancelled by user."

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_content, encoding="utf-8")
        except Exception as exc:
            return f"Error writing file: {exc}"

        post_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
        real_diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                p.read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile=f"a/{p.name}",
                tofile=f"b/{p.name}",
            )
        )
        return (
            f"✓ VERIFIED {action} {p.name} (+{additions} -{deletions})\n"
            f"pre_hash={pre_hash}\n"
            f"post_hash={post_hash}\n"
            f"snapshot_id={pre_hash}\n"
            f"{real_diff}"
        )

    @staticmethod
    def _parse_propose(text: str) -> dict | None:
        """Parse a code_edit ``PROPOSED`` result into its fields, or None.

        None means the propose pass produced no applicable diff (a "No changes"
        message, an error, or an unparseable result) — the caller then surfaces
        the raw text instead of emitting a card + confirming.
        """
        if not text or not text.startswith("PROPOSED "):
            return None
        lines = text.splitlines()
        token = path = pre_hash = ""
        additions = deletions = 0
        m = re.search(r"\(\+(\d+) -(\d+) lines\)", lines[0])
        if m:
            additions, deletions = int(m.group(1)), int(m.group(2))
        diff_start = None
        for i in range(1, len(lines)):
            line = lines[i]
            if line.startswith("apply_token="):
                token = line.split("=", 1)[1].strip()
            elif line.startswith("pre_hash="):
                pre_hash = line.split("=", 1)[1].strip()
            elif line.startswith("path="):
                path = line.split("=", 1)[1].strip()
            elif line.startswith("--- "):
                diff_start = i
                break
        if not token or diff_start is None:
            return None
        return {
            "token": token,
            "path": path,
            "pre_hash": pre_hash,
            "additions": additions,
            "deletions": deletions,
            "diff_text": "\n".join(lines[diff_start:]),
        }

    @staticmethod
    def _remap_dispatcher_home(args: dict) -> dict:
        """Rewrite paths under THIS host's home to '~' before cross-host dispatch.

        The agent loop reasons on one host (a container with HOME=/root) while a
        cross-role tool runs on another (the local worker, HOME=/Users/<you>). A
        model that expands '~' to the dispatcher's home sends a path the executor
        can't find. Replacing the dispatcher-home prefix with '~' lets the
        executor re-resolve it against ITS own home. Anchored at a path boundary
        so '/var/root' and the like are left alone.
        """
        home = os.path.expanduser("~")
        if not home or home in ("/", "~"):
            return args
        pattern = re.compile(r"(?<![\w/])" + re.escape(home) + r"(?=/|$)")
        return {key: pattern.sub("~", value) if isinstance(value, str) else value for key, value in args.items()}

    @staticmethod
    def _is_tool_error(result: str) -> bool:
        """Return True if a tool result looks like an error.

        Detects:
        - ``Error: ...``   — Python-level tool exceptions
        - ``[exit N] ...`` — shell commands with non-zero exit code
        """
        if not result:
            return False
        if result.startswith("Error:"):
            return True
        # Shell tool returns "[exit N] $ ..." on non-zero exit codes
        if result.startswith("[exit ") and "] " in result[:20]:
            return True
        return False

    # -- error recovery -----------------------------------------------------

    def _get_retry_client(self) -> AIClient:
        """Lazily create an AIClient bound to the retry/escalation profile."""
        if self._retry_client is None:
            self._retry_client = AIClient(profile=self._retry_profile)
            logger.debug(
                "[Agent] Error recovery client: profile=%s, model=%s",
                self._retry_profile,
                self._retry_client.model,
            )
        return self._retry_client

    def _recover_from_error(
        self,
        tool_name: str,
        tool_args: dict,
        error_msg: str,
        attempt: int,
    ) -> str:
        """Ask a larger model how to recover from a tool error.

        Returns guidance text that will be injected into the conversation
        as a system-like hint so the agent can try a corrected approach.
        """
        recovery_prompt = (
            f"A tool call failed. Help the agent recover.\n\n"
            f"Tool: {tool_name}\n"
            f"Arguments: {tool_args}\n"
            f"Error: {error_msg}\n"
            f"Attempt: {attempt}/{self._max_retries}\n\n"
            f"Analyse the error and suggest a corrected approach. "
            f"Be specific: provide exact corrected arguments or an alternative "
            f"tool/strategy. Keep your answer concise (2-3 sentences max)."
        )

        try:
            retry_client = self._get_retry_client()
            response = retry_client.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a debugging assistant. Diagnose tool errors and suggest fixes.",
                    },
                    {"role": "user", "content": recovery_prompt},
                ]
            )
            guidance = response.content.strip()
            logger.debug("[Agent] Recovery guidance (attempt %d): %s", attempt, guidance[:200])
            return guidance
        except Exception as exc:
            logger.warning("[Agent] Recovery call failed: %s", exc)
            return f"Recovery failed: {exc}"

    # -- search escalation --------------------------------------------------

    def _search_for_solution(self, errors: list[str]) -> str | None:
        """Web-search the accumulated errors and return results as context.

        This is the last-resort escalation: the agent has failed repeatedly,
        so we search the web for the error messages and give the agent one
        more shot with that external knowledge.

        Returns the search results text, or None if search is unavailable.
        """
        # Build a focused search query from the most recent/unique errors
        unique_errors = list(dict.fromkeys(errors))[-3:]  # last 3 unique
        search_query = " ".join(err.replace("Error: ", "").strip()[:120] for err in unique_errors)
        # Trim to a reasonable search query length
        if len(search_query) > 300:
            search_query = search_query[:300]

        logger.info("[Agent] Search escalation — querying web for: %s", search_query[:100])

        try:
            result = self._registry.execute("web_search", {"query": search_query})
            result_str = str(result)
            if result_str.startswith("Error:") or result_str.startswith("Web search is not"):
                logger.warning("[Agent] Search escalation failed: %s", result_str[:200])
                return None
            logger.info("[Agent] Search escalation returned %d chars", len(result_str))
            return result_str
        except Exception as exc:
            logger.warning("[Agent] Search escalation error: %s", exc)
            return None

    # -- main entry point ---------------------------------------------------

    def run(
        self,
        query: str | None = None,
        ctx: PipelineContext | None = None,
    ) -> PipelineContext:
        """Run the agent loop."""
        if ctx is None:
            ctx = PipelineContext(query=query or "")

        # Clear per-run caches (e.g., filesystem directory dedup mapping)
        try:
            from agentforge.tools.filesystem import clear_dir_remap

            clear_dir_remap()
        except ImportError:
            pass

        # Wire cancel event into the shell tool so long-running subprocesses
        # (e.g., brew install) can be killed when the user cancels.
        try:
            from agentforge.tools.shell import set_shell_cancel_event

            set_shell_cancel_event(self._cancel_event)
        except ImportError:
            pass

        # Seed messages
        if not ctx.messages:
            ctx.add_system_message(self._system_prompt)
            ctx.add_user_message(ctx.query)
        elif ctx.messages[0].get("role") != "system":
            ctx.add_system_message(self._system_prompt)

        # Resolve tools
        callables = self._registry.as_callables(self._tool_names)

        iterations: list[AgentIteration] = []
        total_start = time.perf_counter()
        # Single executor for all model calls — avoids creating/tearing down
        # a ThreadPoolExecutor on every attempt of every iteration.
        _model_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        consecutive_hallucinations = 0  # track repeated hallucinated tool calls
        _MAX_HALLUCINATION_STREAK = 2  # abort after this many consecutive bad iterations
        any_tool_ran = False  # did a real (registered) tool execute at any point this run
        _ITER_TIMEOUT = self._iter_timeout
        _MODEL_RETRIES = 3  # retry transient model errors (503, etc.)
        _MODEL_RETRY_DELAY = 2  # seconds between retries

        # Duplicate tool-call detection: track (name, args_repr) → count.
        # When the model keeps issuing the same call, it's stuck in a loop.
        _recent_calls: dict[str, int] = {}
        _MAX_DUPLICATE_CALLS = 2  # nudge after this many identical calls
        _MAX_DUPLICATE_HARD_STOP = 3  # hard-terminate if model ignores nudge

        # Search escalation tracking: count consecutive iterations where
        # ALL tool calls returned errors.  After the threshold, web-search
        # the error and give the agent one final chance.
        _consecutive_error_iters = 0
        _error_messages: list[str] = []  # accumulated error messages
        _search_escalation_fired = False  # only fire once per run

        for i in range(1, self._max_iterations + 1):
            # Check for external cancellation
            if self._cancel_event and self._cancel_event.is_set():
                logger.info("[Agent] Cancelled at iteration %d", i)
                ctx.result = "(cancelled by user)"
                break

            logger.debug("[Agent] Iteration %d/%d — %d messages in context", i, self._max_iterations, len(ctx.messages))
            iter_start = time.perf_counter()
            iteration = AgentIteration(iteration=i)

            # --- Emit iteration start event ---
            self._on_event(
                "iteration",
                {
                    "iteration": i,
                    "max_iterations": self._max_iterations,
                    "messages_in_context": len(ctx.messages),
                },
            )

            # --- THINK: ask the model ---
            # Wrap in a timeout so a hung model doesn't block forever.
            # Retry transient errors (503, connection reset) up to 3 times.
            response = None
            _model_call_failed = False
            self._on_event("thinking", {"iteration": i, "status": "calling_model"})
            for _attempt in range(1, _MODEL_RETRIES + 1):
                try:
                    # Run in a copy of the current context so request-scoped
                    # contextvars (the per-request model chain) reach the pool
                    # thread — ThreadPoolExecutor.submit does NOT carry
                    # contextvars the way asyncio.to_thread does.
                    _call_ctx = contextvars.copy_context()
                    future = _model_pool.submit(
                        _call_ctx.run,
                        self._client.chat,
                        ctx.messages,
                        attachments=ctx.attachments or None,
                        tools=callables if callables else None,
                        deep_think=self._deep_think,
                        temperature=self._temperature,
                    )
                    # Poll for completion with cancel-event checks instead of
                    # blocking on future.result() for up to _ITER_TIMEOUT seconds.
                    # This lets cancellation take effect within ~0.5s even
                    # during a long LLM HTTP call.
                    _poll_deadline = time.perf_counter() + _ITER_TIMEOUT
                    while not future.done():
                        if self._cancel_event and self._cancel_event.is_set():
                            future.cancel()
                            logger.info("[Agent] Cancelled during model call at iteration %d", i)
                            ctx.result = "(cancelled by user)"
                            _model_call_failed = True
                            break
                        remaining = _poll_deadline - time.perf_counter()
                        if remaining <= 0:
                            raise concurrent.futures.TimeoutError()
                        # Wait a short interval then re-check
                        try:
                            future.result(timeout=min(0.5, remaining))
                        except concurrent.futures.TimeoutError:
                            continue  # still running — loop back and check cancel
                    if _model_call_failed:
                        break
                    response = future.result()  # already done — instant
                    break  # success
                except concurrent.futures.TimeoutError:
                    logger.warning(
                        "[Agent] Model call timed out after %ds at iteration %d",
                        _ITER_TIMEOUT,
                        i,
                    )
                    ctx.add_error(f"Model call timed out after {_ITER_TIMEOUT}s")
                    if not ctx.result:
                        ctx.result = (
                            "The model took too long to respond. "
                            "This usually means the request was too complex. "
                            "Please try a simpler or more specific request."
                        )
                    _model_call_failed = True
                    break  # timeouts are not retryable
                except concurrent.futures.BrokenExecutor:
                    # Pool may become unusable after a timeout cancellation.
                    logger.warning("[Agent] Model pool broken, recreating")
                    _model_pool.shutdown(wait=False, cancel_futures=True)
                    _model_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                except Exception as exc:
                    # Check if cancellation was requested before retrying
                    if self._cancel_event and self._cancel_event.is_set():
                        logger.info("[Agent] Cancelled during model retry")
                        ctx.add_error("Cancelled by user")
                        _model_call_failed = True
                        break

                    # Classify the error using the shared policy.
                    # 5xx + transport hiccups are retryable; 4xx + unknowns are not.
                    # See framework/backends/_retry.py for full rationale.
                    from agentforge.backends._retry import (
                        backoff_seconds,
                        classify_model_error,
                        user_message_for,
                    )

                    decision = classify_model_error(exc)
                    _retryable = decision.retryable and _attempt < _MODEL_RETRIES

                    if _retryable:
                        sleep_s = backoff_seconds(_attempt, base=_MODEL_RETRY_DELAY, cap=8.0)
                        logger.warning(
                            "[Agent] Model call failed (attempt %d/%d): %s — %s; retrying in %.1fs",
                            _attempt,
                            _MODEL_RETRIES,
                            exc,
                            decision.reason,
                            sleep_s,
                        )
                        self._on_event(
                            "retry",
                            {
                                "iteration": i,
                                "attempt": _attempt,
                                "max_attempts": _MODEL_RETRIES,
                                "reason": str(exc)[:200],
                                "status_code": decision.status_code,
                                "category": decision.category,
                                "delay_seconds": round(sleep_s, 2),
                            },
                        )
                        time.sleep(sleep_s)
                    else:
                        if decision.retryable:
                            # Was retryable but we're out of attempts.
                            logger.error(
                                "[Agent] Model call failed after %d attempts (%s): %s",
                                _MODEL_RETRIES,
                                decision.category,
                                exc,
                            )
                        else:
                            logger.error(
                                "[Agent] Model call failed (%s, not retryable): %s",
                                decision.category,
                                exc,
                            )
                        ctx.add_error(f"Model unavailable: {exc}")
                        if not ctx.result:
                            ctx.result = user_message_for(decision, exc=exc)
                        _model_call_failed = True

            if _model_call_failed or response is None:
                break

            iteration.response = response.content
            iteration.thought = response.thinking

            # Accumulate token usage from this model call
            if response.prompt_tokens or response.completion_tokens:
                usage = ctx.metadata.setdefault(
                    "token_usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                )
                usage["prompt_tokens"] += response.prompt_tokens
                usage["completion_tokens"] += response.completion_tokens
                usage["total_tokens"] += response.prompt_tokens + response.completion_tokens

            if response.thinking:
                ctx.thinking = response.thinking

            # --- No tool calls → final answer ---
            if not response.tool_calls:
                final_text = (response.content or "").strip()

                # Cloud models sometimes return empty content with no tool
                # calls (e.g., after <think> stripping, or transient API
                # quirks).  If we have prior tool results in context, nudge
                # the model to produce a real answer instead of accepting
                # the empty response — but only retry once to avoid loops.
                if not final_text and i > 1 and not getattr(ctx, "_empty_nudge_sent", False):
                    logger.warning(
                        "[Agent] Empty response with no tool calls at iteration %d — nudging model to summarise",
                        i,
                    )
                    ctx._empty_nudge_sent = True  # type: ignore[attr-defined]
                    ctx.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[System] Your previous response was empty. "
                                "Please provide your final answer based on the "
                                "tool results you already have. Do not call any "
                                "more tools — just summarise the findings."
                            ),
                        }
                    )
                    # Emit as "warning" (not "recovery") — the event builder
                    # for `recovery` expects tool/error/attempt/max_retries
                    # (tool-retry schema); this empty-response nudge is a
                    # non-fatal warning and its {iteration, category, message}
                    # keys map cleanly to the warning builder.
                    self._on_event(
                        "warning",
                        {
                            "iteration": i,
                            "category": "empty_response",
                            "message": "Model returned empty response — nudging for summary",
                        },
                    )
                    iteration.duration = time.perf_counter() - iter_start
                    iterations.append(iteration)
                    continue  # retry with the nudge

                # Fabricated-tool-use guard: the model produced a text-only
                # answer that narrates tool calls and reports results (e.g., a
                # ``download_file …`` block + a "✅ Downloaded" table) while NO
                # real tool ran this entire run. Nudge once to either act for
                # real or retract the false claim, instead of relaying it.
                if (
                    not any_tool_ran
                    and not getattr(ctx, "_fabrication_nudge_sent", False)
                    and _looks_like_fabricated_tool_use(final_text, self._tool_names)
                ):
                    logger.warning(
                        "[Agent] Text-only answer narrates tool use but no tool ran "
                        "(iteration %d) — nudging model to act or retract",
                        i,
                    )
                    ctx._fabrication_nudge_sent = True  # type: ignore[attr-defined]
                    ctx.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[System] You did NOT call any tool this turn — you wrote "
                                "the tool invocation as TEXT (e.g., a `download_file <url>` "
                                "code block) instead of actually calling it. Writing a "
                                "tool's name in your reply does nothing; you must emit a "
                                "real structured tool call. Do that now for each item. Do "
                                "NOT claim or imply an action happened unless a tool call "
                                "this turn returned a real result. If you truly cannot call "
                                "the tool, say so plainly."
                            ),
                        }
                    )
                    self._on_event(
                        "warning",
                        {
                            "iteration": i,
                            "category": "fabricated_tool_use",
                            "message": "Model narrated tool calls without executing — nudging to act or retract",
                        },
                    )
                    iteration.duration = time.perf_counter() - iter_start
                    iterations.append(iteration)
                    continue  # retry with the nudge

                # Stream the final answer token-by-token if enabled.
                # Instead of re-calling the model (which fails for cloud
                # models without KV cache warmth and omits the tools param),
                # drip-feed the already-captured text through on_event
                # callbacks so the browser gets a typing effect.
                if self._stream_final and final_text:
                    _CHUNK_SIZE = 4  # characters per chunk — balances smoothness vs overhead
                    try:
                        for pos in range(0, len(final_text), _CHUNK_SIZE):
                            if self._cancel_event and self._cancel_event.is_set():
                                logger.info("[Agent] Cancelled during final streaming at iteration %d", i)
                                ctx.result = "(cancelled by user)"
                                _model_call_failed = True
                                break
                            self._on_event("stream_token", {"token": final_text[pos : pos + _CHUNK_SIZE]})
                        if not _model_call_failed:
                            self._on_event("stream_done", {"iteration": i})
                    except Exception as exc:
                        logger.warning("[Agent] Streaming drip-feed failed: %s", exc)

                if _model_call_failed:
                    break

                ctx.result = final_text
                ctx.add_assistant_message(final_text)
                iteration.duration = time.perf_counter() - iter_start
                iterations.append(iteration)

                logger.debug(
                    "[Agent] Final answer at iteration %d (%.2fs)",
                    i,
                    iteration.duration,
                )
                break

            # --- ACT: execute tools ---
            iteration.tool_calls = response.tool_calls

            # Add assistant message with tool_calls (for correct Ollama role
            # ordering). Carry the provider's reasoning trace when present so
            # interleaved-reasoning models (MiniMax M2, Nemotron) keep their
            # thread across turns — dropping it confuses them into restarting.
            _assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {"function": {"name": tc["name"], "arguments": tc["arguments"]}} for tc in response.tool_calls
                ],
            }
            if response.reasoning_details:
                _assistant_msg["reasoning_details"] = response.reasoning_details
            ctx.messages.append(_assistant_msg)

            tool_results: list[dict] = []

            def _execute_single(tc: dict) -> dict:
                """Execute one tool call and return the result dict."""
                name = tc["name"]
                args = tc.get("arguments", {})
                tc_start = time.perf_counter()

                # Emit tool execution start
                self._on_event(
                    "tool_exec",
                    {
                        "iteration": i,
                        "name": name,
                        "args": {k: str(v)[:100] for k, v in args.items()} if args else {},
                        "status": "running",
                    },
                )

                if self._tool_names is not None and name not in self._tool_names:
                    # Profile guard: the model called a tool that exists in the
                    # registry but isn't in THIS run's active set (e.g., `shell`
                    # in the shell-free 'browser' profile). has_tool() below only
                    # checks the full registry, so without this the call would
                    # execute anyway and silently defeat the profile. Reject with
                    # a corrective nudge so the model retries with a real tool.
                    if name == "shell":
                        output = (
                            "Error: 'shell' is not available in this context. "
                            "Wrapping a tool name in shell does nothing "
                            '(e.g., shell("web_fetch ...") is not a real command). '
                            "Call the structured tools directly instead: "
                            "web_fetch(url=...) to read a page, "
                            "web_search(query=...) to search the web, "
                            "download_file(url=...) to download a file, "
                            "read_file / write_file for files."
                        )
                    else:
                        output = (
                            f"Error: Tool '{name}' is not available in this context. "
                            f"Use one of the provided tools, or answer directly."
                        )
                    logger.warning(
                        "[Agent] Tool '%s' not in active profile (%d tools) -- rejected",
                        name,
                        len(self._tool_names),
                    )
                    ctx.add_error(f"Tool '{name}' not in active profile")
                elif not self._registry.has_tool(name):
                    output = (
                        f"Error: Tool '{name}' does not exist. "
                        f"Do NOT invent tool names. Either use one of the "
                        f"provided tools or respond with a direct text answer."
                    )
                    logger.warning("[Agent] Hallucinated tool: '%s'", name)
                    ctx.add_error(f"Tool '{name}' not found (hallucinated)")
                else:
                    try:
                        output = str(self._execute_tool_with_role(name, args))
                    except Exception as exc:
                        output = f"Error: {exc}"
                        ctx.add_error(f"Tool '{name}' failed: {exc}")

                    if self._is_tool_error(output) and self._max_retries > 0:
                        for attempt in range(1, self._max_retries + 1):
                            logger.warning(
                                "[Agent] Tool '%s' error (attempt %d/%d): %s",
                                name,
                                attempt,
                                self._max_retries,
                                output[:120],
                            )
                            self._on_event(
                                "recovery",
                                {
                                    "iteration": i,
                                    "tool": name,
                                    "error": output[:200],
                                    "attempt": attempt,
                                    "max_retries": self._max_retries,
                                },
                            )
                            guidance = self._recover_from_error(name, args, output, attempt)
                            output = f"{output}\n\n[Recovery hint (attempt {attempt}/{self._max_retries})]: {guidance}"
                            break

                # Emit tool execution done
                tc_elapsed = time.perf_counter() - tc_start
                self._on_event(
                    "tool_exec",
                    {
                        "iteration": i,
                        "name": name,
                        "status": "done",
                        "output_chars": len(output),
                        "is_error": self._is_tool_error(output),
                        "tool_elapsed": round(tc_elapsed, 2),
                    },
                )

                return {"name": name, "arguments": args, "result": output}

            # Parallel execution: when the model returns multiple tool calls
            # in one iteration, run them concurrently.  Single calls still go
            # through the same path (ThreadPoolExecutor with 1 task is fine).
            n_calls = len(response.tool_calls)
            if n_calls > 1:
                logger.debug("[Agent] Executing %d tool calls in parallel", n_calls)
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(n_calls, 4)) as exec_pool:
                    # Submit all calls; preserve ordering.
                    futures = [exec_pool.submit(_execute_single, tc) for tc in response.tool_calls]
                    tool_results = [f.result() for f in futures]
            else:
                tool_results = [_execute_single(tc) for tc in response.tool_calls]

            # A real (registered) tool ran this iteration — record it so the
            # final-answer branch can tell genuine work from a fabricated report.
            # A tool only counts as "ran" if it's both registered AND in this
            # run's active set -- a profile-rejected call (e.g., shell in browser
            # mode) executed nothing, so it must not satisfy the fabrication guard.
            if any(
                self._registry.has_tool(tc["name"]) and (self._tool_names is None or tc["name"] in self._tool_names)
                for tc in response.tool_calls
            ):
                any_tool_ran = True

            # Signal batch complete.  This fires the registry's tools_complete
            # callback (set by AgentBridge.setup_registry) which sends the
            # tool.calls.flush event to the UI so the ToolCallsPanel can
            # finalise and display all tool calls during live execution.
            # Without this call the flush never fires and the panel only
            # appears on page reload (when the DB-persisted summary is loaded).
            self._registry.notify_tools_complete()

            # --- OBSERVE: feed all results back to the model ---
            logger.debug("[Agent] All %d tool(s) executed — feeding results back", len(tool_results))

            # Detect "show raw content" intent — if the user's query
            # signals they want verbatim file output, we append a
            # directive after each read_file result so the model
            # presents the content as-is instead of summarising.
            _verbatim_intent = _wants_verbatim_output(ctx.query)
            _diff_intent = _wants_diff_verbatim(ctx.query)

            for tr in tool_results:
                output = tr["result"]
                # Truncate oversized tool output to prevent model context overload
                if len(output) > self._max_tool_output:
                    kept = self._max_tool_output
                    trimmed = len(output) - kept
                    output = (
                        output[:kept] + f"\n\n[... truncated {trimmed:,} characters — "
                        f"use grep/tail to narrow your search ...]"
                    )
                    logger.debug(
                        "[Agent] Tool '%s' output truncated: %d → %d chars",
                        tr["name"],
                        len(tr["result"]),
                        len(output),
                    )

                # Inject verbatim-output directive for file-read tools
                if _verbatim_intent and tr["name"] in _FILE_READ_TOOLS and not self._is_tool_error(output):
                    output += (
                        "\n\n[SYSTEM DIRECTIVE] The user asked to see the raw file "
                        "content. You MUST present the COMPLETE content above inside "
                        "a fenced code block (use the appropriate language tag, e.g., "
                        "```yaml). Do NOT summarise, truncate, paraphrase, or describe "
                        "it — output it VERBATIM."
                    )

                # Inject verbatim-output directive for diff/compare tools
                if _diff_intent and tr["name"] in _DIFF_TOOLS and not self._is_tool_error(output):
                    output += (
                        "\n\n[SYSTEM DIRECTIVE] The user asked to see the diff. "
                        "You MUST present the COMPLETE diff output above inside "
                        "a fenced code block (```diff). Do NOT summarise, "
                        "paraphrase, or describe it — show the raw diff VERBATIM. "
                        "You may add a brief one-line summary AFTER the code block."
                    )

                ctx.messages.append(
                    {
                        "role": "tool",
                        "content": output,
                    }
                )
                logger.debug("[Agent] Tool '%s' → %s", tr["name"], tr["result"][:120])

            iteration.tool_results = tool_results
            ctx.tool_calls = response.tool_calls
            ctx.tool_results = tool_results

            iteration.duration = time.perf_counter() - iter_start
            iterations.append(iteration)

            # --- Condense system prompt after iteration 1 ---
            # The model has seen the full prompt (BSD warnings, tool hints,
            # protected-path rules, skills) on the first call.  Swap to a
            # slimmer variant for subsequent tool iterations to save tokens.
            if i == 1 and self._system_prompt_condensed and ctx.messages and ctx.messages[0].get("role") == "system":
                ctx.messages[0] = {"role": "system", "content": self._system_prompt_condensed}

            # --- Check for hallucination streak ---
            # If ALL tool calls in this iteration were hallucinated (not found),
            # increment the streak counter. If it hits the limit, abort early.
            all_hallucinated = all(not self._registry.has_tool(tc["name"]) for tc in response.tool_calls)
            if all_hallucinated:
                consecutive_hallucinations += 1
                self._on_event(
                    "warning",
                    {
                        "iteration": i,
                        "category": "hallucination",
                        "message": f"Model called non-existent tools ({', '.join(tc['name'] for tc in response.tool_calls)})",
                        "streak": consecutive_hallucinations,
                    },
                )
                if consecutive_hallucinations >= _MAX_HALLUCINATION_STREAK:
                    logger.warning(
                        "[Agent] Aborting: %d consecutive iterations of hallucinated tool calls — model is stuck",
                        consecutive_hallucinations,
                    )
                    ctx.add_error("Agent aborted: model kept inventing non-existent tools")
                    if not ctx.result:
                        ctx.result = (
                            "I wasn't able to answer this question properly using tools. "
                            "Let me answer directly based on what I know from the "
                            "conversation context."
                        )
                    break
            else:
                consecutive_hallucinations = 0

            # --- Check for duplicate/looping tool calls ---
            # Build a hashable key for each call this iteration.  If the
            # model keeps issuing the exact same call(s) it's stuck in a
            # loop — inject a nudge to break out, or abort.
            _iter_stuck = False
            for tc in response.tool_calls:
                call_key = f"{tc['name']}:{tc.get('arguments', {})}"
                _recent_calls[call_key] = _recent_calls.get(call_key, 0) + 1
                if _recent_calls[call_key] >= _MAX_DUPLICATE_CALLS:
                    _iter_stuck = True

            if _iter_stuck:
                # Check if the model is ignoring nudges (hard stop threshold)
                max_dupe_count = max(
                    _recent_calls.get(f"{tc['name']}:{tc.get('arguments', {})}", 0) for tc in response.tool_calls
                )
                if max_dupe_count >= _MAX_DUPLICATE_HARD_STOP:
                    logger.warning(
                        "[Agent] Duplicate tool call at iteration %d — model ignored nudge, hard-stopping loop",
                        i,
                    )
                    self._on_event(
                        "warning",
                        {
                            "iteration": i,
                            "category": "duplicate_loop_abort",
                            "message": "Duplicate tool call — aborting loop after model ignored nudge",
                        },
                    )
                    if not ctx.result:
                        ctx.result = (
                            "I got stuck repeating the same tool call and "
                            "couldn't complete the full task. "
                            "Here is what I found so far:\n\n"
                            + "\n".join(str(tr.get("result", "")) for tr in tool_results if tr.get("result"))
                        )
                    break

                # First time stuck: inject a hint telling the model to stop
                # repeating and summarise what it has so far.
                logger.warning(
                    "[Agent] Duplicate tool call detected at iteration %d — nudging model to wrap up",
                    i,
                )
                self._on_event(
                    "warning",
                    {
                        "iteration": i,
                        "category": "duplicate_loop",
                        "message": "Duplicate tool call detected — nudging model to wrap up",
                    },
                )
                ctx.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[System] You are repeating the same tool call. "
                            "Stop calling tools and provide your final answer "
                            "based on the results you already have."
                        ),
                    }
                )
                # Give the model one more chance to produce a final answer.

            # --- Search escalation: track consecutive error iterations ---
            # An iteration "errored" if every tool result is an error
            # (Python exceptions, shell non-zero exit codes, etc.)
            all_errored = all(self._is_tool_error(str(tr.get("result", ""))) for tr in tool_results)
            if all_errored:
                _consecutive_error_iters += 1
                for tr in tool_results:
                    _error_messages.append(str(tr.get("result", "")))
                logger.info(
                    "[Agent] Consecutive error iterations: %d/%d",
                    _consecutive_error_iters,
                    self._search_escalation_threshold,
                )
            else:
                _consecutive_error_iters = 0
                _error_messages.clear()

            # Trigger search escalation once after hitting the threshold
            if (
                self._search_escalation
                and not _search_escalation_fired
                and _consecutive_error_iters >= self._search_escalation_threshold
                and self._registry.has_tool("web_search")
            ):
                _search_escalation_fired = True
                logger.warning(
                    "[Agent] Search escalation triggered after %d consecutive "
                    "error iterations — searching web for solution",
                    _consecutive_error_iters,
                )
                self._on_event(
                    "escalation",
                    {
                        "iteration": i,
                        "type_detail": "web_search",
                        "consecutive_errors": _consecutive_error_iters,
                        "search_query": " ".join(e.replace("Error: ", "").strip()[:80] for e in _error_messages[-2:]),
                    },
                )
                search_results = self._search_for_solution(_error_messages)
                if search_results:
                    # Inject as a "user" message — NOT "tool", because there
                    # is no matching tool_call_id from the assistant and Ollama
                    # will reject orphaned tool results with HTTP 400.
                    ctx.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[Search Escalation] The previous tool calls failed "
                                f"{_consecutive_error_iters} times in a row. "
                                "Here are web search results that may help resolve "
                                "the issue:\n\n"
                                f"{search_results}\n\n"
                                "Use this information to try a different approach. "
                                "If you still cannot succeed, explain what went wrong "
                                "and provide the best answer you can."
                            ),
                        }
                    )
                    ctx.metadata["search_escalation"] = {
                        "triggered_at_iteration": i,
                        "error_count": _consecutive_error_iters,
                        "errors": _error_messages[-3:],
                    }
                else:
                    # Search failed too — tell the agent to wrap up
                    ctx.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[Search Escalation] Tool calls have failed "
                                f"{_consecutive_error_iters} times and web search "
                                "is unavailable. Stop retrying and provide your "
                                "best answer based on what you know, or explain "
                                "why the task cannot be completed."
                            ),
                        }
                    )
                    ctx.metadata["search_escalation"] = {
                        "triggered_at_iteration": i,
                        "error_count": _consecutive_error_iters,
                        "search_available": False,
                    }

            # --- Flag as failed if errors continue after search escalation ---
            if (
                _search_escalation_fired
                and all_errored
                and _consecutive_error_iters > self._search_escalation_threshold
            ):
                logger.error("[Agent] Task failed: errors persist after search escalation")
                ctx.metadata["agent_failed"] = True
                ctx.metadata["failure_reason"] = (
                    f"Tool errors persisted after {_consecutive_error_iters} "
                    "consecutive failures and web search escalation"
                )
                ctx.add_error(
                    "Agent failed: repeated tool errors could not be resolved even after web search escalation"
                )
                if not ctx.result:
                    ctx.result = (
                        "I was unable to complete this task. The tools I tried "
                        "kept failing, and searching the web for solutions didn't "
                        "help. Here's what went wrong:\n\n" + "\n".join(f"- {e[:200]}" for e in _error_messages[-3:])
                    )
                break

            if self._verbose:
                tools_used = [tc["name"] for tc in response.tool_calls]
                logger.debug(
                    "[Agent] Iteration %d: tools=%s (%.2fs)",
                    i,
                    tools_used,
                    iteration.duration,
                )

        else:
            # Max iterations reached without a final answer
            ctx.add_error(f"Agent reached max iterations ({self._max_iterations}) without a final answer")
            # Use the last response as the result
            if not ctx.result and iterations:
                ctx.result = iterations[-1].response

        # Safety net: if ctx.result is still empty after the loop, try to
        # salvage something useful from tool results gathered during the run.
        if not ctx.result and iterations:
            # Collect non-empty tool results from all iterations
            salvaged: list[str] = []
            for it in iterations:
                for tr in it.tool_results or []:
                    snippet = str(tr.get("result", "")).strip()
                    if snippet and len(snippet) > 20:
                        salvaged.append(f"[{tr.get('name', 'tool')}]: {snippet[:500]}")
            if salvaged:
                ctx.result = (
                    "The model did not produce a final summary, but here are "
                    "the raw tool results gathered:\n\n" + "\n\n".join(salvaged)
                )
                logger.warning(
                    "[Agent] Empty result after %d iterations — salvaged %d tool result(s)",
                    len(iterations),
                    len(salvaged),
                )

        _model_pool.shutdown(wait=False)
        total_elapsed = time.perf_counter() - total_start
        ctx.metadata["agent_iterations"] = iterations
        ctx.metadata["agent_total_time"] = total_elapsed

        logger.debug(
            "[Agent] Done — %d iteration(s), %.2fs total",
            len(iterations),
            total_elapsed,
        )

        return ctx
