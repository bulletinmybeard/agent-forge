"""WebSocket message protocol — shared type definitions.

All messages between client and server are JSON objects with a ``type`` field.
This module defines the valid types and helper functions for constructing them.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Server → Client message constructors
# ---------------------------------------------------------------------------


def session_init(
    session_id: str,
    tools: int,
    profiles: list[str],
    *,
    canvas_enabled: bool = False,
    provider_override: str | None = None,
) -> dict:
    return {
        "type": "session.init",
        "session_id": session_id,
        "tools": tools,
        "profiles": profiles,
        "canvas_enabled": canvas_enabled,
        "provider_override": provider_override,
    }


def agent_routing() -> dict:
    return {"type": "agent.routing"}


def agent_routed(
    profile: str,
    reason: str,
    elapsed: float,
    *,
    available_modes: list[str] | None = None,
    available_custom_aliases: list[str] | None = None,
    confidence: str = "",
) -> dict:
    """Final routing decision card.

    ``available_modes`` and ``available_custom_aliases`` let the React
    router chip render its override dropdown without an extra REST call
    — the same data is computed once on the server when the verdict is
    emitted.

    ``confidence`` is the heuristic's confidence label (high/medium/low)
    or empty when the LLM picked. The chip uses it to colour-code
    borderline picks so users can spot likely misroutes at a glance.
    """
    msg: dict[str, Any] = {
        "type": "agent.routed",
        "profile": profile,
        "reason": reason,
        "elapsed": round(elapsed, 2),
    }
    if available_modes:
        msg["available_modes"] = list(available_modes)
    if available_custom_aliases:
        msg["available_custom_aliases"] = list(available_custom_aliases)
    if confidence:
        msg["confidence"] = confidence
    return msg


def query_reroute_ack(original_mode: str, new_mode: str) -> dict:
    """Acknowledgement of a user reroute click — sent right before the
    re-run kicks off so the UI can render a small "switching to X" hint.
    """
    return {
        "type": "query.reroute.ack",
        "original_mode": original_mode,
        "new_mode": new_mode,
    }


def agent_model_fallback(
    *,
    prev_profile: str,
    prev_model: str,
    next_profile: str,
    next_model: str,
    reason: str = "",
    provider: str = "",
) -> dict:
    """Fired when ``AIClient`` advances along the profile's ``fallbacks``
    chain because the primary model raised. Lets the UI render a small
    amber badge ("Primary <model> was unavailable — fell back to <next>")
    so the user sees why the response came from a different model than
    the config card advertised.

    Multiple events can fire in one run if more than one fallback hop
    happens. Frontend should append, not replace.
    """
    return {
        "type": "agent.model_fallback",
        "prev_profile": prev_profile,
        "prev_model": prev_model,
        "next_profile": next_profile,
        "next_model": next_model,
        "reason": reason,
        "provider": provider,
    }


def agent_config(
    profile: str,
    model: str,
    tools: int,
    session_id: str,
    *,
    provider: str = "ollama",
    mode: str = "",
    no_history: bool = False,
) -> dict:
    msg: dict[str, Any] = {
        "type": "agent.config",
        "profile": profile,
        "model": model,
        "provider": provider,
        "mode": mode,
        "tools": tools,
        "session_id": session_id,
    }
    # Derive the memory tier from `mode` via the shared policy so the
    # config card can show "Memory full / session / none" alongside the
    # mode chip — makes it obvious at a glance whether the current
    # run's response will get cached in semantic memory or stay
    # session-scoped (live-data modes like @cloud / @monitor stay
    # session-scoped or none-tier and never leak into cross-session
    # recall).
    if mode:
        try:
            from web.server.memory_policy import get_tier

            msg["memory_tier"] = get_tier(mode).value
        except Exception:
            # Don't fail the WS payload just because policy lookup hiccuped.
            pass
    if no_history:
        msg["no_history"] = True
    return msg


def tool_call(name: str, args: dict[str, Any], guard: dict[str, Any] | None = None) -> dict:
    msg: dict[str, Any] = {"type": "tool.call", "name": name, "args": args}
    if guard is not None:
        msg["guard"] = guard
    return msg


def tool_calls_flush() -> dict:
    return {"type": "tool.calls.flush"}


def run_idle() -> dict:
    """Signal that no agent job is currently active for the session.

    Sent after replaying buffered ephemeral events on WS reconnect when
    ``job_store`` has no active job — the client uses this to clear any
    "working" indicator that was re-armed by replayed tool.call events.
    """
    return {"type": "run.idle"}


def secret_request(request_id: str, label: str, prompt: str) -> dict:
    return {"type": "secret.request", "request_id": request_id, "label": label, "prompt": prompt}


def confirm_request(request_id: str, prompt: str, *, auto_accepted: bool = False) -> dict:
    msg: dict[str, Any] = {
        "type": "confirm.request",
        "request_id": request_id,
        "prompt": prompt,
    }
    if auto_accepted:
        msg["auto_accepted"] = True
    return msg


def result_chunk(token: str) -> dict:
    """Send a single streaming token to the client (not persisted to DB)."""
    return {"type": "result.chunk", "token": token}


def result_done() -> dict:
    """Signal that streaming is complete (the full result follows via agent.result)."""
    return {"type": "result.done"}


def agent_result(text: str, elapsed: float) -> dict:
    return {
        "type": "agent.result",
        "text": text,
        "elapsed": round(elapsed, 1),
    }


def file_diff(
    tool: str,
    path: str,
    pre_hash: str,
    post_hash: str,
    additions: int,
    deletions: int,
    diff_text: str,
    *,
    snapshot_id: str = "",
    action: str = "edited",
) -> dict:
    """Verified-write diff payload for code_edit / revert_file / write_file.

    Carries the unified diff, file path, and hash envelope so the client can
    render a syntax-highlighted file diff card under the tool call panel.

    ``action`` is one of ``"edited"`` | ``"reverted"`` | ``"written"`` and
    controls the accent colour + verb shown in the UI header.
    """
    return {
        "type": "file.diff",
        "tool": tool,
        "action": action,
        "path": path,
        "pre_hash": pre_hash,
        "post_hash": post_hash,
        "snapshot_id": snapshot_id,
        "additions": additions,
        "deletions": deletions,
        "diff_text": diff_text,
    }


def agent_cancelled(elapsed: float) -> dict:
    """Notify the client that the current run was cancelled by the user."""
    return {
        "type": "agent.cancelled",
        "elapsed": round(elapsed, 1),
    }


def agent_summary(
    iterations: int,
    elapsed: float,
    tool_calls: int,
    tools: dict[str, int],
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict:
    msg: dict = {
        "type": "agent.summary",
        "iterations": iterations,
        "elapsed": round(elapsed, 1),
        "tool_calls": tool_calls,
        "tools": tools,
    }
    total = prompt_tokens + completion_tokens
    if total > 0:
        msg["prompt_tokens"] = prompt_tokens
        msg["completion_tokens"] = completion_tokens
        msg["total_tokens"] = total
    # Models actually used this request (query refiner -> agent -> any fallback /
    # escalation -> answer refiner), de-duped into a transition chain. Read from
    # the request-scoped contextvar AIClient appends to; empty when only one
    # model ran or tracking wasn't initialised.
    try:
        from agentforge.client import get_models_used

        models = get_models_used()
        if models:
            msg["models"] = models
    except Exception:  # noqa: BLE001 — never let summary formatting fail a run
        pass
    return msg


def search_meta(
    refined_query: str | None,
    filters: dict[str, str],
    result_count: int,
    dropped_by_floor: int,
    best_score: float,
    general_knowledge: bool,
    intent: str | None = None,
    preferred_methods: list[str] | None = None,
    demoted_by_method: int = 0,
    search_elapsed: float = 0.0,
    is_sticky: bool = False,
    parsed_query: str | None = None,
) -> dict:
    return {
        "type": "search.meta",
        "refined_query": refined_query,
        "filters": filters,
        "result_count": result_count,
        "dropped_by_floor": dropped_by_floor,
        "best_score": round(best_score, 4),
        "general_knowledge": general_knowledge,
        "intent": intent,
        "preferred_methods": preferred_methods or [],
        "demoted_by_method": demoted_by_method,
        "search_elapsed": round(search_elapsed, 2),
        "is_sticky": is_sticky,
        "parsed_query": parsed_query,
    }


def parallel_plan(groups: list[dict[str, Any]], elapsed: float) -> dict:
    """Notify the client that a parallel execution plan was created."""
    return {
        "type": "parallel.plan",
        "groups": groups,
        "elapsed": round(elapsed, 2),
    }


def parallel_group_event(
    group_idx: int,
    label: str,
    event: str,
    data: dict[str, Any],
) -> dict:
    """Stream a progress event for a parallel task group."""
    return {
        "type": "parallel.group",
        "group_idx": group_idx,
        "label": label,
        "event": event,  # start | command | result | done | error
        **data,
    }


# ---------------------------------------------------------------------------
# Discovery protocol messages
# ---------------------------------------------------------------------------


def discovery_scope(areas: list[dict[str, Any]], elapsed: float) -> dict:
    """Notify the client that investigation areas were identified."""
    return {
        "type": "discovery.scope",
        "areas": areas,
        "elapsed": round(elapsed, 2),
    }


def discovery_area_event(
    area_id: str,
    area_label: str,
    event: str,
    data: dict[str, Any],
) -> dict:
    """Stream a progress event for a discovery investigation area."""
    return {
        "type": "discovery.area",
        "area_id": area_id,
        "label": area_label,
        "event": event,  # start | probe | command | result | analyse | followup | finding | done | error
        **data,
    }


def discovery_plan(
    summary: str,
    total_reclaimable: str,
    recommendations: list[dict[str, Any]],
    elapsed: float,
) -> dict:
    """Send the synthesised discovery plan to the client for approval."""
    return {
        "type": "discovery.plan",
        "summary": summary,
        "total_reclaimable": total_reclaimable,
        "recommendations": recommendations,
        "elapsed": round(elapsed, 2),
    }


def discovery_action_event(
    action_idx: int,
    description: str,
    event: str,
    data: dict[str, Any],
) -> dict:
    """Stream a progress event during discovery plan execution."""
    return {
        "type": "discovery.action",
        "action_idx": action_idx,
        "description": description,
        "event": event,  # start | command | result | done
        **data,
    }


# ---------------------------------------------------------------------------
# Research protocol messages
# ---------------------------------------------------------------------------


def research_plan(sub_agents: list[dict[str, Any]], planner_elapsed: float) -> dict:
    """Send the research plan — list of sub-investigations — to the client."""
    return {
        "type": "research.plan",
        "sub_agents": sub_agents,
        "planner_elapsed": round(planner_elapsed, 2),
    }


def research_progress(
    phase: str,
    agent_id: str | None = None,
    label: str | None = None,
    findings_preview: str | None = None,
    tool_count: int | None = None,
    sub_agents: list[dict[str, Any]] | None = None,
) -> dict:
    """Stream a progress event for a research sub-agent."""
    msg: dict[str, Any] = {
        "type": "research.progress",
        "phase": phase,  # "starting" | "completed"
    }
    if agent_id is not None:
        msg["agent_id"] = agent_id
    if label is not None:
        msg["label"] = label
    if findings_preview is not None:
        msg["findings_preview"] = findings_preview
    if tool_count is not None:
        msg["tool_count"] = tool_count
    if sub_agents is not None:
        msg["sub_agents"] = sub_agents
    return msg


def research_activity(
    agent_id: str,
    tool_name: str,
    status: str,
    args_preview: str | None = None,
    elapsed: float | None = None,
) -> dict:
    """Stream a tool-level activity event for a research sub-agent."""
    msg: dict[str, Any] = {
        "type": "research.activity",
        "agent_id": agent_id,
        "tool": tool_name,
        "status": status,  # "running" | "done"
    }
    if args_preview is not None:
        msg["args_preview"] = args_preview
    if elapsed is not None:
        msg["elapsed"] = round(elapsed, 2)
    return msg


# ---------------------------------------------------------------------------
# Agent progress events (real-time lifecycle streaming)
# ---------------------------------------------------------------------------


def agent_iteration(
    iteration: int,
    max_iterations: int,
    messages_in_context: int,
    elapsed: float,
) -> dict:
    """Notify the client that a new agent iteration has started."""
    return {
        "type": "agent.iteration",
        "iteration": iteration,
        "max_iterations": max_iterations,
        "messages_in_context": messages_in_context,
        "elapsed": round(elapsed, 2),
    }


def agent_thinking(iteration: int, status: str, elapsed: float) -> dict:
    """Notify the client that the model is generating a response."""
    return {
        "type": "agent.thinking",
        "iteration": iteration,
        "status": status,
        "elapsed": round(elapsed, 2),
    }


def agent_tool_exec(
    iteration: int,
    name: str,
    status: str,
    elapsed: float,
    **kwargs: Any,
) -> dict:
    """Notify the client about tool execution start/completion."""
    msg: dict[str, Any] = {
        "type": "agent.tool_exec",
        "iteration": iteration,
        "name": name,
        "status": status,  # "running" | "done"
        "elapsed": round(elapsed, 2),
    }
    # Include optional fields (args, output_chars, is_error)
    for k, v in kwargs.items():
        msg[k] = v
    return msg


def agent_retry(
    iteration: int,
    attempt: int,
    max_attempts: int,
    reason: str,
    delay_seconds: int,
    elapsed: float,
) -> dict:
    """Notify the client that a model call is being retried."""
    return {
        "type": "agent.retry",
        "iteration": iteration,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "reason": reason,
        "delay_seconds": delay_seconds,
        "elapsed": round(elapsed, 2),
    }


def agent_recovery(
    iteration: int,
    tool: str,
    error: str,
    attempt: int,
    max_retries: int,
    elapsed: float,
) -> dict:
    """Notify the client that error recovery is in progress."""
    return {
        "type": "agent.recovery",
        "iteration": iteration,
        "tool": tool,
        "error": error,
        "attempt": attempt,
        "max_retries": max_retries,
        "elapsed": round(elapsed, 2),
    }


def agent_escalation(
    iteration: int,
    type_detail: str,
    consecutive_errors: int,
    search_query: str,
    elapsed: float,
) -> dict:
    """Notify the client that search escalation has fired."""
    return {
        "type": "agent.escalation",
        "iteration": iteration,
        "type_detail": type_detail,
        "consecutive_errors": consecutive_errors,
        "search_query": search_query[:200],
        "elapsed": round(elapsed, 2),
    }


def agent_warning(
    iteration: int,
    category: str,
    message: str,
    elapsed: float,
) -> dict:
    """Notify the client of a non-fatal agent issue (hallucination, loop, etc.)."""
    return {
        "type": "agent.warning",
        "iteration": iteration,
        "category": category,  # "hallucination" | "duplicate_loop"
        "message": message,
        "elapsed": round(elapsed, 2),
    }


def pipeline_step(
    step: str,
    status: str,
    elapsed: float,
    **kwargs: Any,
) -> dict:
    """Notify the client about a pipeline step (search/discovery) progress.

    Unlike agent events (iteration-based), pipeline steps are named phases
    of a single-pass pipeline: refining, searching, reranking, generating, etc.
    """
    msg: dict[str, Any] = {
        "type": "pipeline.step",
        "step": step,  # e.g., "refining", "searching", "reranking", "generating"
        "status": status,  # "running" | "done"
        "elapsed": round(elapsed, 2),
    }
    for k, v in kwargs.items():
        msg[k] = v
    return msg


def agent_error(message: str, recoverable: bool = False) -> dict:
    return {
        "type": "agent.error",
        "message": message,
        "recoverable": recoverable,
    }


def session_title(session_id: str, title: str) -> dict:
    return {
        "type": "session.title",
        "session_id": session_id,
        "title": title,
    }


def context_usage(
    used_tokens: int,
    max_tokens: int,
    percent: float,
    message_count: int,
) -> dict:
    """Notify the client about current context window utilisation."""
    return {
        "type": "context.usage",
        "used_tokens": used_tokens,
        "max_tokens": max_tokens,
        "percent": round(percent, 1),
        "message_count": message_count,
    }


def session_compacted(summary: str) -> dict:
    """Notify the client that session history was compacted."""
    return {
        "type": "session.compacted",
        "summary": summary,
    }


def secret_redacted(count: int, secret_types: list[str]) -> dict:
    """Warn the client that secrets were redacted before sending to the model."""
    return {
        "type": "secret.redacted",
        "count": count,
        "secret_types": secret_types,
        "message": (
            f"Redacted {count} secret(s) ({', '.join(secret_types)}) from the prompt before sending to the model."
        ),
    }


def prompt_refined(original: str, refined: str) -> dict:
    """Notify the client that the opening prompt was rewritten before running."""
    return {
        "type": "prompt.refined",
        "original": original,
        "refined": refined,
        "message": "Refined your prompt for clarity before running.",
    }


def instruction_saved(instruction_id: int, text: str, scope: str, total: int) -> dict:
    """Confirm that a #remember instruction was stored."""
    return {
        "type": "instruction.saved",
        "instruction_id": instruction_id,
        "text": text,
        "scope": scope,  # "session" | "global"
        "total": total,  # total active instructions for this session
    }


def instruction_cleared(count: int, scope: str) -> dict:
    """Confirm that #forget cleared instructions."""
    return {
        "type": "instruction.cleared",
        "count": count,
        "scope": scope,  # "session" | "global" | "all"
    }


def instructions_list(instructions: list[dict]) -> dict:
    """Send the full list of active instructions (e.g., on session restore)."""
    return {
        "type": "instructions.list",
        "instructions": instructions,
    }


def pong() -> dict:
    return {"type": "pong"}


# ---------------------------------------------------------------------------
# Scheduler protocol messages
# ---------------------------------------------------------------------------


def scheduler_job_created(
    job_id: str,
    label: str,
    cron: str,
    cron_human: str,
    command: str,
    elapsed: float,
) -> dict:
    """Notify the client that a scheduled job was created."""
    return {
        "type": "scheduler.job_created",
        "job_id": job_id,
        "label": label,
        "cron": cron,
        "cron_human": cron_human,
        "command": command,
        "elapsed": round(elapsed, 2),
    }


def scheduler_job_updated(job_id: str, fields: dict, elapsed: float) -> dict:
    """Notify the client that a scheduled job was updated."""
    return {
        "type": "scheduler.job_updated",
        "job_id": job_id,
        "fields": fields,
        "elapsed": round(elapsed, 2),
    }


def scheduler_job_deleted(job_id: str, label: str, elapsed: float) -> dict:
    """Notify the client that a scheduled job was deleted."""
    return {
        "type": "scheduler.job_deleted",
        "job_id": job_id,
        "label": label,
        "elapsed": round(elapsed, 2),
    }


def scheduler_job_list(jobs: list[dict], elapsed: float) -> dict:
    """Send the list of all scheduled jobs to the client."""
    return {
        "type": "scheduler.job_list",
        "jobs": jobs,
        "count": len(jobs),
        "elapsed": round(elapsed, 2),
    }


def scheduler_guard_rejected(command: str, verdict: str) -> dict:
    """Notify the client that a command was rejected by the safety guard."""
    return {
        "type": "scheduler.guard_rejected",
        "command": command,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# @coding / @code protocol messages
# ---------------------------------------------------------------------------


def coding_plan(plan: dict, source: str, elapsed: float = 0.0) -> dict:
    """Show the client the plan the coding runner is about to execute.

    ``source`` is one of ``"planner"`` | ``"template"`` — lets the UI
    label where the plan came from. ``plan`` is the raw JSON dict (the
    planner's output or the template's builder output), not the parsed
    ``Plan`` dataclass.
    """
    return {
        "type": "coding.plan",
        "source": source,
        "plan": plan,
        "elapsed": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Monitor protocol messages
# ---------------------------------------------------------------------------


def monitor_job_created(
    job_id: str,
    label: str,
    url: str,
    cron: str,
    cron_human: str,
    extraction_mode: str,
    css_selector: str | None,
    initial_snapshot: dict | None,
    elapsed: float,
) -> dict:
    """Notify the client that a monitor job was created."""
    return {
        "type": "monitor.job_created",
        "job_id": job_id,
        "label": label,
        "url": url,
        "cron": cron,
        "cron_human": cron_human,
        "extraction_mode": extraction_mode,
        "css_selector": css_selector,
        "initial_snapshot": initial_snapshot,
        "elapsed": round(elapsed, 2),
    }


def monitor_job_updated(job_id: str, fields: dict, elapsed: float) -> dict:
    """Notify the client that a monitor job was updated."""
    return {
        "type": "monitor.job_updated",
        "job_id": job_id,
        "fields": fields,
        "elapsed": round(elapsed, 2),
    }


def monitor_job_deleted(job_id: str, label: str, elapsed: float) -> dict:
    """Notify the client that a monitor job was deleted."""
    return {
        "type": "monitor.job_deleted",
        "job_id": job_id,
        "label": label,
        "elapsed": round(elapsed, 2),
    }


def monitor_job_list(jobs: list[dict], elapsed: float) -> dict:
    """Send the list of all monitor jobs to the client."""
    return {
        "type": "monitor.job_list",
        "jobs": jobs,
        "count": len(jobs),
        "elapsed": round(elapsed, 2),
    }


def monitor_check_completed(
    job_id: str,
    label: str,
    status: str,
    diff_summary: str | None = None,
    lines_added: int = 0,
    lines_removed: int = 0,
    elapsed: float = 0,
) -> dict:
    """Notify the client of a completed monitor check result."""
    return {
        "type": "monitor.check_completed",
        "job_id": job_id,
        "label": label,
        "status": status,
        "diff_summary": diff_summary,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "elapsed": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Botty — Session Awareness Layer
# ---------------------------------------------------------------------------


def botty_nudge(
    nudge_id: str,
    message: str,
    action_type: str,
    related_sessions: list[dict[str, Any]] | None = None,
    reasoning: str = "",
) -> dict:
    """Botty has a suggestion for the user."""
    return {
        "type": "botty.nudge",
        "nudge_id": nudge_id,
        "message": message,
        "action_type": action_type,
        "related_sessions": related_sessions or [],
        "reasoning": reasoning,
    }


def botty_status(phase: str, momentum: str, message_count: int) -> dict:
    """Botty's current observation of the session state."""
    return {
        "type": "botty.status",
        "phase": phase,
        "momentum": momentum,
        "message_count": message_count,
    }


def botty_recall(results: list[dict[str, Any]]) -> dict:
    """Cross-session search results from Botty."""
    return {
        "type": "botty.recall",
        "results": results,
    }


def botty_quiet(reason: str, resume_after_seconds: int = 0) -> dict:
    """Botty is going quiet (e.g., after dismissal)."""
    return {
        "type": "botty.quiet",
        "reason": reason,
        "resume_after_seconds": resume_after_seconds,
    }


# ---------------------------------------------------------------------------
# Canvas protocol messages
# ---------------------------------------------------------------------------


def canvas_item_added(item: dict[str, Any]) -> dict:
    """Notify the client that a canvas item was auto-detected and added."""
    return {
        "type": "canvas.item_added",
        "item": item,
    }


def canvas_item_deleted(item_id: int) -> dict:
    """Notify the client that a canvas item was deleted."""
    return {
        "type": "canvas.item_deleted",
        "item_id": item_id,
    }


# ---------------------------------------------------------------------------
# Query retry protocol messages
# ---------------------------------------------------------------------------


def query_retry_error(prompt_text: str, reason: str) -> dict:
    """Error response to a query.retry message.

    ``reason`` is one of ``"not_found"``, ``"in_flight"``, ``"forbidden"``.
    """
    return {
        "type": "query.retry.error",
        "prompt_text": prompt_text,
        "reason": reason,
    }
