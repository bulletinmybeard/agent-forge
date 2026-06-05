"""SAQ-only dispatch helpers.

Sync wrappers around SAQ enqueue / apply so that FastAPI endpoint bodies,
APScheduler callbacks, and framework tool dispatch (all sync contexts) can
hand work off to the async queue cleanly.

Four public entry points:

- ``enqueue_monitor_check(job_id, check_id)``           (fire-and-forget)
- ``enqueue_scheduled_command(job_id, run_id, command)`` (fire-and-forget)
- ``enqueue_agent_job(job_id, session_id, query, mode, overrides_json)``
  returns the SAQ job key for optional later abort
- ``saq_dispatch_tool(tool_name, args, target_role)`` enqueue + await
  a cross-role tool call

Every call goes straight to SAQ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import warnings
from typing import Any

from agentforge.tools.routing import agent_dispatch_timeout, tool_dispatch_timeout

logger = logging.getLogger(__name__)

# SAQ retry config for monitor / scheduled jobs (best-effort, idempotent tasks).
_SAQ_RETRIES = 2
_SAQ_RETRY_DELAY = 5.0
_SAQ_RETRY_BACKOFF = True

# Agent jobs stream events to the browser as they progress, so a retry would
# look like a duplicate answer. Allow exactly one retry for transient infra
# failures (worker crash, Redis blip) and nothing more.
_SAQ_AGENT_RETRIES = 1
_SAQ_AGENT_RETRY_DELAY = 10.0
_SAQ_AGENT_RETRY_BACKOFF = True
# SAQ default job timeout is 10s; agent runs (LLM calls + tool loops + RAG)
# routinely take minutes. 15 min is generous but safe — discovery / research
# modes occasionally run that long. Cancellation still works inside that
# window via the Stop button + SaqCancelEvent.
# Both default to 900s; override via tool_routing.yaml `dispatch:` or env.
_SAQ_AGENT_TIMEOUT = agent_dispatch_timeout()

# Tool calls — no retries (see cross-locality tool dispatch below).
# 900s matches shell.py's upper bound (_MIN_TIMEOUT=600s, auto-extended to 900s
# for long-running installs). The previous 120s cap killed brew/npm/pip calls
# and hid subprocess-side hangs behind a SAQ TimeoutError.
_SAQ_TOOL_TIMEOUT = tool_dispatch_timeout()

QUEUE_NAME_SHARED = "agentforge:shared"


def _run_coro_sync(coro) -> Any:
    """Run an async coroutine from a sync context, safe regardless of loop state."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _runner():
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=10)
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def _saq_enqueue(
    function: str,
    *,
    retries: int = _SAQ_RETRIES,
    retry_delay: float = _SAQ_RETRY_DELAY,
    retry_backoff: bool = _SAQ_RETRY_BACKOFF,
    timeout: float | None = None,
    queue_name: str | None = None,
    **kwargs,
) -> str | None:
    """Enqueue a SAQ job and return its key.

    Defaults to ``agentforge:shared``. Pass an explicit ``queue_name`` to pin the
    job to a specific tools queue (e.g., for mode-level routing).
    """
    from saq.job import Job

    from agentforge.tools.routing import _load
    from web.server.queue.queues import _build_client, get_queue

    if queue_name is None:
        queue = get_queue()
    elif queue_name == QUEUE_NAME_SHARED:
        queue = get_queue()
    else:
        # Match the queue_name against a known role's queue; fall through to
        # raw RedisQueue construction if the caller passed a hand-built name.
        roles_cfg = _load()["roles"]
        matching_role = next(
            (role for role, info in roles_cfg.items() if info.get("queue") == queue_name),
            None,
        )
        if matching_role is None:
            from saq.queue.redis import RedisQueue

            queue = RedisQueue(_build_client(), name=queue_name)
        else:
            from web.server.queue.queues import get_tool_queue_for_role

            queue = get_tool_queue_for_role(matching_role)
    job_kwargs: dict = dict(
        function=function,
        kwargs=kwargs,
        retries=retries,
        retry_delay=retry_delay,
        retry_backoff=retry_backoff,
    )
    if timeout is not None:
        job_kwargs["timeout"] = timeout
    job = Job(**job_kwargs)
    queued = await queue.enqueue(job)
    return getattr(queued, "key", None) if queued is not None else None


async def _saq_abort(job_key: str, *, error: str = "user cancelled") -> bool:
    """Best-effort abort of a SAQ job by key. Returns True if abort was sent."""
    from web.server.queue.queues import get_queue

    queue = get_queue()
    try:
        job = await queue.job(job_key)
        if job is None:
            return False
        await queue.abort(job, error)
        return True
    except Exception as exc:
        logger.warning("SAQ abort failed for %s: %s", job_key, exc)
        return False


# ---------------------------------------------------------------------------
# Monitor check
# ---------------------------------------------------------------------------


def enqueue_monitor_check(job_id: str, check_id: int) -> None:
    logger.info("enqueue_monitor_check: job_id=%s check_id=%s", job_id, check_id)
    _run_coro_sync(
        _saq_enqueue(
            "run_monitor_check_saq",
            job_id=job_id,
            check_id=check_id,
        )
    )


# ---------------------------------------------------------------------------
# Scheduled command
# ---------------------------------------------------------------------------


def enqueue_scheduled_command(job_id: str, run_id: str, command: str) -> None:
    logger.info("enqueue_scheduled_command: job_id=%s run_id=%s", job_id, run_id)
    _run_coro_sync(
        _saq_enqueue(
            "run_scheduled_command_saq",
            job_id=job_id,
            run_id=run_id,
            command=command,
        )
    )


# ---------------------------------------------------------------------------
# Agent job — chat queries (every mode flows through here)
# ---------------------------------------------------------------------------


# Modes pinned to a specific role (read from tool_routing.yaml -> modes).
# When a chat mode appears there, agent jobs for that mode go to that role's
# tools queue instead of agentforge:shared, so only the matching worker picks them
# up. Anything not listed stays on agentforge:shared.
def _mode_queue_name(mode: str) -> str | None:
    from agentforge.tools.routing import dispatch_mode, get_queue_for_role, get_role_for_mode

    # Single-host: ignore the role map, run the job on the shared queue. The
    # `local` tools queue has no separate worker here, so pinning would hang.
    if dispatch_mode() == "in_process":
        return None
    role = get_role_for_mode(mode)
    if role is None:
        return None
    return get_queue_for_role(role)


# Deprecated — kept so existing tests keep checking that ``coding`` is pinned.
# Builds the set lazily from the YAML on each access via __contains__.
class _ModePinnedSet:
    def __contains__(self, mode: str) -> bool:
        from agentforge.tools.routing import get_role_for_mode

        return get_role_for_mode(mode) is not None

    def __iter__(self):
        from agentforge.tools.routing import _load

        return iter(_load()["modes"].keys())


_MAC_ONLY_MODES = _ModePinnedSet()


def enqueue_agent_job(
    job_id: str,
    session_id: str,
    query: str,
    mode: str,
    overrides_json: str | None,
) -> str | None:
    """Enqueue a chat agent job. Returns the SAQ job key for later abort/lookup."""
    queue_name = _mode_queue_name(mode)
    logger.info(
        "enqueue_agent_job: job_id=%s session=%s mode=%s queue=%s",
        job_id[:8],
        session_id,
        mode,
        queue_name or QUEUE_NAME_SHARED,
    )
    return _run_coro_sync(
        _saq_enqueue(
            "run_agent_job_saq",
            retries=_SAQ_AGENT_RETRIES,
            retry_delay=_SAQ_AGENT_RETRY_DELAY,
            retry_backoff=_SAQ_AGENT_RETRY_BACKOFF,
            timeout=_SAQ_AGENT_TIMEOUT,
            queue_name=queue_name,
            job_id=job_id,
            session_id=session_id,
            query=query,
            mode=mode,
            overrides_json=overrides_json,
        )
    )


def abort_agent_job(saq_job_key: str) -> bool:
    """Abort a SAQ-queued agent job by its SAQ key. Sync wrapper for ws cancel path."""
    if not saq_job_key:
        return False
    return _run_coro_sync(_saq_abort(saq_job_key)) or False


# ---------------------------------------------------------------------------
# Cross-role tool dispatch
# ---------------------------------------------------------------------------


async def _saq_tool_apply(
    target_role: str,
    tool_name: str,
    args_json: str,
    *,
    session_id: str | None = None,
    timeout: float = _SAQ_TOOL_TIMEOUT,
) -> str:
    """Enqueue a tool call on the role's tools queue and await its string result."""
    from saq.job import Job

    from web.server.queue.queues import get_tool_queue_for_role

    queue = get_tool_queue_for_role(target_role)
    # session_id lets the worker prompt the user (e.g., for sudo) back through
    # this session's WebSocket; None when there's no session context.
    job = Job(
        function="execute_tool_saq",
        kwargs={"tool_name": tool_name, "args_json": args_json, "session_id": session_id},
        timeout=timeout,
        retries=0,  # tool calls are not retried
    )
    # Queue.apply = enqueue + wait for result. Raises on timeout/abort.
    result = await queue.apply(job, timeout=timeout)
    return str(result) if result is not None else ""


def saq_dispatch_tool(
    tool_name: str,
    args: dict,
    target_role: str | None = None,
    *,
    timeout: float = _SAQ_TOOL_TIMEOUT,
    target_locality: str | None = None,
) -> str:
    """Sync cross-role tool dispatch. Called from framework/agent.py and
    registry.execute_with_role when the tool's role doesn't match the
    current worker's role.

    *target_locality* is accepted as a deprecated alias and translated through
    the legacy locality->role translation.
    """
    from agentforge.tools.routing import translate_legacy_locality

    if target_role is None and target_locality is None:
        raise TypeError("saq_dispatch_tool() requires target_role")
    if target_locality is not None:
        warnings.warn(
            "saq_dispatch_tool(target_locality=...) is deprecated; use target_role=",
            DeprecationWarning,
            stacklevel=2,
        )
        if target_role is None:
            target_role = translate_legacy_locality(target_locality)

    args_json = json.dumps(args, default=str)
    from agentforge.config import get_request_session_id

    session_id = get_request_session_id()
    logger.info(
        "[cross_dispatch] Dispatching '%s' to %s worker (args=%s)",
        tool_name,
        target_role,
        args_json[:200],
    )
    try:
        result = _run_coro_sync(
            _saq_tool_apply(target_role, tool_name, args_json, session_id=session_id, timeout=timeout)
        )
    except Exception as exc:
        logger.error(
            "[cross_dispatch] '%s' on %s worker failed: %s",
            tool_name,
            target_role,
            exc,
        )
        raise
    return result or ""
