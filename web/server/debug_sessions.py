"""Assemble a per-session debug bundle (SQLite + audit + Loki)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from .audit_log import get_audit_log
from .database import ChatDatabase
from .loki_logs import LOG_LIMIT, WINDOW_PAD_SECONDS, query_session_logs


def _unix_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1_000_000_000)


def _audit_tools_from_messages(session_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build audit-shaped tool rows from persisted ``messages[].tool_calls``.

    Worker-mode runs (@search, @agent, …) used to skip Redis audit because
    SAQ workers never called ``init_runtime()``. The SQLite messages still
    have the calls, so the Debug Audit tab can show them.
    """
    rows: list[dict[str, Any]] = []
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            args = tc.get("args") or {}
            try:
                args_json = json.dumps(args, default=str)
            except Exception:
                args_json = str(args)
            result = tc.get("result")
            if result is None:
                result_s = ""
            elif isinstance(result, str):
                result_s = result
            else:
                try:
                    result_s = json.dumps(result, default=str)
                except Exception:
                    result_s = str(result)
            is_error = result_s.startswith("ERROR") or result_s.startswith("Error:")
            rows.append(
                {
                    "session_id": session_id,
                    "tool_name": str(tc.get("name") or "unknown"),
                    "args_json": args_json[:2048],
                    "result_preview": result_s[:500],
                    "result_size": len(result_s),
                    "status": "error" if is_error else "success",
                    "error_message": result_s[:500] if is_error else "",
                    "duration_ms": 0,
                    "timestamp": msg.get("created_at") or "",
                    "mode": "",
                    "model": "",
                    "source": "messages",
                }
            )
    return rows


async def build_debug_bundle(session_id: str, db: ChatDatabase) -> dict[str, Any]:
    session = db.get_session(session_id)
    if session is None:
        raise KeyError(session_id)

    messages: list[dict[str, Any]] = []
    messages_error: str | None = None
    try:
        messages = [m.to_dict() for m in db.get_messages(session_id)]
    except Exception as exc:
        messages_error = str(exc)

    audit_tools: list[dict[str, Any]] = []
    audit_runs: list[dict[str, Any]] = []
    audit_error: str | None = None
    try:
        audit = get_audit_log()
        if audit is None:
            audit_error = "audit log not available"
        else:
            audit_tools = await audit.query_tool_executions(session_id=session_id, count=500)
            audit_runs = await audit.query_agent_runs(session_id=session_id, count=500)
    except Exception as exc:
        audit_error = str(exc)

    if not audit_tools:
        audit_tools = _audit_tools_from_messages(session_id, messages)

    logs: list[dict[str, Any]] = []
    logs_error: str | None = None
    truncated = False
    loki_url = os.environ.get("LOKI_URL", "").strip()
    if not loki_url:
        logs_error = "LOKI_URL unset"
    else:
        try:
            start = session.created_at or datetime.now()
            end = (session.updated_at or start) + timedelta(seconds=WINDOW_PAD_SECONDS)
            logs, truncated = await query_session_logs(
                session_id,
                start_ns=_unix_ns(start),
                end_ns=_unix_ns(end),
                loki_url=loki_url,
                limit=LOG_LIMIT,
            )
        except Exception as exc:
            logs_error = str(exc)

    return {
        "session": session.to_dict(),
        "messages": messages,
        "messages_error": messages_error,
        "audit_tools": audit_tools,
        "audit_runs": audit_runs,
        "audit_error": audit_error,
        "logs": logs,
        "logs_error": logs_error,
        "truncated": truncated,
    }
