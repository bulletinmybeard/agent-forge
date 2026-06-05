"""DiscoveryRunner — multi-phase investigative agent for system analysis.

Unlike ParallelAgentRunner (single-round: plan → execute → done), the
DiscoveryRunner runs an iterative multi-phase workflow:

    Phase 1: SCOPING (heavy model)
        Analyse the user's goal and identify independent investigation areas.
        Each area gets initial probe commands and domain hints.

    Phase 2: INVESTIGATION (parallel, iterative)
        Each area runs its own mini agent loop:
        1. Execute probe commands
        2. Analyse output → decide if deeper investigation needed
        3. Run follow-up commands (up to N rounds)
        4. Produce a structured finding

    Phase 3: SYNTHESIS (heavy model)
        Aggregate all findings into an actionable plan with:
        - Summary of what was found
        - Prioritised recommendations (safe/risky/needs-confirmation)
        - Concrete cleanup/fix commands

    Phase 4: EXECUTION (on user approval)
        Spawn agents per recommendation group.
        Destructive command guards and sudo prompts still apply.

Usage::

    from agentforge.discovery import DiscoveryRunner

    runner = DiscoveryRunner(
        planner_client=heavy_client,
        worker_client=fast_client,
        registry=registry,
    )

    # Phase 1: Scope
    scope = runner.scope(query, conversation_history)

    # Phase 2: Investigate (parallel, iterative)
    findings = runner.investigate(scope, on_area_event=callback)

    # Phase 3: Synthesise
    plan = runner.synthesise(query, findings)

    # Phase 4: Execute (after user approval)
    results = runner.execute_plan(plan, on_action_event=callback)
"""

from __future__ import annotations

import concurrent.futures
import json
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
# Helpers
# ---------------------------------------------------------------------------

_NA_VALUES = {"n/a", "na", "none", "unknown", "-", "null", "undefined"}


def _sanitize_na(value: str) -> str:
    """Strip placeholder 'N/A' values that LLMs sometimes return.

    Returns empty string for values like "N/A", "unknown", "none", etc.
    """
    if not value or not isinstance(value, str):
        return ""
    if value.strip().lower() in _NA_VALUES:
        return ""
    return value


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class InvestigationArea:
    """One independent area to investigate (produced by the scoping phase)."""

    id: str  # e.g., "docker_images", "homebrew_cache"
    label: str  # human-readable: "Docker images & layers"
    description: str  # what to look for
    probe_commands: list[dict]  # initial commands: [{"command": "...", "cwd": "..."}]
    hints: str = ""  # domain hints for the area agent
    priority: int = 1  # 1=high, 2=medium, 3=low


@dataclass
class AreaFinding:
    """Result of investigating one area (produced by Phase 2)."""

    area_id: str
    area_label: str
    rounds: int = 0  # how many probe rounds ran
    total_size: str = ""  # e.g., "12.4 GB"
    items: list[dict] = field(default_factory=list)
    # each item: {"path": ..., "size": ..., "description": ...,
    #             "safe_to_delete": bool, "risk": "safe"|"caution"|"danger"}
    summary: str = ""  # LLM-generated summary of findings
    cleanup_commands: list[dict] = field(default_factory=list)
    # each: {"command": ..., "description": ..., "destructive": bool, "sudo": bool}
    raw_outputs: list[dict] = field(default_factory=list)
    # each: {"command": ..., "output": ..., "round": int}
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class DiscoveryPlan:
    """Actionable plan produced by the synthesis phase."""

    summary: str  # overall situation summary
    total_reclaimable: str = ""  # e.g., "~45 GB"
    recommendations: list[dict] = field(default_factory=list)
    # each: {"area": ..., "action": ..., "size": ..., "risk": ...,
    #         "commands": [...], "needs_sudo": bool, "needs_confirm": bool}
    findings: list[AreaFinding] = field(default_factory=list)


# Callback types
AreaEventCallback = Callable[[str, str, str, dict[str, Any]], None]
# (area_id, area_label, event_type, data)
# event_type: "start", "probe", "result", "analyse", "followup", "finding", "done", "error"

ActionEventCallback = Callable[[int, str, str, dict[str, Any]], None]
# (action_idx, description, event_type, data)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SCOPE_SYSTEM_PROMPT = """\
You are an expert system investigator.  Given a user's goal, identify \
INDEPENDENT areas of investigation that can be explored in parallel.

For each area, provide:
- id: short snake_case identifier
- label: human-readable name (2-5 words)
- description: what to look for and why it matters (1-2 sentences)
- probe_commands: initial shell commands to run (2-4 commands per area)
- hints: domain knowledge the investigating agent should know
- priority: 1 (high — likely biggest impact), 2 (medium), 3 (low)

RULES:
1. Each area must be INDEPENDENT — it investigates a different part of \
the system.
2. Commands must be non-destructive (read-only).  Use du, df, ls, find, \
docker system df, brew --cache, etc.
3. "cwd" is the working directory — use /tmp for global commands (docker, \
brew, system tools).  NEVER use file/socket/device paths as cwd.
4. Include at least 3 areas, at most 8.  Prioritise likely big wins.
5. Tailor areas to the user's specific concern when mentioned.
6. Return ONLY valid JSON — no markdown fences, no explanation.

OUTPUT FORMAT:
{
  "areas": [
    {
      "id": "docker_images",
      "label": "Docker images & layers",
      "description": "Check for unused, dangling, and large Docker images",
      "probe_commands": [
        {"command": "docker system df", "cwd": "/tmp"},
        {"command": "docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | sort -k2 -rh | head -20", "cwd": "/tmp"}
      ],
      "hints": "Dangling images (<none>:<none>) are always safe to delete. Old versioned images that aren't in use can usually be pruned.",
      "priority": 1
    }
  ]
}\
"""

_ANALYSE_SYSTEM_PROMPT = """\
You are a system investigator analysing command output for one specific area.

Your job:
1. Analyse the command output provided.
2. Decide if you need DEEPER investigation (more specific commands).
3. If yes, provide follow-up commands (max 3) that dig into the most \
promising leads.
4. If you have enough information, produce your final FINDING.

RULES:
- Follow-up commands must be NON-DESTRUCTIVE (read-only).
- Be thorough — extract the key facts and metrics from the output.
- If investigating disk space: focus on SIZE (what's consuming the most).
- If investigating security/config/performance: focus on ISSUES and RISKS.
- Be specific about what's safe vs risky to change.
- Return ONLY valid JSON.

OUTPUT FORMAT (when you need more info):
{
  "status": "dig_deeper",
  "reason": "Found open ports, need to check which services are listening",
  "commands": [
    {"command": "sudo lsof -i -P -n | grep LISTEN", "cwd": "/tmp"}
  ]
}

OUTPUT FORMAT (when you have enough):
{
  "status": "complete",
  "total_size": "12.4 GB",
  "items": [
    {
      "path": "/var/lib/docker/overlay2",
      "size": "8.2 GB",
      "description": "Docker build cache layers",
      "safe_to_delete": true,
      "risk": "safe"
    }
  ],
  "summary": "Docker is using 12.4 GB total. 4.2 GB are dangling images safe to prune.",
  "cleanup_commands": [
    {"command": "docker image prune -f", "description": "Remove dangling images", "destructive": true, "sudo": false}
  ]
}

IMPORTANT:
- "total_size" is ONLY for disk-space investigations (e.g., "12.4 GB"). \
For non-disk investigations (security, performance, network, etc.), \
set total_size to "" (empty string). NEVER use "N/A", "unknown", or "none".
- "items[].size" is also optional. For non-disk items, set to "" or omit.
- "items[].safe_to_delete" can also mean "safe to apply/change" for config items.
- "cleanup_commands" can include fix/remediation commands, not just deletions.\
"""

_SYNTHESISE_SYSTEM_PROMPT = """\
You are a system analyst producing an actionable plan from investigation findings.

Given findings from multiple investigation areas, produce a clear, \
prioritised plan for the user.

RULES:
1. Summarise the overall situation in 2-3 sentences.
2. List recommendations sorted by priority (highest impact first).
3. Clearly mark what's SAFE vs what NEEDS CAUTION.
4. Group related actions together.
5. For disk-space investigations: estimate total reclaimable space.
6. For security/config/performance: estimate severity and urgency.
7. Include the exact commands the user would need to run.
8. Mark commands that need sudo or user confirmation.
9. Return ONLY valid JSON.

OUTPUT FORMAT:
{
  "summary": "Your system has X issues across Y areas. The highest priority items are...",
  "total_reclaimable": "~45 GB",
  "recommendations": [
    {
      "area": "Docker build cache",
      "action": "Prune unused build cache layers",
      "size": "18.3 GB",
      "risk": "safe",
      "commands": [
        {"command": "docker builder prune -f", "description": "Clear build cache", "sudo": false}
      ],
      "needs_sudo": false,
      "needs_confirm": true
    }
  ]
}

IMPORTANT:
- "total_reclaimable" is ONLY for disk-space investigations. \
For non-disk investigations, set to "" (empty string). NEVER use "N/A".
- "size" on recommendations is optional. For non-disk items, set to "" or omit.
- "risk" should always be set: "safe", "caution", or "danger".
- NEVER use "N/A", "unknown", or "none" for any field — use "" instead.\
"""


# ---------------------------------------------------------------------------
# DiscoveryRunner
# ---------------------------------------------------------------------------


class DiscoveryRunner:
    """Multi-phase discovery agent."""

    def __init__(
        self,
        planner_client: AIClient,
        worker_client: AIClient,
        registry: ToolRegistry,
        *,
        max_rounds: int | None = None,
        max_workers: int | None = None,
        output_max_chars: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        from .config import get_config

        cfg = get_config()

        self._planner = planner_client
        self._worker = worker_client
        self._registry = registry
        self._max_rounds = max_rounds or cfg.get_by_provider("discovery", "max_rounds", 3)
        self._max_workers = max_workers or cfg.get_by_provider("discovery", "max_workers", 4)
        self._output_max_chars = output_max_chars or cfg.get("discovery.output_max_chars", 3000)
        self._cancel_event = cancel_event

        # Accumulated token usage across all LLM calls (scope + analyse + synthesise)
        self.token_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self._token_lock = threading.Lock()

    def _accumulate_tokens(self, response: Any) -> None:
        """Add token counts from a ChatResponse to the running total."""
        pt = getattr(response, "prompt_tokens", 0) or 0
        ct = getattr(response, "completion_tokens", 0) or 0
        if pt or ct:
            with self._token_lock:
                self.token_usage["prompt_tokens"] += pt
                self.token_usage["completion_tokens"] += ct
                self.token_usage["total_tokens"] += pt + ct

    # -- Phase 1: Scoping ---------------------------------------------------

    def scope(
        self,
        query: str,
        conversation_history: list[dict] | None = None,
    ) -> list[InvestigationArea]:
        """Analyse the user's goal and produce investigation areas.

        Uses the heavy planner model to identify what to investigate.
        """
        messages: list[dict] = [
            {"role": "system", "content": _SCOPE_SYSTEM_PROMPT},
        ]

        if conversation_history:
            for turn in conversation_history[-6:]:
                messages.append(turn)

        messages.append({"role": "user", "content": query})

        try:
            response = self._planner.chat(messages, temperature=0.2)
            self._accumulate_tokens(response)
            text = response.content.strip()

            # Strip markdown fences
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(l for l in lines if not l.strip().startswith("```"))

            data = json.loads(text)
            areas = []
            for a in data.get("areas", []):
                areas.append(
                    InvestigationArea(
                        id=a.get("id", "unknown"),
                        label=a.get("label", "Unknown area"),
                        description=a.get("description", ""),
                        probe_commands=a.get("probe_commands", []),
                        hints=a.get("hints", ""),
                        priority=a.get("priority", 2),
                    )
                )

            # Sort by priority
            areas.sort(key=lambda a: a.priority)

            logger.info(
                "[Discovery] Scoped %d investigation areas: %s",
                len(areas),
                ", ".join(a.id for a in areas),
            )
            return areas

        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("[Discovery] Scoping failed: %s", exc)
            return []

    # -- Phase 2: Investigation ---------------------------------------------

    def investigate(
        self,
        areas: list[InvestigationArea],
        *,
        on_area_event: AreaEventCallback | None = None,
    ) -> list[AreaFinding]:
        """Investigate all areas in parallel with iterative deepening.

        Each area runs up to max_rounds of probe → analyse → dig deeper.
        """
        if not areas:
            return []

        logger.info(
            "[Discovery] Investigating %d areas with max_workers=%d, max_rounds=%d",
            len(areas),
            self._max_workers,
            self._max_rounds,
        )

        findings: list[AreaFinding | None] = [None] * len(areas)

        def _investigate_area(idx: int, area: InvestigationArea) -> AreaFinding:
            return self._investigate_single_area(idx, area, on_area_event)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers,
        ) as pool:
            futures = {pool.submit(_investigate_area, i, area): i for i, area in enumerate(areas)}
            for future in concurrent.futures.as_completed(futures):
                idx = futures[future]
                try:
                    findings[idx] = future.result()
                except Exception as exc:
                    findings[idx] = AreaFinding(
                        area_id=areas[idx].id,
                        area_label=areas[idx].label,
                        errors=[str(exc)],
                    )

        result = [f for f in findings if f is not None]
        logger.info(
            "[Discovery] Investigation complete — %d areas, %d with errors",
            len(result),
            sum(1 for f in result if f.errors),
        )
        return result

    def _investigate_single_area(
        self,
        idx: int,
        area: InvestigationArea,
        on_area_event: AreaEventCallback | None,
    ) -> AreaFinding:
        """Run iterative investigation for a single area."""

        finding = AreaFinding(
            area_id=area.id,
            area_label=area.label,
        )
        area_start = time.perf_counter()

        if on_area_event:
            on_area_event(
                area.id,
                area.label,
                "start",
                {
                    "description": area.description,
                    "probe_commands": len(area.probe_commands),
                    "priority": area.priority,
                },
            )

        # Commands to execute this round (start with probe commands)
        pending_commands = list(area.probe_commands)
        round_num = 0

        for round_num in range(1, self._max_rounds + 1):
            if not pending_commands:
                break

            # Check for external cancellation
            if self._cancel_event and self._cancel_event.is_set():
                finding.errors.append("Cancelled by user")
                logger.info("[Discovery] Area '%s' cancelled at round %d", area.id, round_num)
                break

            if on_area_event:
                on_area_event(
                    area.id,
                    area.label,
                    "probe",
                    {
                        "round": round_num,
                        "commands": len(pending_commands),
                    },
                )

            # Execute commands for this round
            round_outputs: list[dict] = []
            for cmd_spec in pending_commands:
                if self._cancel_event and self._cancel_event.is_set():
                    break

                command = cmd_spec.get("command", "")
                cwd = cmd_spec.get("cwd", "/tmp")

                if on_area_event:
                    on_area_event(
                        area.id,
                        area.label,
                        "command",
                        {
                            "command": command,
                            "cwd": cwd,
                            "round": round_num,
                        },
                    )

                cmd_start = time.perf_counter()
                try:
                    output = self._registry.execute_with_locality(
                        "shell",
                        {
                            "command": command,
                            "cwd": cwd,
                        },
                    )
                except Exception as exc:
                    output = f"Error: {exc}"
                    finding.errors.append(f"Round {round_num}: {command}: {exc}")

                cmd_elapsed = time.perf_counter() - cmd_start

                # Strip shell tool echo prefix
                raw = output.strip()
                if raw.startswith("$ "):
                    first_nl = raw.find("\n")
                    raw = raw[first_nl + 1 :].strip() if first_nl != -1 else "(no output)"

                # Truncate if very long
                if len(raw) > self._output_max_chars:
                    raw = raw[: self._output_max_chars] + "\n… (truncated)"

                entry = {
                    "command": command,
                    "cwd": cwd,
                    "output": raw,
                    "elapsed": round(cmd_elapsed, 2),
                    "round": round_num,
                }
                round_outputs.append(entry)
                finding.raw_outputs.append(entry)

                if on_area_event:
                    on_area_event(area.id, area.label, "result", entry)

            # Analyse outputs — ask the worker LLM if we need to dig deeper
            if on_area_event:
                on_area_event(
                    area.id,
                    area.label,
                    "analyse",
                    {
                        "round": round_num,
                    },
                )

            analysis = self._analyse_round(area, round_outputs, round_num)

            if analysis is None:
                # Analysis failed — use what we have
                finding.rounds = round_num
                break

            if analysis.get("status") == "complete":
                # We have enough — populate the finding
                finding.rounds = round_num
                finding.total_size = _sanitize_na(analysis.get("total_size", ""))
                finding.items = analysis.get("items", [])
                # Sanitize N/A from item sizes too
                for item in finding.items:
                    if "size" in item:
                        item["size"] = _sanitize_na(item["size"])
                finding.summary = analysis.get("summary", "")
                finding.cleanup_commands = analysis.get("cleanup_commands", [])

                if on_area_event:
                    on_area_event(
                        area.id,
                        area.label,
                        "finding",
                        {
                            "total_size": finding.total_size,
                            "items_count": len(finding.items),
                            "cleanup_commands": len(finding.cleanup_commands),
                        },
                    )
                break

            elif analysis.get("status") == "dig_deeper":
                # Need more commands
                pending_commands = analysis.get("commands", [])
                if on_area_event:
                    on_area_event(
                        area.id,
                        area.label,
                        "followup",
                        {
                            "round": round_num,
                            "reason": analysis.get("reason", ""),
                            "commands": len(pending_commands),
                        },
                    )
            else:
                # Unknown status — stop
                finding.rounds = round_num
                break
        else:
            # Hit max rounds — produce finding from what we have
            finding.rounds = round_num
            if not finding.summary:
                finding.summary = (
                    f"Investigation reached max rounds ({self._max_rounds}) without definitive conclusion."
                )

        finding.elapsed = time.perf_counter() - area_start

        if on_area_event:
            on_area_event(
                area.id,
                area.label,
                "done",
                {
                    "rounds": finding.rounds,
                    "elapsed": round(finding.elapsed, 2),
                    "total_size": finding.total_size,
                    "errors": finding.errors,
                },
            )

        logger.info(
            "[Discovery] Area '%s' done — %d rounds, %.1fs, size=%s",
            area.label,
            finding.rounds,
            finding.elapsed,
            finding.total_size or "unknown",
        )
        return finding

    def _analyse_round(
        self,
        area: InvestigationArea,
        round_outputs: list[dict],
        round_num: int,
    ) -> dict | None:
        """Ask the worker LLM to analyse round outputs and decide next steps."""

        # Build context for the analyser
        output_text = ""
        for out in round_outputs:
            output_text += f"$ {out['command']}\n{out['output']}\n\n"

        user_msg = (
            f"AREA: {area.label}\n"
            f"DESCRIPTION: {area.description}\n"
            f"HINTS: {area.hints}\n"
            f"ROUND: {round_num}/{self._max_rounds}\n\n"
            f"COMMAND OUTPUT:\n{output_text}\n"
            f"Analyse the output.  "
        )

        if round_num >= self._max_rounds:
            user_msg += (
                "This is the LAST round — you MUST produce a complete finding now, "
                "even if incomplete.  Set status to 'complete'."
            )
        else:
            user_msg += (
                "Decide: do you have enough information for a finding, or do you need follow-up commands to dig deeper?"
            )

        messages = [
            {"role": "system", "content": _ANALYSE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        try:
            response = self._worker.chat(messages, temperature=0.1)
            self._accumulate_tokens(response)
            text = response.content.strip()

            # Strip markdown fences
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(l for l in lines if not l.strip().startswith("```"))

            return json.loads(text)

        except (json.JSONDecodeError, Exception) as exc:
            logger.warning(
                "[Discovery] Analysis failed for area '%s' round %d: %s",
                area.label,
                round_num,
                exc,
            )
            return None

    # -- Phase 3: Synthesis -------------------------------------------------

    def synthesise(
        self,
        query: str,
        findings: list[AreaFinding],
    ) -> DiscoveryPlan:
        """Aggregate findings into an actionable plan using the heavy model."""

        # Build findings summary for the synthesiser
        findings_text = ""
        for f in findings:
            findings_text += f"### {f.area_label}\n"
            findings_text += f"Size: {f.total_size or 'unknown'}\n"
            findings_text += f"Summary: {f.summary}\n"
            if f.items:
                findings_text += "Items:\n"
                for item in f.items:
                    risk = item.get("risk", "unknown")
                    safe = "✓ safe" if item.get("safe_to_delete") else "⚠ caution"
                    findings_text += (
                        f"  - {item.get('path', '?')}: {item.get('size', '?')} "
                        f"[{risk}] ({safe}) — {item.get('description', '')}\n"
                    )
            if f.cleanup_commands:
                findings_text += "Suggested cleanup:\n"
                for cmd in f.cleanup_commands:
                    sudo = " (sudo)" if cmd.get("sudo") else ""
                    findings_text += f"  - {cmd.get('command', '?')}{sudo} — {cmd.get('description', '')}\n"
            if f.errors:
                findings_text += f"Errors: {', '.join(f.errors)}\n"
            findings_text += "\n"

        messages = [
            {"role": "system", "content": _SYNTHESISE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"ORIGINAL REQUEST: {query}\n\n"
                    f"INVESTIGATION FINDINGS:\n{findings_text}\n"
                    f"Produce an actionable cleanup plan."
                ),
            },
        ]

        try:
            response = self._planner.chat(messages, temperature=0.2)
            self._accumulate_tokens(response)
            text = response.content.strip()

            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(l for l in lines if not l.strip().startswith("```"))

            data = json.loads(text)

            # Sanitize N/A values from synthesis output
            recs = data.get("recommendations", [])
            for rec in recs:
                if "size" in rec:
                    rec["size"] = _sanitize_na(rec["size"])

            plan = DiscoveryPlan(
                summary=data.get("summary", ""),
                total_reclaimable=_sanitize_na(data.get("total_reclaimable", "")),
                recommendations=recs,
                findings=findings,
            )

            logger.info(
                "[Discovery] Synthesis complete — %d recommendations, %s reclaimable",
                len(plan.recommendations),
                plan.total_reclaimable,
            )
            return plan

        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("[Discovery] Synthesis failed: %s", exc)
            # Fallback: return a basic plan from raw findings
            return DiscoveryPlan(
                summary=f"Synthesis failed ({exc}). Raw findings attached.",
                findings=findings,
            )

    # -- Phase 4: Execution -------------------------------------------------

    def execute_plan(
        self,
        plan: DiscoveryPlan,
        *,
        approved_indices: list[int] | None = None,
        on_action_event: ActionEventCallback | None = None,
    ) -> PipelineContext:
        """Execute approved recommendations from the plan."""
        recs = plan.recommendations
        if approved_indices is not None:
            recs = [recs[i] for i in approved_indices if i < len(recs)]

        if not recs:
            ctx = PipelineContext(query="(no approved actions)")
            ctx.result = "No actions to execute."
            return ctx

        total_start = time.perf_counter()
        action_results: list[dict] = []

        for i, rec in enumerate(recs):
            area = rec.get("area", "Unknown")
            action = rec.get("action", "")
            commands = rec.get("commands", [])

            if on_action_event:
                on_action_event(
                    i,
                    action,
                    "start",
                    {
                        "area": area,
                        "commands": len(commands),
                        "needs_sudo": rec.get("needs_sudo", False),
                    },
                )

            action_outputs = []
            for cmd_spec in commands:
                command = cmd_spec.get("command", "")
                description = cmd_spec.get("description", "")

                if on_action_event:
                    on_action_event(
                        i,
                        action,
                        "command",
                        {
                            "command": command,
                            "description": description,
                        },
                    )

                try:
                    output = self._registry.execute_with_locality(
                        "shell",
                        {
                            "command": command,
                            "cwd": "/tmp",
                        },
                    )
                except Exception as exc:
                    output = f"Error: {exc}"

                action_outputs.append(
                    {
                        "command": command,
                        "description": description,
                        "output": output,
                    }
                )

                if on_action_event:
                    on_action_event(
                        i,
                        action,
                        "result",
                        {
                            "command": command,
                            "output": output[:500],
                        },
                    )

            action_results.append(
                {
                    "area": area,
                    "action": action,
                    "outputs": action_outputs,
                }
            )

            if on_action_event:
                on_action_event(
                    i,
                    action,
                    "done",
                    {
                        "area": area,
                    },
                )

        total_elapsed = time.perf_counter() - total_start

        # Build result
        ctx = PipelineContext(query="(discovery execution)")
        ctx.metadata["discovery_actions"] = action_results
        ctx.metadata["discovery_elapsed"] = total_elapsed

        parts = [f"Executed {len(recs)} cleanup actions ({total_elapsed:.1f}s):\n"]
        for ar in action_results:
            parts.append(f"**{ar['area']}** — {ar['action']}")
            for out in ar["outputs"]:
                raw = out["output"].strip()
                if raw.startswith("$ "):
                    first_nl = raw.find("\n")
                    raw = raw[first_nl + 1 :].strip() if first_nl != -1 else "(done)"
                parts.append(f"  `{out['command']}`")
                if raw:
                    parts.append(f"  ```\n  {raw}\n  ```")
            parts.append("")

        ctx.result = "\n".join(parts).rstrip()

        logger.info(
            "[Discovery] Execution complete — %d actions, %.1fs",
            len(recs),
            total_elapsed,
        )
        return ctx
