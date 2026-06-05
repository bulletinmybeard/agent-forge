"""LLM planner for ``@coding`` mode — ``coding.auto_planner: true`` path.

A single LLM call reads the user prompt and emits a JSON plan that the driver can execute.
The plan goes through ``parse_plan`` (from ``driver.py``) for structural validation — unknown tools,
bad arg shapes, and missing ``$var`` references are caught before any real work runs.

On any failure (LLM error, JSON parse error, plan validation error) we fall back to the fixed template.
The runner retains the caller's ability to run even when the planner is misbehaving.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chalkbox.logging.bridge import get_logger

from agentforge.coding.driver import Plan, PlanError, parse_plan

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "coding" / "planner.md"
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class PlannerResult:
    plan: Plan | None = None
    raw_plan: dict | None = None  # JSON dict the LLM emitted, pre-parsing
    error: str | None = None  # human-readable reason on failure


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


_OPS_PLACEHOLDER = "{{REGISTERED_OPS}}"


def _load_planner_prompt() -> str:
    """Read the planner prompt and inline the live named-ops registry.

    The prompt has a ``{{REGISTERED_OPS}}`` placeholder; we replace it at
    every call with whatever ``agentforge.coding.named_ops.list_ops_for_prompt``
    currently returns. This keeps the markdown free of stale op lists —
    adding a new op never requires editing the prompt.
    """
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if _OPS_PLACEHOLDER not in text:
        return text
    try:
        from agentforge.coding.named_ops import list_ops_for_prompt

        ops_block = list_ops_for_prompt()
    except Exception as exc:
        logger.warning("[coding.planner] failed to load named ops: %s", exc)
        ops_block = "  (named ops unavailable)"
    return text.replace(_OPS_PLACEHOLDER, ops_block)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict | None:
    m = _JSON_BLOCK_RE.search(text)
    candidate = m.group(1).strip() if m else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Planner entry point
# ---------------------------------------------------------------------------


def plan_from_prompt(
    prompt: str,
    *,
    profile: str = "coding",
    client_factory: Any = None,
) -> PlannerResult:
    """Generate a plan for ``prompt`` via one LLM call.

    Returns a ``PlannerResult``:

    - ``plan`` is set on success.
    - ``error`` is set on any failure (LLM error, parse error, validation
      error, explicit planner "cannot plan" response). ``plan`` is None.
    - ``raw_plan`` carries the JSON dict the LLM produced (when it
      parsed), for surfacing in the UI before execution.
    """
    if client_factory is None:
        from agentforge.client import AIClient

        def _default_factory(prof: str):
            return AIClient(profile=prof)

        client_factory = _default_factory

    client = client_factory(profile)
    messages = [
        {"role": "system", "content": _load_planner_prompt()},
        {"role": "user", "content": prompt.strip()},
    ]

    try:
        resp = client.chat(messages, stream=False, temperature=0.0)
    except Exception as exc:
        logger.warning("[coding.planner] LLM call failed: %s", exc)
        return PlannerResult(error=f"planner LLM call failed: {exc}")

    content = (getattr(resp, "content", "") or "").strip()
    data = _extract_json(content)
    if data is None:
        logger.warning(
            "[coding.planner] unparseable JSON in response (first 200 chars): %r",
            content[:200],
        )
        return PlannerResult(error="planner response was not parseable JSON")

    # Explicit "cannot plan" signal from the planner — surface cleanly.
    if isinstance(data.get("error"), str):
        return PlannerResult(raw_plan=data, error=data["error"])

    try:
        plan = parse_plan(data)
    except PlanError as exc:
        logger.warning("[coding.planner] plan validation failed: %s", exc)
        return PlannerResult(raw_plan=data, error=f"plan validation failed: {exc}")

    return PlannerResult(plan=plan, raw_plan=data)


__all__ = ["PlannerResult", "plan_from_prompt"]
