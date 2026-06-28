"""Coding-mode plan driver.

A ``Plan`` is a list of ``PlanStep``s; each step names a tool in ``TOOL_REGISTRY``,
a dict of args (with optional ``$varname`` substitutions that resolve against a rolling context),
and an optional ``assign`` key that binds the tool's return value back into the context.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from chalkbox.logging.bridge import get_logger

from agentforge.tools import coding_tools

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Plan schema
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    assign: str | None = None


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool registry — mirrors coding_tools module for the driver to dispatch.
# ---------------------------------------------------------------------------


def _load_code_codemod():
    """Lazy import — pulls in the named_ops registry only when needed.

    Avoids a circular import at module-load time (codemod_tool -> named_ops -> ... -> framework.config)
    and keeps the driver tests that don't touch codemods free of pydantic dependency churn.
    """
    from agentforge.coding.codemod_tool import code_codemod

    return code_codemod


TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "code_find": coding_tools.code_find,
    "code_narrow": coding_tools.code_narrow,
    "code_transform": coding_tools.code_transform,
    "code_verify": coding_tools.code_verify,
    # NOTE: code_apply and code_undo are intentionally NOT registered
    # here. They mutate the filesystem and must only be called by the
    # runner AFTER the user's confirm dialog — never from inside a plan.
    # The planner prompt already forbids them, but LLMs drift, so the
    # registry is the hard gate. parse_plan rejects any step that
    # references a tool not in this registry.
    #
    # code_codemod IS in the registry — it's deterministic, AST-driven,
    # and snapshots every file it touches via the same rollback store
    # code_apply uses, so `@coding undo <burst_id>` reverts codemod
    # writes the same way it reverts transform writes. The runner is
    # still responsible for gating it behind the confirm dialog.
    "code_codemod": _load_code_codemod(),
}

# Tools we explicitly refuse to plan — a distinct error message is more
# helpful than the generic "unknown tool" when the planner drifts.
_FORBIDDEN_TOOLS: frozenset[str] = frozenset({"code_apply", "code_undo"})


class PlanError(Exception):
    """Raised when a plan references an unknown tool or malformed args."""


# ---------------------------------------------------------------------------
# Plan parsing + arg substitution
# ---------------------------------------------------------------------------


def parse_plan(raw: dict[str, Any]) -> Plan:
    """Validate a raw dict (from JSON) and return a ``Plan``.

    Rejects unknown tool names early so the driver never calls a missing
    function — important once the Phase-5 planner LLM is emitting plans.
    """
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list):
        raise PlanError(f"plan must have a 'steps' list, got {type(steps_raw).__name__}")

    steps: list[PlanStep] = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            raise PlanError(f"step {i} is not a dict: {s!r}")
        tool = s.get("tool")
        if not isinstance(tool, str) or not tool:
            raise PlanError(f"step {i} missing 'tool' name")
        if tool in _FORBIDDEN_TOOLS:
            raise PlanError(
                f"step {i} references {tool!r} — that tool mutates the "
                f"filesystem and can only run after the user confirms, "
                f"so it must never appear in a plan. Remove the step."
            )
        if tool not in TOOL_REGISTRY:
            raise PlanError(f"step {i} references unknown tool {tool!r}. Known: {sorted(TOOL_REGISTRY)}")
        args = s.get("args") or {}
        if not isinstance(args, dict):
            raise PlanError(f"step {i} 'args' must be a dict, got {type(args).__name__}")
        assign = s.get("assign")
        if assign is not None and not isinstance(assign, str):
            raise PlanError(f"step {i} 'assign' must be a string, got {type(assign).__name__}")
        steps.append(PlanStep(tool=tool, args={str(k): v for k, v in args.items()}, assign=assign))

    return Plan(steps=steps)


def resolve_args(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``$varname`` references in ``args`` against ``ctx``.

    Only top-level string values that start with ``$`` are treated as
    references — anything else passes through unchanged. This keeps the DSL
    tiny (no nested template language, no shell escapes).
    """
    out: dict[str, Any] = {}
    for key, val in args.items():
        if isinstance(val, str) and val.startswith("$"):
            var = val[1:]
            if var not in ctx:
                raise PlanError(f"arg {key!r} references unbound variable {val!r}. Bound: {sorted(ctx)}")
            out[key] = ctx[var]
        else:
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_plan(
    plan: Plan,
    initial_ctx: dict[str, Any] | None = None,
    on_event: Callable[..., None] | None = None,
    tool_overrides: dict[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Execute ``plan`` synchronously, returning the final context.

    ``initial_ctx`` seeds the execution context — useful for injecting
    things the plan author doesn't know (session_id, burst_id). Each
    step's ``assign`` key binds its return value into the context; if
    omitted, the result is logged and dropped.

    ``on_event`` is an optional progress callback. The driver fires:

      on_event("step_start", step=<PlanStep>, step_idx=<int>, total=<int>, resolved=<dict>)
      on_event("step_done",  step=<PlanStep>, step_idx=<int>, total=<int>, result=<any>)

    Used by the runner to push per-stage updates into the UI's Tool
    Calls panel without coupling the driver to a specific transport.
    Any exception from the callback is swallowed so a buggy UI hook
    never breaks plan execution.

    ``tool_overrides`` replaces specific tools in ``TOOL_REGISTRY`` for
    this run only — intended for runner-side instrumentation (e.g.,
    wrapping ``code_transform`` with a per-file progress callback).
    Doesn't mutate the global registry.

    Raises ``PlanError`` if a step's args reference unbound variables
    (e.g., the planner forgot to ``assign`` an earlier step).
    """
    ctx: dict[str, Any] = dict(initial_ctx or {})
    registry = {**TOOL_REGISTRY, **(tool_overrides or {})}
    total = len(plan.steps)
    for i, step in enumerate(plan.steps):
        fn = registry[step.tool]  # parse_plan already validated against TOOL_REGISTRY
        resolved = resolve_args(step.args, ctx)
        logger.info(
            "[coding.driver] step %d/%d tool=%s assign=%s args=%s",
            i + 1,
            total,
            step.tool,
            step.assign,
            {k: _preview(v) for k, v in resolved.items()},
        )
        if on_event is not None:
            try:
                on_event("step_start", step=step, step_idx=i, total=total, resolved=resolved)
            except Exception:
                logger.debug("[coding.driver] on_event step_start raised", exc_info=True)
        result = fn(**resolved)
        if step.assign:
            ctx[step.assign] = result
        if on_event is not None:
            try:
                on_event("step_done", step=step, step_idx=i, total=total, result=result)
            except Exception:
                logger.debug("[coding.driver] on_event step_done raised", exc_info=True)
    return ctx


def _preview(v: Any, cap: int = 80) -> Any:
    """Truncate long values for log lines so context windows don't blow up."""
    if isinstance(v, str):
        return v if len(v) <= cap else f"{v[:cap]}…"
    if isinstance(v, list):
        return f"<list len={len(v)}>"
    if isinstance(v, dict):
        return f"<dict keys={sorted(v)[:5]}>"
    return v


__all__ = [
    "Plan",
    "PlanError",
    "PlanStep",
    "TOOL_REGISTRY",
    "parse_plan",
    "resolve_args",
    "run_plan",
]
