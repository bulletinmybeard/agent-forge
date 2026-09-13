"""Pull result text + tool calls out of an AgentLoop PipelineContext."""

from __future__ import annotations

from typing import Any


def extract_agent_loop_output(ctx: Any) -> tuple[str, list[dict], dict[str, int]]:
    """Return ``(result_text, tool_calls, token_usage)`` from an AgentLoop ctx.

    Tool calls live on ``ctx.metadata["agent_iterations"]``. ``AgentLoop`` has
    no ``_iterations`` attribute — reading that always produced an empty list,
    so review/research result rows persisted with no tool_calls and the panel
    vanished on refresh.
    """
    if ctx is None:
        return "", [], {}
    result_text = ctx.result if hasattr(ctx, "result") and isinstance(ctx.result, str) else str(ctx)
    metadata = ctx.metadata or {}
    tokens = metadata.get("token_usage") or {}
    if not isinstance(tokens, dict):
        tokens = {}
    calls = _flatten_iterations(metadata.get("agent_iterations") or [])
    return result_text or "(no findings)", calls, tokens


def _flatten_iterations(iterations: list) -> list[dict]:
    out: list[dict] = []
    for it in iterations:
        if isinstance(it, dict):
            raw_calls = it.get("tool_calls") or []
            raw_results = it.get("tool_results") or []
        else:
            raw_calls = getattr(it, "tool_calls", None) or []
            raw_results = getattr(it, "tool_results", None) or []
        results_by_idx = {i: r for i, r in enumerate(raw_results)}
        for idx, tc in enumerate(raw_calls):
            entry = {
                "name": tc.get("name", "?"),
                "args": tc.get("arguments", tc.get("args", {})),
            }
            res = results_by_idx.get(idx)
            if isinstance(res, dict) and "result" in res:
                entry["result"] = res.get("result", "")
            out.append(entry)
    return out
