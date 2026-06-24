"""ParallelAgentRunner — decompose a query into independent task groups and execute them concurrently.

Given a user query like "reinstall node deps in project-a and Python deps in project-b", the runner:

1.  Asks a fast model to produce a **plan** — a JSON list of independent task groups, each with a description and ordered list of shell commands.
2.  Spawns one :class:`AgentLoop` per group and runs them in parallel using :class:`concurrent.futures.ThreadPoolExecutor`.
3.  Aggregates the results and returns a unified :class:`PipelineContext`.

The planner output format::

    {
      "parallel": true,
      "groups": [
        {
          "label": "React project — reinstall node_modules",
          "commands": [
            {"command": "rm -rf node_modules", "cwd": "/Users/me/app-react"},
            {"command": "npm install",          "cwd": "/Users/me/app-react"},
            {"command": "stat node_modules",    "cwd": "/Users/me/app-react"}
          ]
        },
        {
          "label": "Python project — reinstall .venv",
          "commands": [
            {"command": "rm -rf .venv",    "cwd": "/Users/me/app-python"},
            {"command": "poetry install",  "cwd": "/Users/me/app-python"},
            {"command": "stat .venv",      "cwd": "/Users/me/app-python"}
          ]
        }
      ]
    }

When the planner decides the task is NOT parallelisable (single location, dependent steps, etc.), it returns ``{"parallel": false}`` and the caller should fall back to the normal sequential :class:`AgentLoop`.

Usage::

    from agentforge.parallel import ParallelAgentRunner

    runner = ParallelAgentRunner(client, registry)
    plan = runner.plan(query)

    if plan and plan.get("parallel"):
        ctx = runner.execute(plan, on_group_event=my_callback)
    else:
        # fall back to normal AgentLoop
        ...
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from chalkbox.logging.bridge import get_logger

from .client import AIClient
from .context import PipelineContext
from .tools import ToolRegistry

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Planning prompt
# ---------------------------------------------------------------------------

_PLAN_SYSTEM_PROMPT = """\
You are a task planner.  Given a user request that may involve multiple \
independent operations (different projects, different directories, different \
services), decompose it into parallel groups.

RULES:
1. Each group is a sequence of shell commands that MUST run in order (they \
depend on each other within the group).
2. Groups themselves are INDEPENDENT — they can run at the same time.
3. If the task is inherently sequential or only involves one location, \
return {"parallel": false}.
4. "cwd" is the WORKING DIRECTORY where the command runs — an existing \
directory on the filesystem.  Use the project root or /tmp when the \
command doesn't need a specific directory (e.g., docker, curl, ping).  \
NEVER use a file path, socket path, or device path as cwd.
5. Return ONLY valid JSON, no markdown fences, no explanation.
6. CRITICAL — only generate commands you know will work verbatim.  \
If the task requires reading file contents first in order to know what to \
change (e.g., "fix the review findings", "apply the suggested changes", \
"update the file based on the analysis"), return {"parallel": false}.  \
NEVER invent placeholder patterns like 's/old/new/g' for files you have \
not read.  When in doubt, return {"parallel": false} so the sequential \
agent can read and edit files properly.

OUTPUT FORMAT (when parallelisable):
{
  "parallel": true,
  "groups": [
    {
      "label": "short human-readable label",
      "commands": [
        {"command": "...", "cwd": "/absolute/path/to/directory"},
        {"command": "...", "cwd": "/absolute/path/to/directory"}
      ]
    }
  ]
}

OUTPUT FORMAT (when NOT parallelisable):
{"parallel": false}\
"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class TaskGroup:
    """One independent chain of commands."""

    label: str
    commands: list[dict[str, str]]  # each: {"command": "...", "cwd": "..."}


@dataclass
class GroupResult:
    """Result of executing a single task group."""

    label: str
    outputs: list[dict[str, Any]] = field(default_factory=list)
    # each entry: {"command": ..., "cwd": ..., "output": ..., "elapsed": ...}
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0


# Callback type: (group_index, group_label, event_type, data)
GroupEventCallback = Callable[[int, str, str, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Robust JSON extraction — local LLMs are inconsistent about JSON syntax
# ---------------------------------------------------------------------------


def _parse_json_plan(text: str) -> dict | None:
    """Best-effort JSON extraction for planner responses.

    Tolerates the common ways local models butcher JSON:
      - markdown fences anywhere in the response, not just at the start
      - preamble or trailing text around the JSON object
      - Python literals (True / False / None) instead of true / false / null
      - unquoted property names ({parallel: true})
      - single-quoted strings ({'parallel': true})

    Returns the parsed dict or None when nothing salvageable was found.
    """
    # Drop any line that opens or closes a markdown fence (```json, ```)
    lines = [ln for ln in text.split("\n") if not ln.strip().startswith("```")]
    cleaned = "\n".join(lines).strip()

    # Carve out the object via outer braces — strips preamble / trailing text
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = cleaned[start : end + 1]

    # Strategy 1: strict JSON
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Python literals -> JSON literals
    normalized = re.sub(r"\bTrue\b", "true", candidate)
    normalized = re.sub(r"\bFalse\b", "false", normalized)
    normalized = re.sub(r"\bNone\b", "null", normalized)
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        pass

    # Strategy 3: quote unquoted property names — {key: val} -> {"key": val}
    quoted_keys = re.sub(
        r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):",
        r'\1"\2"\3:',
        normalized,
    )
    try:
        return json.loads(quoted_keys)
    except json.JSONDecodeError:
        pass

    # Strategy 4: swap single quotes for double quotes — last-resort for
    # Python-style dict output. Brittle if a string contains an apostrophe,
    # which is unlikely for shell-command planner output.
    swapped = quoted_keys.replace("'", '"')
    try:
        return json.loads(swapped)
    except json.JSONDecodeError:
        pass

    return None


# ---------------------------------------------------------------------------
# ParallelAgentRunner
# ---------------------------------------------------------------------------


class ParallelAgentRunner:
    """Plan and execute independent task groups in parallel."""

    def __init__(
        self,
        client: AIClient,
        registry: ToolRegistry,
        *,
        max_workers: int | None = None,
        output_max_chars: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        from .config import get_config

        cfg = get_config()

        self._client = client
        self._registry = registry
        self._max_workers = max_workers or cfg.get_by_provider("parallel", "max_workers", 4)
        self._truncate_output = cfg.get("parallel.truncate_output", True)
        self._output_max_chars = output_max_chars or cfg.get("parallel.output_max_chars", 1500)
        self._cancel_event = cancel_event

    # -- Plan ---------------------------------------------------------------

    def plan(self, query: str, conversation_history: list[dict] | None = None) -> dict | None:
        """Ask the model to decompose ``query`` into parallel groups.

        Returns the parsed JSON plan dict, or None if parsing failed.
        The caller should check ``plan.get("parallel")`` to decide whether
        to use :meth:`execute` or fall back to a normal AgentLoop.
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
        ]

        # Include recent conversation context so the planner knows about
        # previously mentioned paths / projects.
        if conversation_history:
            for turn in conversation_history[-6:]:  # last 3 exchanges max
                messages.append(turn)

        messages.append({"role": "user", "content": query})

        try:
            response = self._client.chat(messages, temperature=0.0)
            text = response.content.strip()
        except Exception as exc:
            logger.warning("[Parallel] Planner chat call failed: %s", exc)
            return None

        plan = _parse_json_plan(text)
        if plan is None:
            logger.warning(
                "[Parallel] Could not parse plan JSON. done_reason=%s "
                "completion_tokens=%d. Raw content (first 300 chars): %r",
                response.done_reason,
                response.completion_tokens,
                text[:300],
            )
            return None

        logger.debug(
            "[Parallel] Plan: parallel=%s, groups=%d",
            plan.get("parallel"),
            len(plan.get("groups", [])),
        )
        return plan

    # -- Execute ------------------------------------------------------------

    def execute(
        self,
        plan: dict,
        *,
        on_group_event: GroupEventCallback | None = None,
    ) -> PipelineContext:
        """Execute all groups in the plan concurrently."""
        groups = [TaskGroup(label=g["label"], commands=g["commands"]) for g in plan.get("groups", [])]

        if not groups:
            ctx = PipelineContext(query="(empty plan)")
            ctx.result = "No task groups to execute."
            return ctx

        total_start = time.perf_counter()
        results: list[GroupResult] = [None] * len(groups)  # type: ignore[list-item]

        def _run_group(idx: int, group: TaskGroup) -> GroupResult:
            """Execute a single group's commands sequentially."""
            gr = GroupResult(label=group.label)
            group_start = time.perf_counter()

            if on_group_event:
                on_group_event(
                    idx,
                    group.label,
                    "start",
                    {
                        "commands": len(group.commands),
                    },
                )

            for cmd_spec in group.commands:
                # Check for external cancellation
                if self._cancel_event and self._cancel_event.is_set():
                    gr.errors.append("Cancelled by user")
                    logger.info("[Parallel] Group '%s' cancelled", group.label)
                    break

                command = cmd_spec.get("command", "")
                cwd = cmd_spec.get("cwd", "")

                if on_group_event:
                    on_group_event(
                        idx,
                        group.label,
                        "command",
                        {
                            "command": command,
                            "cwd": cwd,
                        },
                    )

                cmd_start = time.perf_counter()
                try:
                    output = self._registry.execute_with_role(
                        "shell",
                        {
                            "command": command,
                            "cwd": cwd,
                        },
                    )
                except Exception as exc:
                    output = f"Error: {exc}"
                    gr.errors.append(f"{command}: {exc}")

                cmd_elapsed = time.perf_counter() - cmd_start

                entry = {
                    "command": command,
                    "cwd": cwd,
                    "output": output,
                    "elapsed": round(cmd_elapsed, 2),
                }
                gr.outputs.append(entry)

                if on_group_event:
                    on_group_event(idx, group.label, "result", entry)

                # If a command fails (exit non-zero), stop the chain for
                # this group — subsequent commands likely depend on it.
                if output.startswith("[exit") or output.startswith("Error:"):
                    gr.errors.append(f"Command failed: {command}")
                    logger.warning(
                        "[Parallel] Group '%s' — command failed, stopping chain: %s",
                        group.label,
                        command,
                    )
                    break

            gr.elapsed = time.perf_counter() - group_start

            if on_group_event:
                on_group_event(
                    idx,
                    group.label,
                    "done",
                    {
                        "elapsed": round(gr.elapsed, 2),
                        "errors": gr.errors,
                    },
                )

            return gr

        # Run groups in parallel
        logger.debug(
            "[Parallel] Executing %d groups with max_workers=%d",
            len(groups),
            self._max_workers,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(_run_group, i, g): i for i, g in enumerate(groups)}
            for future in concurrent.futures.as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    results[idx] = GroupResult(
                        label=groups[idx].label,
                        errors=[str(exc)],
                    )

        total_elapsed = time.perf_counter() - total_start

        # Aggregate into a single PipelineContext
        ctx = PipelineContext(query="(parallel execution)")
        ctx.metadata["parallel_results"] = results
        ctx.metadata["parallel_elapsed"] = total_elapsed
        ctx.metadata["parallel_groups"] = len(groups)

        # Build a human-readable summary
        parts: list[str] = []
        all_ok = True
        for gr in results:
            status = "✓" if not gr.errors else "✗"
            if gr.errors:
                all_ok = False
            parts.append(f"{status} **{gr.label}** ({gr.elapsed:.1f}s)")
            for out in gr.outputs:
                raw = out["output"].strip()
                # Strip the shell tool's command echo prefix (e.g., "$ du -sh /tmp\n")
                if raw.startswith("$ "):
                    first_nl = raw.find("\n")
                    if first_nl != -1:
                        raw = raw[first_nl + 1 :].strip()
                    else:
                        raw = "(no output)"
                # Truncate very long output but keep newlines for readability
                if self._truncate_output and len(raw) > self._output_max_chars:
                    raw = raw[: self._output_max_chars] + "\n… (truncated)"
                parts.append(f"  `{out['command']}`")
                if raw:
                    parts.append(f"  ```\n  {raw}\n  ```")
            if gr.errors:
                for e in gr.errors:
                    parts.append(f"  ⚠ {e}")
            parts.append("")  # blank line between groups

        header = f"Executed {len(groups)} task groups in parallel ({total_elapsed:.1f}s total):\n\n"
        ctx.result = header + "\n".join(parts).rstrip()

        logger.debug(
            "[Parallel] Done — %d groups, %.1fs total, all_ok=%s",
            len(groups),
            total_elapsed,
            all_ok,
        )

        return ctx
