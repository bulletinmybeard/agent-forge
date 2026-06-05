"""Registry of named, deterministic codemods for ``@coding``.

Each op exposes:

- ``name`` — registry key the planner emits in a ``code_codemod`` step
- ``description`` — one line of help text rendered into the planner prompt
- ``param_schema`` — pydantic model the planner's ``params`` dict is
  validated against
- ``run(params, *, session_id, burst_id)`` — does the work and returns a
  ``NamedOpResult``

Ops MUST register their files through the same snapshot store and Redis
burst key as ``code_apply`` — that's how ``code_undo`` reverts both
codemod-applied and transform-applied changes through one path. See the
``remove_jsx_attr`` op for the canonical wiring.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class NamedOpParams(BaseModel):
    """Base class for op-specific param schemas. Ops subclass this."""

    model_config = {"extra": "forbid"}


class NamedOpResult(BaseModel):
    ok: bool
    files_touched: list[str] = []
    sites_changed: int = 0
    snapshot_ids: list[str] = []
    error: str | None = None


@runtime_checkable
class NamedOp(Protocol):
    name: str
    description: str
    param_schema: type[NamedOpParams]

    def run(
        self,
        params: NamedOpParams,
        *,
        session_id: str,
        burst_id: str,
    ) -> NamedOpResult: ...


REGISTRY: dict[str, NamedOp] = {}


def register(op: NamedOp) -> None:
    """Register an op. Idempotent on identical re-registration."""
    existing = REGISTRY.get(op.name)
    if existing is op:
        return
    if existing is not None:
        # Surface duplicate names early — silent shadowing makes debugging
        # the planner prompt impossible.
        raise ValueError(f"named op {op.name!r} already registered as {existing!r}")
    REGISTRY[op.name] = op


def get(name: str) -> NamedOp | None:
    return REGISTRY.get(name)


def list_ops_for_prompt() -> str:
    """Render the registered ops as markdown for the planner prompt.

    Called at planner-prompt build time so the prompt always reflects
    what's actually registered — adding a new op never requires editing
    the markdown.
    """
    if not REGISTRY:
        return "(no named ops registered)"
    lines: list[str] = []
    for op in REGISTRY.values():
        fields = ", ".join(op.param_schema.model_fields.keys())
        lines.append(f"  - `{op.name}` — {op.description}")
        lines.append(f"    params: {fields}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Eager imports — each op file calls ``register(...)`` at module import,
# so importing this package populates ``REGISTRY``.
# ---------------------------------------------------------------------------

# FUTURE OPS (v2): add_jsx_attr, rename_jsx_prop, replace_component, remove_import.
# Add them as new modules and import here — no other wire-up needed.
from agentforge.coding.named_ops import remove_jsx_attr  # noqa: E402,F401

__all__ = [
    "REGISTRY",
    "NamedOp",
    "NamedOpParams",
    "NamedOpResult",
    "get",
    "list_ops_for_prompt",
    "register",
]
