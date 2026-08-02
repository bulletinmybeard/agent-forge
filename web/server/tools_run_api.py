"""Direct tool execution API for none LLM agent calls.

Exposes:

- ``POST /api/tools/run`` — run one registered tool on the worker role
  resolved from ``tool_routing.yaml`` (macOS/local tools worker or in-process).
- ``GET  /api/tools/run/{job_id}`` — poll a previously started async run.

Uses the same ``execute_tool_saq`` / registry path as agent cross-dispatch, so
shell permissions and host PATH on the native tools worker apply unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools-run"])

# Tools safe for direct IDE/API invocation without an agent confirm gate.
# shell/ssh stay off the default list — use chat + confirm, or open permissions
# and extend tools_run.allowed_tools in config.
_DEFAULT_ALLOWLIST = frozenset(
    {
        "linter_run",
        "test_runner",
        "k6_load_test",
        "docker_ps",
        "docker_stats",
        "docker_logs",
        "docker_inspect",
        "docker_images",
        "docker_df",
        "docker_compose_status",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
    }
)

# In-memory async job store (web process). Fine for single-web + poll clients.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = asyncio.Lock()
_JOB_TTL_S = 3600


class ToolRunRequest(BaseModel):
    """Body for ``POST /api/tools/run``."""

    tool: str = Field(..., description="Registered tool name, e.g. linter_run")
    args: dict[str, Any] = Field(default_factory=dict, description="Tool keyword arguments")
    timeout_s: float = Field(
        900.0,
        ge=5.0,
        le=3600.0,
        description="Max seconds to wait for the tool (SAQ apply timeout)",
    )
    wait: bool = Field(
        True,
        description="If true, block until done. If false, return job_id and poll GET /api/tools/run/{job_id}",
    )
    session_id: str | None = Field(
        None,
        description="Optional chat session for sudo/secret prompts on the tools worker",
    )


class ToolRunResponse(BaseModel):
    job_id: str
    status: str  # queued | running | done | error | cancelled
    tool: str
    role: str
    output: str = ""
    error: str | None = None
    duration_s: float | None = None
    dispatch: str | None = None  # in_process | saq


def _allowlist() -> frozenset[str]:
    """Merge default allowlist with config.yaml tools_run.allowed_tools and env."""
    allowed = set(_DEFAULT_ALLOWLIST)
    try:
        from pathlib import Path

        import yaml

        cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            extra = (cfg.get("tools_run") or {}).get("allowed_tools") or []
            if isinstance(extra, list):
                allowed.update(str(x).strip() for x in extra if str(x).strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("tools_run allowlist config load failed: %s", exc)

    env_extra = os.environ.get("AGENTFORGE_TOOLS_RUN_ALLOW", "")
    if env_extra.strip():
        allowed.update(t.strip() for t in env_extra.split(",") if t.strip())

    # Explicit denylist always wins
    deny = os.environ.get("AGENTFORGE_TOOLS_RUN_DENY", "")
    if deny.strip():
        allowed -= {t.strip() for t in deny.split(",") if t.strip()}

    return frozenset(allowed)


def _assert_allowed(tool: str) -> None:
    allowed = _allowlist()
    if tool not in allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Tool {tool!r} is not allowed for direct /api/tools/run. "
                f"Allowed: {sorted(allowed)}. "
                "Add to tools_run.allowed_tools in config.yaml or "
                "AGENTFORGE_TOOLS_RUN_ALLOW."
            ),
        )


def _resolve_role(tool: str) -> str:
    from agentforge.tools.routing import get_role_for_tool

    return get_role_for_tool(tool)


def _execute_in_process(tool: str, args: dict[str, Any]) -> str:
    """Run the tool on this process's registry (in_process / light deploys)."""
    from web.server.ws_endpoint import get_runtime

    try:
        rt = get_runtime()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Runtime not ready: {exc}") from exc

    reg = getattr(rt, "registry", None)
    if reg is None:
        raise HTTPException(status_code=503, detail="Tool registry not loaded")

    if tool not in reg.list_tools():
        raise HTTPException(
            status_code=404,
            detail=f"Tool {tool!r} not registered. Available: {reg.list_tools()[:40]}…",
        )

    # Direct REST has no confirm UI — skip confirm for allowlisted tools only.
    return str(reg.execute(tool, args, skip_confirm=True))


async def _enqueue_saq_tool(
    tool: str,
    args: dict[str, Any],
    role: str,
    *,
    timeout_s: float,
    session_id: str | None,
) -> str:
    """Enqueue execute_tool_saq on the role's tools queue; return SAQ job key."""
    from saq.job import Job

    from web.server.queue.queues import get_tool_queue_for_role

    queue = get_tool_queue_for_role(role)
    args_json = json.dumps(args, default=str)
    job = Job(
        function="execute_tool_saq",
        kwargs={
            "tool_name": tool,
            "args_json": args_json,
            "session_id": session_id,
        },
        timeout=int(timeout_s),
        retries=0,
    )
    queued = await queue.enqueue(job)
    key = getattr(queued, "key", None) if queued is not None else None
    if not key:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to enqueue {tool!r} on role {role!r} tools queue",
        )
    return str(key)


async def _await_saq_job(
    role: str,
    saq_key: str,
    *,
    timeout_s: float,
    cancel_event: asyncio.Event | None = None,
) -> str:
    """Poll a SAQ tools job until complete/failed/aborted or cancel_event is set."""
    from saq.job import Status

    from web.server.queue.queues import get_tool_queue_for_role

    queue = get_tool_queue_for_role(role)
    deadline = time.monotonic() + timeout_s
    poll = 0.25
    while True:
        if cancel_event is not None and cancel_event.is_set():
            try:
                job = await queue.job(saq_key)
                if job is not None:
                    await queue.abort(job, "user cancelled")
            except Exception as exc:  # noqa: BLE001
                logger.debug("SAQ abort after cancel failed key=%s: %s", saq_key, exc)
            raise HTTPException(status_code=499, detail="cancelled")

        if time.monotonic() > deadline:
            try:
                job = await queue.job(saq_key)
                if job is not None:
                    await queue.abort(job, "timeout")
            except Exception:  # noqa: BLE001
                pass
            raise HTTPException(
                status_code=504,
                detail=f"Tools worker timed out after {timeout_s:.0f}s",
            )

        try:
            job = await queue.job(saq_key)
        except Exception as exc:
            logger.warning("SAQ job fetch failed key=%s: %s", saq_key, exc)
            job = None

        if job is not None:
            status = getattr(job, "status", None)
            if status == Status.COMPLETE:
                result = getattr(job, "result", None)
                return str(result) if result is not None else ""
            if status == Status.FAILED:
                err = getattr(job, "error", None) or "tool failed"
                return f"Error: {err}"
            if status in (Status.ABORTED, Status.ABORTING):
                raise HTTPException(status_code=499, detail="cancelled")

        await asyncio.sleep(poll)


async def _run_tool(
    tool: str,
    args: dict[str, Any],
    *,
    timeout_s: float,
    session_id: str | None,
    cancel_event: asyncio.Event | None = None,
    job_id: str | None = None,
) -> tuple[str, str, str]:
    """Return (output, role, dispatch_mode)."""
    from agentforge.tools.routing import dispatch_mode

    role = _resolve_role(tool)
    mode = dispatch_mode()

    if mode == "in_process":
        output = await asyncio.to_thread(_execute_in_process, tool, args)
        return output, role, "in_process"

    try:
        saq_key = await _enqueue_saq_tool(tool, args, role, timeout_s=timeout_s, session_id=session_id)
        if job_id:
            cur = (await _get_job(job_id)) or {}
            await _store_job(
                job_id,
                {**cur, "saq_key": saq_key, "role": role, "_ts": time.time()},
            )
        output = await _await_saq_job(role, saq_key, timeout_s=timeout_s, cancel_event=cancel_event)
        return output, role, "saq"
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("SAQ tool dispatch failed tool=%s role=%s: %s", tool, role, exc)
        raise HTTPException(
            status_code=502,
            detail=(
                f"Tools worker for role {role!r} unreachable or timed out for {tool!r}. "
                f"Ensure the matching SAQ tools worker is running "
                f"(e.g. scripts/setup-local-worker.sh status). ({exc})"
            ),
        ) from exc


async def _store_job(job_id: str, payload: dict[str, Any]) -> None:
    async with _JOBS_LOCK:
        _JOBS[job_id] = payload
        # Opportunistic prune
        now = time.time()
        stale = [jid for jid, p in _JOBS.items() if now - float(p.get("_ts", now)) > _JOB_TTL_S]
        for jid in stale:
            _JOBS.pop(jid, None)


async def _get_job(job_id: str) -> dict[str, Any] | None:
    async with _JOBS_LOCK:
        return _JOBS.get(job_id)


@router.post("/run", response_model=ToolRunResponse)
async def tools_run(body: ToolRunRequest) -> ToolRunResponse:
    """Run a registered tool without an LLM agent loop.

    Routes to the worker role from ``tool_routing.yaml`` (e.g. macOS
    ``local`` tools worker) via SAQ, or executes in-process when
    ``AGENTFORGE_DISPATCH_MODE=in_process``.
    """
    tool = body.tool.strip()
    if not tool:
        raise HTTPException(status_code=400, detail="tool is required")

    _assert_allowed(tool)
    role = _resolve_role(tool)
    job_id = str(uuid.uuid4())
    started = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()

    cancel_event = asyncio.Event()
    await _store_job(
        job_id,
        {
            "job_id": job_id,
            "status": "queued",
            "tool": tool,
            "role": role,
            "output": "",
            "error": None,
            "duration_s": None,
            "dispatch": None,
            "created_at": now_iso,
            "cancel_event": cancel_event,
            "_ts": time.time(),
        },
    )

    if not body.wait:

        async def _bg() -> None:
            await _store_job(
                job_id,
                {
                    **((await _get_job(job_id)) or {}),
                    "status": "running",
                    "_ts": time.time(),
                },
            )
            t0 = time.monotonic()
            try:
                output, role_out, dispatch = await _run_tool(
                    tool,
                    body.args,
                    timeout_s=body.timeout_s,
                    session_id=body.session_id,
                    cancel_event=cancel_event,
                    job_id=job_id,
                )
                # Honour cancel that raced the finish
                if cancel_event.is_set():
                    await _store_job(
                        job_id,
                        {
                            "job_id": job_id,
                            "status": "cancelled",
                            "tool": tool,
                            "role": role_out,
                            "output": "",
                            "error": "cancelled",
                            "duration_s": round(time.monotonic() - t0, 3),
                            "dispatch": dispatch,
                            "created_at": now_iso,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "cancel_event": cancel_event,
                            "_ts": time.time(),
                        },
                    )
                    return
                err = None
                status = "done"
                if isinstance(output, str) and output.startswith("Error:"):
                    status = "error"
                    err = output
                await _store_job(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": status,
                        "tool": tool,
                        "role": role_out,
                        "output": output if status == "done" else "",
                        "error": err,
                        "duration_s": round(time.monotonic() - t0, 3),
                        "dispatch": dispatch,
                        "created_at": now_iso,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "cancel_event": cancel_event,
                        "_ts": time.time(),
                    },
                )
            except HTTPException as exc:
                status = "cancelled" if exc.status_code == 499 else "error"
                await _store_job(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": status,
                        "tool": tool,
                        "role": role,
                        "output": "",
                        "error": str(exc.detail),
                        "duration_s": round(time.monotonic() - t0, 3),
                        "dispatch": None,
                        "created_at": now_iso,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "cancel_event": cancel_event,
                        "_ts": time.time(),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("async tools/run failed job=%s", job_id)
                await _store_job(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": "error",
                        "tool": tool,
                        "role": role,
                        "output": "",
                        "error": str(exc),
                        "duration_s": round(time.monotonic() - t0, 3),
                        "dispatch": None,
                        "created_at": now_iso,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "cancel_event": cancel_event,
                        "_ts": time.time(),
                    },
                )

        asyncio.create_task(_bg())
        return ToolRunResponse(
            job_id=job_id,
            status="queued",
            tool=tool,
            role=role,
        )

    # Synchronous wait (still cancellable via DELETE /api/tools/run/{job_id})
    try:
        output, role_out, dispatch = await _run_tool(
            tool,
            body.args,
            timeout_s=body.timeout_s,
            session_id=body.session_id,
            cancel_event=cancel_event,
            job_id=job_id,
        )
    except HTTPException as exc:
        if exc.status_code == 499:
            return ToolRunResponse(
                job_id=job_id,
                status="cancelled",
                tool=tool,
                role=role,
                error="cancelled",
            )
        raise
    duration = round(time.monotonic() - started, 3)
    status = "done"
    error = None
    if isinstance(output, str) and output.startswith("Error:"):
        status = "error"
        error = output

    resp = ToolRunResponse(
        job_id=job_id,
        status=status,
        tool=tool,
        role=role_out,
        output=output if status == "done" else (output or ""),
        error=error,
        duration_s=duration,
        dispatch=dispatch,
    )
    await _store_job(
        job_id,
        {
            **resp.model_dump(),
            "created_at": now_iso,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "_ts": time.time(),
        },
    )
    return resp


@router.get("/run/{job_id}", response_model=ToolRunResponse)
async def tools_run_status(job_id: str) -> ToolRunResponse:
    """Poll a job started with ``wait=false``."""
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown or expired job_id")
    return ToolRunResponse(
        job_id=job["job_id"],
        status=job["status"],
        tool=job["tool"],
        role=job.get("role") or "",
        output=job.get("output") or "",
        error=job.get("error"),
        duration_s=job.get("duration_s"),
        dispatch=job.get("dispatch"),
    )


@router.delete("/run/{job_id}", response_model=ToolRunResponse)
async def tools_run_cancel(job_id: str) -> ToolRunResponse:
    """Cancel a tools/run job (best-effort SAQ abort on the tools worker)."""
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown or expired job_id")

    status = job.get("status") or ""
    if status in ("done", "error", "cancelled"):
        return ToolRunResponse(
            job_id=job["job_id"],
            status=status,
            tool=job["tool"],
            role=job.get("role") or "",
            output=job.get("output") or "",
            error=job.get("error"),
            duration_s=job.get("duration_s"),
            dispatch=job.get("dispatch"),
        )

    cancel_event = job.get("cancel_event")
    if isinstance(cancel_event, asyncio.Event):
        cancel_event.set()

    saq_key = job.get("saq_key")
    role = job.get("role") or ""
    if saq_key and role:
        try:
            from web.server.queue.queues import get_tool_queue_for_role

            queue = get_tool_queue_for_role(role)
            saq_job = await queue.job(str(saq_key))
            if saq_job is not None:
                await queue.abort(saq_job, "user cancelled")
        except Exception as exc:  # noqa: BLE001
            logger.warning("tools/run cancel SAQ abort failed job=%s: %s", job_id, exc)

    await _store_job(
        job_id,
        {
            **job,
            "status": "cancelled",
            "error": "cancelled",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "_ts": time.time(),
        },
    )
    return ToolRunResponse(
        job_id=job_id,
        status="cancelled",
        tool=job["tool"],
        role=role,
        error="cancelled",
        dispatch=job.get("dispatch"),
    )


@router.get("/run-allowlist")
async def tools_run_allowlist() -> dict[str, Any]:
    """Return the tools allowed for direct ``POST /api/tools/run``."""
    from agentforge.tools.routing import get_role_for_tool

    tools = sorted(_allowlist())
    return {
        "allowed_tools": [{"name": name, "role": get_role_for_tool(name)} for name in tools],
        "count": len(tools),
    }
