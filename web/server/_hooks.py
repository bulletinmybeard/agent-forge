"""Thin hook helpers — fire audit log, result store, and session event calls.

All hooks are fire-and-forget: failures are logged at DEBUG level and never
propagate to the caller.  Every function checks whether the service singleton
is initialised (``get_*`` returns ``None`` when disabled or init failed) so
callers never need to guard.

Usage in ws_endpoint runners::

    from ._hooks import hooks_run_started, hooks_run_completed, hooks_run_error, hooks_run_cancelled, hooks_log_tools

    await hooks_run_started(session_id, mode="agent", model=model, profile=profile, query=query)
    ...
    await hooks_log_tools(session_id, tool_calls, mode="agent", model=model)
    await hooks_run_completed(session_id, mode="agent", model=model, ...)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tools whose results should be treated as filesystem mutations.
# Extend this set carefully — only tools that actually modify disk state.
# ---------------------------------------------------------------------------
_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "code_edit",
        "write_file",
        "revert_file",
        "revert_lines",
        # "shell",  # opt-in: enable if you want post-write hooks on shell calls
    }
)


# ---------------------------------------------------------------------------
# Run lifecycle hooks
# ---------------------------------------------------------------------------


async def hooks_run_started(
    session_id: str,
    *,
    mode: str = "",
    model: str = "",
    profile: str = "",
    query: str = "",
) -> None:
    """Fire at the start of every runner — audit log + session event."""
    try:
        from .audit_log import get_audit_log

        audit = get_audit_log()
        if audit:
            await audit.log_agent_run(
                session_id=session_id,
                event="start",
                query_preview=query[:200] if query else "",
                mode=mode,
                model=model,
                profile=profile,
            )
    except Exception:
        logger.debug("hooks_run_started: audit log failed", exc_info=True)

    try:
        from .session_events import get_session_event_publisher

        pub = get_session_event_publisher()
        if pub:
            await pub.publish_session_event(
                event_type="run_started",
                session_id=session_id,
                mode=mode,
                model=model,
                query_preview=query[:200] if query else "",
            )
    except Exception:
        logger.debug("hooks_run_started: session events failed", exc_info=True)


async def hooks_run_completed(
    session_id: str,
    *,
    mode: str = "",
    model: str = "",
    profile: str = "",
    duration_ms: int = 0,
    iterations: int = 0,
    tool_count: int = 0,
    tools_used: str = "",
    query: str = "",
    result_text: str = "",
    incognito: bool = False,
) -> None:
    """Fire on successful completion — audit log + session event + result store.

    Result-store persistence is gated by ``memory_policy.should_store_result``
    so NONE-tier modes (``@cloud``, ``@docker``, ``@monitor`` etc.) and all
    incognito runs never leak their result text into Redis. Audit log and
    session event publish still fire — those are needed for UI + compliance
    independent of the memory tier.
    """
    try:
        from .audit_log import get_audit_log

        audit = get_audit_log()
        if audit:
            await audit.log_agent_run(
                session_id=session_id,
                event="complete",
                mode=mode,
                model=model,
                profile=profile,
                iterations=iterations,
                tool_count=tool_count,
                tools_used=tools_used,
                total_duration_ms=duration_ms,
            )
    except Exception:
        logger.debug("hooks_run_completed: audit log failed", exc_info=True)

    try:
        from .session_events import get_session_event_publisher

        pub = get_session_event_publisher()
        if pub:
            await pub.publish_session_event(
                event_type="run_completed",
                session_id=session_id,
                mode=mode,
                model=model,
                duration_ms=duration_ms,
                tool_count=tool_count,
                tools_used=tools_used,
                query_preview=query[:200] if query else "",
                status="success",
            )
    except Exception:
        logger.debug("hooks_run_completed: session events failed", exc_info=True)

    # Store agent result in result store (if non-trivial and allowed by tier)
    if result_text and len(result_text) > 20:
        try:
            from .memory_policy import should_store_result

            if not should_store_result(mode, incognito=incognito):
                logger.debug(
                    "result_store skipped by policy (mode=%r, incognito=%s)",
                    mode,
                    incognito,
                )
            else:
                from .result_store import get_result_store

                store = get_result_store()
                if store:
                    label = f"{mode}_result"
                    store.store(
                        session_id=session_id,
                        label=label,
                        data=result_text,
                        tool_name=f"_{mode}",
                        content_type="text",
                    )
        except Exception:
            logger.debug("hooks_run_completed: result store failed", exc_info=True)


async def hooks_run_error(
    session_id: str,
    *,
    mode: str = "",
    model: str = "",
    duration_ms: int = 0,
    error_message: str = "",
) -> None:
    """Fire on error — audit log + session event."""
    try:
        from .audit_log import get_audit_log

        audit = get_audit_log()
        if audit:
            await audit.log_agent_run(
                session_id=session_id,
                event="error",
                mode=mode,
                model=model,
                error_message=error_message[:500] if error_message else "",
            )
    except Exception:
        logger.debug("hooks_run_error: audit log failed", exc_info=True)

    try:
        from .session_events import get_session_event_publisher

        pub = get_session_event_publisher()
        if pub:
            await pub.publish_session_event(
                event_type="run_error",
                session_id=session_id,
                mode=mode,
                model=model,
                duration_ms=duration_ms,
                error_message=error_message[:200] if error_message else "",
            )
    except Exception:
        logger.debug("hooks_run_error: session events failed", exc_info=True)


async def hooks_run_cancelled(
    session_id: str,
    *,
    mode: str = "",
    duration_ms: int = 0,
) -> None:
    """Fire on cancellation — audit log + session event."""
    try:
        from .audit_log import get_audit_log

        audit = get_audit_log()
        if audit:
            await audit.log_agent_run(
                session_id=session_id,
                event="cancelled",
                mode=mode,
            )
    except Exception:
        logger.debug("hooks_run_cancelled: audit log failed", exc_info=True)

    try:
        from .session_events import get_session_event_publisher

        pub = get_session_event_publisher()
        if pub:
            await pub.publish_session_event(
                event_type="run_cancelled",
                session_id=session_id,
                mode=mode,
                duration_ms=duration_ms,
            )
    except Exception:
        logger.debug("hooks_run_cancelled: session events failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tool-level hooks
# ---------------------------------------------------------------------------


async def hooks_log_tools(
    session_id: str,
    tool_calls: list[dict[str, Any]],
    *,
    mode: str = "",
    model: str = "",
) -> None:
    """Log individual tool executions to audit log + auto-store to result store.

    Called after agent.run() completes, iterating over the extracted tool_calls
    list.  Individual tool results are not available (they stay inside the
    framework), so we log calls with args only.
    """
    if not tool_calls:
        return

    # Audit log — one entry per tool call
    try:
        from .audit_log import get_audit_log

        audit = get_audit_log()
        if audit:
            for tc in tool_calls:
                try:
                    await audit.log_tool_execution(
                        session_id=session_id,
                        tool_name=tc.get("name", "unknown"),
                        args=tc.get("args", {}),
                        result=None,  # not available post-hoc
                        status="success",
                        mode=mode,
                        model=model,
                    )
                except Exception:
                    logger.debug("hooks_log_tools: audit entry failed for %s", tc.get("name"))
    except Exception:
        logger.debug("hooks_log_tools: audit log import failed", exc_info=True)


# ---------------------------------------------------------------------------
# File-write verification hook
# ---------------------------------------------------------------------------


def _parse_code_edit_result(result_text: str) -> dict[str, str] | None:
    """Extract the structured header emitted by verified-write tools.

    Returns a dict with keys ``path``, ``pre_hash``, ``post_hash``, and
    (optionally) ``snapshot_id`` — or ``None`` if the tool result does not
    contain the verified-write marker.  The header lives in the first ~10
    lines of the result text::

        ✓ VERIFIED .zshrc updated (+12 -3 lines)
        pre_hash=abc...
        post_hash=def...
        path=/home/user/.zshrc
        snapshot_id=abc...

    Both ``code_edit`` and ``revert_file`` emit this format.
    """
    if not result_text or "✓ VERIFIED" not in result_text:
        return None
    header: dict[str, str] = {}
    interesting = {"pre_hash", "post_hash", "path", "snapshot_id"}
    for line in result_text.splitlines()[:10]:
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in interesting:
                header[key] = line.split("=", 1)[1].strip()
    if {"pre_hash", "post_hash", "path"} <= header.keys():
        return header
    return None


# Prefixes that identify a tool result as an EXPLICIT failure — the tool
# ran, decided not to touch the disk, and returned a human-readable error
# message.  These must NOT be classified as "unverified writes": the tool
# did exactly what it was supposed to do (refuse the write) and there's
# no upgrade for us to prompt about.
_ERROR_RESULT_PREFIXES = (
    "error:",  # revert_file "snapshot not found", read errors, etc.
    "error ",
    "no changes",  # revert_file no-op when disk already matches target
    "failed:",
    "usage:",  # tool called with bad args
    "traceback",  # raw exception surfaced by the worker
)


def _is_tool_failure(result_text: str) -> bool:
    """Return True if *result_text* is clearly a tool-level error/no-op.

    This is used by ``hooks_post_write`` to avoid logging spurious
    "Unverified write" warnings (and publishing ``file.write.unverified``
    session events) when a write tool intentionally refused to touch the
    disk — e.g., revert_file returning ``"Error: snapshot not found"``
    for a bogus pre_hash the model fabricated.  Those paths never
    performed a write, so there is nothing to verify and nothing to warn
    about.
    """
    if not result_text:
        return False
    head = result_text.lstrip()[:64].lower()
    return any(head.startswith(p) for p in _ERROR_RESULT_PREFIXES)


async def hooks_post_write(
    session_id: str,
    tool_name: str,
    args: dict[str, Any],
    result_text: str,
    *,
    mode: str = "",
    model: str = "",
) -> None:
    """Fire after any tool in _WRITE_TOOLS completes.

    Responsibilities:
      1. Parse the verification header from the tool result (code_edit only for now).
      2. Store a write receipt in the result store for audit + rollback lookup.
      3. Publish a session event so the UI can render a "✓ disk verified" badge.
      4. If the result text does NOT contain the verification header, log a
         warning and publish ``file.write.unverified`` — this surfaces tools
         that ran an unverified write path and should be upgraded.
    """
    if tool_name not in _WRITE_TOOLS:
        return

    header = _parse_code_edit_result(result_text)

    # --- Case 0: explicit tool failure — tool refused to write -----------
    # A result starting with "Error:" / "No changes" / etc. means the tool
    # intentionally did NOT touch the disk (e.g., revert_file couldn't find
    # the requested snapshot). That's a well-behaved refusal, not an
    # unverified write, so we return silently without logging or
    # publishing ``file.write.unverified`` (which previously produced
    # misleading WARNING lines in the logs after every failed revert).
    if header is None and _is_tool_failure(result_text):
        logger.debug(
            "[hooks_post_write] %s returned a failure/no-op result — skipping verified-write bookkeeping (session=%s)",
            tool_name,
            session_id,
        )
        return

    # --- Case 1: unverified write (tool did not emit the header) ---------
    if header is None:
        logger.warning(
            "[hooks_post_write] Unverified write from %s (session=%s). "
            "Tool did not emit pre_hash/post_hash. Consider upgrading this tool.",
            tool_name,
            session_id,
        )
        try:
            from .session_events import get_session_event_publisher

            pub = get_session_event_publisher()
            if pub:
                await pub.publish_session_event(
                    event_type="file.write.unverified",
                    session_id=session_id,
                    tool=tool_name,
                    path=str(args.get("file_path") or args.get("path") or ""),
                )
        except Exception:
            logger.debug("hooks_post_write: unverified publish failed", exc_info=True)
        return

    # --- Case 2: verified write — snapshot + publish ---------------------
    pre_hash = header["pre_hash"]
    post_hash = header["post_hash"]
    path = header["path"]
    snapshot_id = header.get("snapshot_id", "")
    name = Path(path).name if path else "unknown"

    # Store a write receipt in the result store (audit + future rollback lookup)
    try:
        from .result_store import get_result_store

        store = get_result_store()
        if store:
            _snap_line = f"snapshot_id={snapshot_id}\n" if snapshot_id else ""
            store.store(
                session_id=session_id,
                label=f"file_write:{name}:{post_hash[:8]}",
                data=(
                    f"path={path}\n"
                    f"tool={tool_name}\n"
                    f"mode={mode}\n"
                    f"pre_hash={pre_hash}\n"
                    f"post_hash={post_hash}\n"
                    f"{_snap_line}"
                ),
                tool_name=tool_name,
                content_type="file_write_receipt",
                source_url=path,
            )
    except Exception:
        logger.debug("hooks_post_write: result_store failed", exc_info=True)

    # Publish a session event so the UI can render the disk-verified badge
    try:
        from .session_events import get_session_event_publisher

        pub = get_session_event_publisher()
        if pub:
            event_payload: dict[str, Any] = {
                "event_type": "file.write.verified",
                "session_id": session_id,
                "tool": tool_name,
                "path": path,
                "pre_hash": pre_hash[:12],
                "post_hash": post_hash[:12],
            }
            if snapshot_id:
                event_payload["snapshot_id"] = snapshot_id[:12]
            await pub.publish_session_event(**event_payload)
    except Exception:
        logger.debug("hooks_post_write: session_events failed", exc_info=True)

    # Audit log entry carrying the verified hashes
    try:
        from .audit_log import get_audit_log

        audit = get_audit_log()
        if audit:
            await audit.log_tool_execution(
                session_id=session_id,
                tool_name=tool_name,
                args={"file_path": path, "verified": True},
                result={"pre_hash": pre_hash[:12], "post_hash": post_hash[:12]},
                status="success",
                mode=mode,
                model=model,
            )
    except Exception:
        logger.debug("hooks_post_write: audit failed", exc_info=True)
