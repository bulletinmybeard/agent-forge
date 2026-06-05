"""SAQ job functions.

- ``run_monitor_check_saq``     — web-page monitor. Runs on the remote worker.
- ``run_scheduled_command_saq`` — shell command runner. Runs on the local
  (macOS) worker.
- ``run_agent_job_saq``         — chat agent job dispatch (every mode).
- ``execute_tool_saq``          — single cross-dispatched tool call.
- ``prune_memory_saq``          — nightly retention sweep.

Implementation notes:

- Native async: ``httpx.AsyncClient`` instead of ``httpx.get/post``.
- Sync-only chunks (Playwright, subprocess) run through ``asyncio.to_thread``.
- SAQ-native retries are configured on enqueue via dispatch_compat, not here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time

import httpx

from web.server.queue.jobs_common import internal_auth_headers

logger = logging.getLogger(__name__)

# Ensure extractor logs surface in SAQ worker output.
logging.getLogger("web.server.monitor_extractors").setLevel(logging.DEBUG)

AGENTFORGE_WEB_URL = os.environ.get("AGENTFORGE_WEB_URL", "http://localhost:8200")

# Screenshots land in the shared upload dir so the web app can serve them.
_SERVICE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_UPLOAD_DIR = os.path.join(_SERVICE_ROOT, "data", "uploads")

_CHECK_TIMEOUT = 120  # seconds — Playwright can be slow
_SCHEDULED_CMD_TIMEOUT = 120  # seconds


# ---------------------------------------------------------------------------
# run_monitor_check_saq
# ---------------------------------------------------------------------------


async def run_monitor_check_saq(ctx: dict, job_id: str, check_id: int) -> dict:
    """Execute a monitor check on the host."""
    logger.info("Monitor check starting (SAQ): job=%s, check=%s", job_id, check_id)
    start = time.monotonic()

    async with httpx.AsyncClient(base_url=AGENTFORGE_WEB_URL, timeout=10, headers=internal_auth_headers()) as http:
        try:
            resp = await http.get(f"/api/monitor/jobs/{job_id}")
            if resp.status_code != 200:
                payload = _error_payload("Job not found", start)
                await _report_result(http, check_id, payload)
                return payload

            job = resp.json()

            from web.server.monitor_extractors import (
                capture_check_screenshot,
                extract,
                save_screenshot_b64,
                vision_fallback,
            )
            from web.server.monitor_service import _effective_extraction_mode

            effective_mode = _effective_extraction_mode(job.get("extraction_mode", "text"))

            result = await _to_thread(
                extract,
                url=job["url"],
                mode=effective_mode,
                css_selector=job.get("css_selector"),
                original_prompt=job.get("original_prompt"),
                screenshot=True,
            )

            screenshot_path = None
            if result.get("screenshot_b64"):
                try:
                    screenshot_path = await _to_thread(
                        save_screenshot_b64,
                        screenshot_b64=result["screenshot_b64"],
                        job_id=job_id,
                        check_id=check_id,
                        upload_dir=_UPLOAD_DIR,
                    )
                except Exception as exc:
                    logger.debug("Sidecar screenshot save failed: %s", exc)

            if not screenshot_path:
                try:
                    screenshot_path = await _to_thread(
                        capture_check_screenshot,
                        url=job["url"],
                        job_id=job_id,
                        check_id=check_id,
                        upload_dir=_UPLOAD_DIR,
                    )
                except Exception as exc:
                    logger.warning("Audit screenshot failed (non-fatal): %s", exc)

            if "error" in result and job.get("original_prompt"):
                logger.info("Primary extraction failed for %s — trying vision fallback", job["url"])
                vision_result = await _to_thread(
                    vision_fallback,
                    url=job["url"],
                    original_prompt=job.get("original_prompt"),
                )
                if vision_result:
                    result = vision_result

            if "error" in result:
                payload = _error_payload(result["error"], start)
                payload["screenshot_path"] = screenshot_path
                await _report_result(http, check_id, payload)
                return payload

            current_content = result["content"]
            current_hash = result["content_hash"]

            structured_selectors = job.get("structured_selectors")
            structured_content = None
            if structured_selectors:
                try:
                    from web.server.monitor_extractors import extract_structured

                    structured_content = await _to_thread(
                        extract_structured,
                        url=job["url"],
                        structured_selectors=structured_selectors,
                        mode=effective_mode,
                        original_prompt=job.get("original_prompt"),
                    )
                except Exception as exc:
                    logger.warning("Structured extraction failed: %s", exc)

            snap_resp = await http.get(f"/internal/monitor/jobs/{job_id}/latest-snapshot")

            if snap_resp.status_code != 200 or not snap_resp.json().get("content"):
                await _store_snapshot(http, job_id, current_content, current_hash, job, result, structured_content)
                payload = {
                    "status": "unchanged",
                    "note": "First check — baseline stored",
                    "duration_s": time.monotonic() - start,
                    "screenshot_path": screenshot_path,
                }
                await _report_result(http, check_id, payload)
                return payload

            prev_snap = snap_resp.json()
            prev_hash = prev_snap.get("content_hash", "")
            prev_content = prev_snap.get("content", "")

            from web.server.monitor_differ import compute_diff, generate_heuristic_summary, quick_check
            from web.server.monitor_extractors import compute_structured_diff

            structured_diff_result = None
            if structured_selectors and structured_content:
                prev_structured = prev_snap.get("structured_content")
                structured_diff_result = compute_structured_diff(prev_structured, structured_content)

            hash_changed = quick_check(prev_hash, current_hash)

            if not hash_changed and not structured_diff_result:
                duration = time.monotonic() - start
                payload = {
                    "status": "unchanged",
                    "prev_hash": prev_hash,
                    "current_hash": current_hash,
                    "duration_s": duration,
                    "screenshot_path": screenshot_path,
                }
                await _report_result(http, check_id, payload)
                return payload

            diff = compute_diff(prev_content, current_content)
            summary = generate_heuristic_summary(diff, job["url"])

            if structured_diff_result:
                field_changes = []
                for field, change in structured_diff_result.items():
                    old_v = change.get("old") or "(empty)"
                    new_v = change.get("new") or "(empty)"
                    field_changes.append(f"{field}: {old_v} → {new_v}")
                if field_changes:
                    summary = "Field changes: " + "; ".join(field_changes) + ". " + (summary or "")

            await _store_snapshot(http, job_id, current_content, current_hash, job, result, structured_content)

            from web.server.monitor_notifier import notify

            await _to_thread(
                notify,
                label=job["label"],
                url=job["url"],
                status="changed",
                diff_summary=summary,
                notification_method=job.get("notification_method", "terminal-notifier"),
                webhook_url=job.get("webhook_url"),
            )

            duration = time.monotonic() - start
            payload = {
                "status": "changed",
                "prev_hash": prev_hash,
                "current_hash": current_hash,
                "diff_summary": summary,
                "structured_diff": structured_diff_result,
                "diff_lines_added": diff.lines_added,
                "diff_lines_removed": diff.lines_removed,
                "duration_s": duration,
                "screenshot_path": screenshot_path,
            }

            logger.info(
                "Monitor check (SAQ): %s CHANGED (+%d/-%d, %.1fs)",
                job["label"],
                diff.lines_added,
                diff.lines_removed,
                duration,
            )

            await _report_result(http, check_id, payload)
            return payload

        except Exception as exc:
            payload = _error_payload(str(exc), start)
            try:
                await _report_result(http, check_id, payload)
            except Exception:
                logger.exception("Failed to report error payload")
            logger.exception("Monitor check failed (SAQ): job=%s, check=%s", job_id, check_id)
            return payload


# ---------------------------------------------------------------------------
# run_scheduled_command_saq
# ---------------------------------------------------------------------------


async def run_scheduled_command_saq(ctx: dict, job_id: str, run_id: str, command: str) -> dict:
    """Execute a scheduled shell command on the host."""
    logger.info("Executing scheduled command (SAQ): %s (job=%s, run=%s)", command, job_id, run_id)
    start = time.monotonic()

    try:
        result = await _to_thread(
            subprocess.run,
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_SCHEDULED_CMD_TIMEOUT,
        )
        duration = time.monotonic() - start
        status = "success" if result.returncode == 0 else "error"
        output = (result.stdout or "") + (result.stderr or "")
        if len(output) > 10000:
            output = output[:10000] + "\n... (truncated)"

        payload = {
            "status": status,
            "exit_code": result.returncode,
            "output": output or None,
            "error": result.stderr[:10000] if result.stderr and result.returncode != 0 else None,
            "duration_s": duration,
        }

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        payload = {
            "status": "error",
            "exit_code": -1,
            "output": None,
            "error": f"Command timed out after {_SCHEDULED_CMD_TIMEOUT}s",
            "duration_s": duration,
        }

    except Exception as exc:
        duration = time.monotonic() - start
        payload = {
            "status": "error",
            "exit_code": -1,
            "output": None,
            "error": str(exc),
            "duration_s": duration,
        }

    try:
        async with httpx.AsyncClient(base_url=AGENTFORGE_WEB_URL, timeout=10, headers=internal_auth_headers()) as http:
            await http.post(f"/internal/scheduler/runs/{run_id}/complete", json=payload)
    except Exception as exc:
        logger.warning("Failed to report scheduled run result: %s", exc)

    logger.info("Scheduled command (SAQ) %s: %s (%.1fs)", payload["status"], job_id, payload["duration_s"])
    return payload


# ---------------------------------------------------------------------------
# run_agent_job_saq
# ---------------------------------------------------------------------------

# Cap agent jobs at 2 in-flight per worker process. SAQ 0.26 only exposes a
# single overall concurrency knob, but agent runs are heavy (LLM, tools, RAG)
# while monitor/scheduled/tool-dispatch jobs are light. The semaphore lets the
# worker keep its overall concurrency at 4 (settings) while ensuring agent jobs
# don't crowd everything else out.
_AGENT_INFLIGHT_LIMIT = 2
_agent_semaphore: asyncio.Semaphore | None = None


def _get_agent_semaphore() -> asyncio.Semaphore:
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(_AGENT_INFLIGHT_LIMIT)
    return _agent_semaphore


async def run_agent_job_saq(
    ctx: dict,
    *,
    job_id: str,
    session_id: str,
    query: str,
    mode: str,
    overrides_json: str | None = None,
) -> None:
    """Execute a chat agent job via SAQ.

    SearchRuntime is sourced from ``ctx["runtime"]`` (loaded once in worker
    startup), cancellation flows through ``SaqCancelEvent`` (SAQ native abort
    + HTTP fallback). Concurrency is capped at ``_AGENT_INFLIGHT_LIMIT`` per
    worker via an in-process semaphore.
    """
    from web.server.queue.jobs_common import _post_status

    logger.info("SAQ worker queued agent job %s (session=%s, mode=%s)", job_id, session_id, mode)

    sem = _get_agent_semaphore()
    async with sem:
        logger.info("SAQ worker starting agent job %s", job_id)
        _post_status(job_id, "running")

        overrides = json.loads(overrides_json) if overrides_json else None

        try:
            await _execute_agent_job(ctx, job_id, session_id, query, mode, overrides)
            _post_status(job_id, "done")
            logger.info("SAQ worker completed agent job %s", job_id)
        except Exception as exc:
            logger.exception("SAQ worker agent job %s failed", job_id)
            _post_status(job_id, "error", error=str(exc))
            raise  # let SAQ's retry semantics decide


async def _execute_agent_job(
    ctx: dict,
    job_id: str,
    session_id: str,
    query: str,
    mode: str,
    overrides: dict | None,
) -> None:
    """Async dispatch to the right ``_run_*`` runner in ws_endpoint."""
    from agentforge.config import set_request_provider_override, set_request_role_override_map

    try:
        from agentforge.tools.pipeline_tools import set_pipeline_session
    except ModuleNotFoundError:
        # pipeline_tools is an optional/stripped plugin (@pipeline mode). When
        # absent, the pipeline session namespace is simply not set.
        def set_pipeline_session(_session_id: str) -> None:
            return None

    from web.server.queue.jobs_common import (
        HttpCallbackSocket,
        HttpConfirmationBroker,
        SaqCancelEvent,
        _NullDatabase,
    )
    from web.server.ws_endpoint import (
        SearchRuntime,
        _run_agent,
        _run_coding,
        _run_custom_agent,
        _run_discovery,
        _run_log_analysis,
        _run_pipeline,
        _run_research,
        _run_review,
        _run_search,
        _run_sql,
        _run_web_search,
    )

    set_pipeline_session(session_id)

    # Start a fresh per-request model chain (parent context, before any runner /
    # to_thread) so the run summary can show every model used this request
    # (refiner -> agent -> any fallback / escalation).
    from agentforge.client import reset_models_used

    reset_models_used()

    # Cross-process bridge for the per-session AI provider override. The WS
    # endpoint stuffs the value into ``overrides["_provider_override"]`` before
    # enqueuing; the worker has its own ConfigManager singleton in a separate
    # memory space, so we must set the ContextVar here. Pop so it doesn't
    # leak to the runner as an "unknown" override key.
    if overrides and "_provider_override" in overrides:
        _po = overrides.pop("_provider_override") or None
        set_request_provider_override(_po)
        if _po:
            logger.info(
                "SAQ agent job %s: applying provider_override=%s for session %s",
                job_id[:8],
                _po,
                session_id,
            )

    # Same cross-process bridge for the session id: when this agent (running on
    # a worker) dispatches a tool to another worker, the session id must travel
    # with it so the tool can prompt the user (e.g., for sudo) via this session.
    from agentforge.config import set_request_session_id

    set_request_session_id(session_id)

    # Same cross-process bridge for per-app role overrides
    # (app_provider_role_mapping, e.g., Felix). The WS endpoint stuffs the
    # computed {role: concrete} map into overrides["_role_override_map"]; apply
    # it here so the worker's profile resolution honours it. Pop so it doesn't
    # leak to the runner as an unknown override key.
    if overrides and "_role_override_map" in overrides:
        _rom = overrides.pop("_role_override_map") or None
        set_request_role_override_map(_rom)
        if _rom:
            logger.info(
                "SAQ agent job %s: applying role_override_map (%d roles) for session %s",
                job_id[:8],
                len(_rom),
                session_id,
            )

    # Browser-extension agent runs select a shell-free tool subset. The WS
    # endpoint sets overrides["_tool_profile"] (see worker-mode dispatch); pop
    # it here and forward as _run_agent's _profile_override so the agent loop
    # loads the matching tool set. Pop so it doesn't leak as an unknown override.
    _tool_profile = overrides.pop("_tool_profile", None) if overrides else None

    # SearchRuntime is loaded once per worker process in settings_shared.startup().
    # Fall back to a fresh load if a job somehow runs before startup completes
    # (shouldn't happen in practice, but cheaper than a crash).
    rt = ctx.get("runtime")
    if rt is None:
        logger.warning("SearchRuntime missing from ctx — loading on demand")
        rt = SearchRuntime()
        ctx["runtime"] = rt

    ws = HttpCallbackSocket(session_id, job_id)
    broker = HttpConfirmationBroker(session_id)
    loop = asyncio.get_event_loop()
    cancel_event = SaqCancelEvent(ctx, job_id)
    await cancel_event.start()

    try:
        # Pull pre-loaded conversation history out of overrides (injected by
        # ws_endpoint._enqueue_job) so the runner gets multi-turn context.
        conversation_history: list[dict] = []
        incognito_history = False
        if overrides and "_conversation_history" in overrides:
            conversation_history = overrides.pop("_conversation_history") or []
            incognito_history = bool(overrides.pop("_incognito_history", False))
            logger.info(
                "SAQ agent job %s: %d history turns for session %s (incognito=%s)",
                job_id[:8],
                len(conversation_history),
                session_id,
                incognito_history,
            )

        db = _NullDatabase(
            session_id,
            conversation_history=conversation_history,
            incognito_history=incognito_history,
        )

        if mode == "pipeline":
            await _run_pipeline(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode == "agent":
            await _run_agent(
                ws,
                query,
                session_id,
                rt,
                db,
                broker,
                loop,
                overrides,
                cancel_event,
                forced=True,
                _profile_override=_tool_profile,
            )
        elif mode == "web_search":
            await _run_web_search(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode == "logs":
            await _run_log_analysis(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode == "sql":
            await _run_sql(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode == "discover":
            await _run_discovery(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode == "review":
            await _run_review(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode == "research":
            await _run_research(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode == "coding":
            await _run_coding(ws, query, session_id, rt, db, broker, loop, overrides, cancel_event)
        elif mode.startswith("custom:"):
            agent_id = mode.split(":", 1)[1]
            agent_cfg = None
            for cfg in rt.custom_agents.values():
                if cfg.get("id") == agent_id:
                    agent_cfg = cfg
                    break
            if agent_cfg:
                await _run_custom_agent(
                    ws,
                    query,
                    session_id,
                    rt,
                    db,
                    broker,
                    loop,
                    overrides,
                    cancel_event,
                    agent_cfg,
                )
            else:
                raise ValueError(f"Unknown custom agent: {agent_id}")
        elif mode == "search":
            await _run_search(ws, query, session_id, rt, db, overrides)
        else:
            raise ValueError(f"Unknown job mode: {mode}")
    finally:
        await cancel_event.stop()


# ---------------------------------------------------------------------------
# prune_memory_saq — nightly retention sweep
# ---------------------------------------------------------------------------

# Retention windows. Kept module-level (not in config.yaml) for the internal
# release — if they need to be tunable, lift them behind app.config.memory.*
# in a later pass.
_CONV_MEMORY_MAX_AGE_DAYS = 30
_USER_FACTS_MAX_AGE_DAYS = 90


async def prune_memory_saq(ctx: dict) -> dict:
    """Prune aged conversation_memory + user_facts rows.

    Scheduled once per day (see settings_shared.cron_jobs). Safe to run
    multiple times concurrently — both backends are idempotent. Errors on
    one backend do not abort the other.
    """
    logger.info("prune_memory_saq: starting nightly retention sweep")

    qdrant_removed = 0
    facts_removed = 0

    try:
        from web.server.conversation_memory import get_conversation_memory

        mem = get_conversation_memory()
        if mem:
            qdrant_removed = mem.delete_older_than(_CONV_MEMORY_MAX_AGE_DAYS)
        else:
            logger.debug("prune_memory_saq: conversation_memory not initialised")
    except Exception as exc:
        logger.warning("prune_memory_saq: conversation_memory prune failed: %s", exc)

    try:
        # agentforge-web owns the SQLite file; the SAQ worker runs in the same
        # image and just needs to open it read/write with the same path
        # resolution logic as _init_database() in web.server.app.
        from pathlib import Path

        import yaml

        from web.server.database.manager import ChatDatabase

        service_root = Path(__file__).resolve().parents[3]
        config_path = service_root / "config.yaml"
        db_rel = "data/web_chat.db"
        if config_path.exists():
            with open(config_path) as fh:
                cfg = yaml.safe_load(fh) or {}
            db_rel = cfg.get("web", {}).get("database_path", db_rel)
        db = ChatDatabase(service_root / db_rel)
        facts_removed = db.delete_old_facts(_USER_FACTS_MAX_AGE_DAYS)
    except Exception as exc:
        logger.warning("prune_memory_saq: user_facts prune failed: %s", exc)

    logger.info(
        "prune_memory_saq: done. conv_memory_pruned=%s facts_pruned=%d",
        qdrant_removed,
        facts_removed,
    )
    return {
        "conv_memory_pruned": qdrant_removed,
        "facts_pruned": facts_removed,
    }


# ---------------------------------------------------------------------------
# execute_tool_saq
# ---------------------------------------------------------------------------


async def execute_tool_saq(ctx: dict, *, tool_name: str, args_json: str, session_id: str | None = None) -> str:
    """Execute a single cross-dispatched tool call.

    Sources the SearchRuntime from ``ctx["runtime"]`` (preloaded in worker
    startup) and is fully async-callable. Returns the tool's string result;
    on exception returns an ``Error: …`` string so the calling agent sees a
    tool error rather than a queue error.

    Routing is enforced by queue topology — the job is only dispatched to
    a role's tools queue (e.g., ``agentforge:tools:mac`` or ``agentforge:tools:ally``)
    where AGENTFORGE_WORKER_ROLE matches what the YAML expects, so by the time
    this runs we know the current worker is the right role for the tool.

    Confirmation for destructive / sudo commands happens on the AGENT side
    in framework/agent.py::_execute_tool_with_role before dispatch, so we
    do NOT re-check here — the remote worker's registry has no confirm
    handler wired anyway.
    """
    args = json.loads(args_json) if args_json else {}
    logger.info(
        "[execute_tool_saq] Executing '%s' with args=%s",
        tool_name,
        str(args)[:200],
    )

    rt = ctx.get("runtime")
    if rt is None:
        # Defensive fallback — startup hook should have preloaded this.
        logger.warning("SearchRuntime missing from ctx — loading on demand")
        from web.server.ws_endpoint import SearchRuntime

        rt = SearchRuntime()
        ctx["runtime"] = rt

    def _run() -> str:
        # Confirmation already happened on the agent side before dispatch (see
        # docstring); workers have no confirm handler, so skip the gate
        # explicitly — the registry now fails closed when a handler is absent.
        return str(rt.registry.execute(tool_name, args, skip_confirm=True))

    # A sudo password — unlike a y/n confirm — can only be obtained at execution
    # time (the agent doesn't know shell needs sudo until it runs). Wire a
    # provider that prompts the user back through this session's WS. Use the
    # context-isolated setter: this job runs in its own task and `to_thread`
    # copies the context into the tool thread, so concurrent tool jobs (the
    # tools queue runs at concurrency > 1, even across sessions) each get their
    # own provider and can't clobber one another's.
    from agentforge.tools.shell import reset_sudo_secret_provider_ctx, set_sudo_secret_provider_ctx

    provider_token = None
    if session_id:
        from web.server.queue.jobs_common import WorkerSecretProvider

        provider_token = set_sudo_secret_provider_ctx(WorkerSecretProvider(session_id))

    try:
        # The tool functions in the registry are sync; run them in a worker
        # thread so the event loop stays free for other jobs.
        result = await asyncio.to_thread(_run)
        logger.info("[execute_tool_saq] '%s' returned %d chars", tool_name, len(result))
        return result
    except Exception as exc:
        logger.exception("[execute_tool_saq] '%s' failed", tool_name)
        return f"Error: {exc}"
    finally:
        if provider_token is not None:
            reset_sudo_secret_provider_ctx(provider_token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _error_payload(error: str, start: float) -> dict:
    return {
        "status": "error",
        "error": error,
        "duration_s": time.monotonic() - start,
    }


async def _report_result(http: httpx.AsyncClient, check_id: int, payload: dict) -> None:
    try:
        await http.post(f"/internal/monitor/checks/{check_id}/complete", json=payload)
    except Exception as exc:
        logger.warning("Failed to report monitor check result: %s", exc)


async def _store_snapshot(
    http: httpx.AsyncClient,
    job_id: str,
    content: str,
    content_hash: str,
    job: dict,
    result: dict,
    structured_content: dict | None = None,
) -> None:
    try:
        await http.post(
            f"/internal/monitor/jobs/{job_id}/snapshots",
            json={
                "content": content,
                "content_hash": content_hash,
                "extraction_mode": job.get("extraction_mode", "text"),
                "css_selector_used": job.get("css_selector"),
                "word_count": result.get("word_count", 0),
                "structured_content": structured_content,
            },
        )
    except Exception as exc:
        logger.warning("Failed to store monitor snapshot: %s", exc)
