"""``code_codemod`` — driver tool that dispatches to a named op.

Registers into ``agentforge.coding.driver.TOOL_REGISTRY`` so the existing plan executor
can call codemods the same way it calls ``code_find`` and friends.
The op itself owns snapshotting + rollback wiring — this module is just the dispatch glue.

The tool intentionally lives in its own module (not in ``coding_tools.py``)
because it depends on the named-ops package, which would create a circular import
if ``coding_tools`` tried to import the registry directly.
"""

from __future__ import annotations

import uuid
from typing import Any

from chalkbox.logging.bridge import get_logger

from agentforge.coding import named_ops

logger = get_logger(__name__)


def code_codemod(
    op: str,
    params: dict[str, Any],
    *,
    session_id: str = "",
    burst_id: str | None = None,
) -> dict[str, Any]:
    """Dispatch to a registered named op.

    Returns a flat dict — same envelope shape the other coding tools use,
    so the driver's plan-context binding works without a special case:

    ``{ok, files_touched, sites_changed, snapshot_ids, burst_id, op, error?}``

    Failure modes return ``ok=False`` with an ``error`` string; we don't raise from here
    so the driver keeps running and the runner can render the message to the user.
    """
    op_obj = named_ops.get(op)
    if op_obj is None:
        return {
            "ok": False,
            "op": op,
            "burst_id": burst_id or "",
            "files_touched": [],
            "sites_changed": 0,
            "snapshot_ids": [],
            "error": (f"unknown named op {op!r}. Known: {sorted(named_ops.REGISTRY)}"),
        }

    try:
        validated = op_obj.param_schema(**(params or {}))
    except Exception as exc:
        return {
            "ok": False,
            "op": op,
            "burst_id": burst_id or "",
            "files_touched": [],
            "sites_changed": 0,
            "snapshot_ids": [],
            "error": f"params validation failed: {exc}",
        }

    bid = burst_id or uuid.uuid4().hex[:12]

    try:
        result = op_obj.run(validated, session_id=session_id, burst_id=bid)
    except Exception as exc:
        logger.exception("[code_codemod] op %r raised", op)
        return {
            "ok": False,
            "op": op,
            "burst_id": bid,
            "files_touched": [],
            "sites_changed": 0,
            "snapshot_ids": [],
            "error": f"op raised: {exc}",
        }

    out = result.model_dump()
    out["op"] = op
    out["burst_id"] = bid
    return out


__all__ = ["code_codemod"]
