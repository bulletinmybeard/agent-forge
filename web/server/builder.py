"""@plan / @build runner — draft a markdown plan, then execute approved tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from agentforge.builder.apply import redo_bundle, save_apply_bundle, undo_bundle
from agentforge.builder.intent import (
    is_redo_build,
    is_start_over_plan,
    is_undo_build,
    parse_builder_query,
)
from agentforge.builder.store import (
    extract_section,
    merge_all_tasks,
    merge_overlapping_tasks,
    parse_frontmatter,
    parse_tasks,
    stamp_status,
    tasks_share_files,
    wrap_plan,
    write_plan_file,
)
from agentforge.review.gather import gather_review_context
from agentforge.review.loop import extract_agent_loop_output
from agentforge.review.style import resolve_review_max_workers
from agentforge.review.tools import REVIEW_TOOLS
from web.server import protocol
from web.server.agent_bridge import AgentBridge
from web.server.confirm import ConfirmationBroker

if TYPE_CHECKING:
    from web.server.database.manager import ChatDatabase
    from web.server.ws_endpoint import SearchRuntime

logger = logging.getLogger(__name__)

_PLAN_TOOLS = list(REVIEW_TOOLS)

_BUILD_TOOLS = [
    "read_file",
    "find_files",
    "grep_text",
    "git_diff",
    "git_log",
    "git_status",
    "git_blame",
    "write_file",
    "code_edit",
    "shell",
]

_PROMPTS = Path(__file__).resolve().parents[2] / "agentforge" / "prompts" / "builder"


def _token_pair(tok: dict | None) -> tuple[int, int]:
    tok = tok or {}
    return int(tok.get("prompt_tokens") or 0), int(tok.get("completion_tokens") or 0)


def _is_verify_task(task) -> bool:
    """True only when the *title* is a verify step.

    Do not scan the body: merge footers say "do not write during verify" and
    would mark T1+T2 as read-only.
    """
    title = (getattr(task, "title", "") or "").lower().strip()
    return title.startswith("verify") or title.startswith("read-only")


def _load_prompt(name: str) -> str:
    path = _PROMPTS / name
    return path.read_text(encoding="utf-8")


async def run_builder(
    ws: WebSocket,
    query: str,
    session_id: str,
    rt: SearchRuntime,
    db: ChatDatabase,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    overrides: dict | None = None,
    cancel_event: threading.Event | None = None,
    secret_broker=None,
    mode: str = "plan",
) -> None:
    """Draft (@plan) or execute (@build) a gated markdown plan."""
    total_start = time.perf_counter()
    ws_closed = False
    phase = "build" if mode == "build" else "plan"

    def send_sync(msg: dict) -> None:
        nonlocal ws_closed
        if ws_closed:
            return
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    async def send_and_persist(
        msg: dict,
        msg_type: str | None = None,
        content: str | None = None,
        tool_calls: list | None = None,
    ) -> None:
        nonlocal ws_closed
        if not ws_closed:
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                ws_closed = True
        db.add_message(
            session_id=session_id,
            role="assistant",
            msg_type=msg_type or msg.get("type", "unknown"),
            content=content,
            metadata=msg,
            tool_calls=tool_calls,
            is_incognito=(overrides or {}).get("incognito", False),
        )

    parsed = parse_builder_query(query, default_phase=phase)
    if parsed.phase in {"plan", "build"}:
        phase = parsed.phase
    all_tool_calls: list = []

    try:
        from agentforge.config import get_config as _get_framework_config
        from web.server.ws_endpoint import _persist_token_usage_raw, _send_context_usage

        db.add_message(
            session_id=session_id,
            role="user",
            msg_type="query",
            content=query,
            metadata={"type": "query", "text": query, "mode": phase},
            is_incognito=(overrides or {}).get("incognito", False),
        )

        await send_and_persist(protocol.agent_routing(), msg_type="routing")
        reason = (
            "Build — execute approved plan"
            if phase == "build"
            else "Plan — investigate the repo, then a markdown plan for approval"
        )
        await send_and_persist(protocol.agent_routed("builder", reason, 0.0), msg_type="routed")

        try:
            profile = _get_framework_config().get_profile("coder")
        except Exception:
            profile = _get_framework_config().get_profile("cloud-heavy")

        await send_and_persist(
            protocol.agent_config(
                profile="builder",
                model=profile.model,
                tools=len(_BUILD_TOOLS) if phase == "build" else len(_PLAN_TOOLS),
                session_id=session_id,
                provider=profile.provider,
                mode=phase,
            ),
            msg_type="config",
        )

        try:
            session = db.get_session(session_id)
            if session and session.title == "New chat":
                from web.server.ws_endpoint import _generate_title

                title = await asyncio.to_thread(_generate_title, query)
                db.update_session(session_id, title=title)
                if not ws_closed:
                    try:
                        await ws.send_json(protocol.session_title(session_id, title))
                    except (WebSocketDisconnect, RuntimeError):
                        ws_closed = True
        except Exception:
            logger.debug("builder auto-title failed", exc_info=True)

        from web.server._hooks import hooks_run_started

        await hooks_run_started(session_id, mode=phase, model=profile.model, profile="builder", query=query[:100])

        plan_path = str((overrides or {}).get("_plan_path") or "").strip()
        if not plan_path:
            plan_path = _plan_path_from_messages(db, session_id)
        prompt_tokens = 0
        completion_tokens = 0
        if is_undo_build(query) or is_redo_build(query):
            if not plan_path:
                report = (
                    "No plan file on this session. Run `@plan` then `@build` once "
                    "so writes are recorded, then undo/redo."
                )
            elif is_undo_build(query):
                report = undo_bundle(plan_path)
            else:
                report = redo_bundle(plan_path)
            result_msg = protocol.agent_result(report, time.perf_counter() - total_start)
            if plan_path:
                result_msg["plan_path"] = str(plan_path)
            await send_and_persist(
                result_msg,
                msg_type="result",
                content=report,
            )
        elif phase == "plan":
            report, plan_path, plan_calls, ptok, ctok = await _draft_plan(
                parsed=parsed,
                query=query,
                overrides=overrides,
                profile_name="coder",
                existing_path=None if is_start_over_plan(query) else plan_path,
                rt=rt,
                broker=broker,
                loop=loop,
                send_sync=send_sync,
                cancel_event=cancel_event,
                secret_broker=secret_broker,
                db=db,
                session_id=session_id,
            )
            prompt_tokens += ptok
            completion_tokens += ctok
            all_tool_calls.extend(plan_calls)
            result_msg = protocol.agent_result(report, time.perf_counter() - total_start)
            if plan_path:
                result_msg["plan_path"] = str(plan_path)
            if parsed.target:
                result_msg["plan_target"] = parsed.target
            await send_and_persist(
                result_msg,
                msg_type="result",
                content=report,
                tool_calls=plan_calls or None,
            )

            approved = False
            if broker is not None:
                decision = await broker.request(
                    f"Approve this plan and start @build?\n{plan_path or '(unsaved)'}\nDeny to keep drafting in chat.",
                    ignore_auto_accept=True,
                    kind="plan",
                )
                approved = bool(getattr(decision, "confirmed", decision))

            if approved and plan_path:
                Path(plan_path).write_text(
                    stamp_status(Path(plan_path).read_text(encoding="utf-8"), "approved"),
                    encoding="utf-8",
                )
                if hasattr(broker, "auto_accept"):
                    broker.auto_accept = True
                build_report, build_calls, bptok, bctok = await _execute_plan(
                    plan_path=Path(plan_path),
                    rt=rt,
                    broker=broker,
                    loop=loop,
                    send_sync=send_sync,
                    cancel_event=cancel_event,
                    overrides=overrides,
                    secret_broker=secret_broker,
                    db=db,
                    session_id=session_id,
                )
                prompt_tokens += bptok
                completion_tokens += bctok
                build_msg = protocol.agent_result(build_report, time.perf_counter() - total_start)
                build_msg["plan_path"] = str(plan_path)
                await send_and_persist(
                    build_msg,
                    msg_type="result",
                    content=build_report,
                    tool_calls=build_calls or None,
                )
                report = build_report
                all_tool_calls.extend(build_calls)
            elif not approved:
                note = "\n\nPlan left as draft. Reply with changes, or say **approve the plan**."
                await send_and_persist(
                    protocol.agent_result(note.strip(), 0.0),
                    msg_type="result",
                    content=note.strip(),
                )
        else:
            if not plan_path:
                report = (
                    "No plan file on this session. Run `@plan` first, or pass the path to "
                    "a file under `~/agent-forge/plans/`."
                )
            else:
                report, build_calls, bptok, bctok = await _execute_plan(
                    plan_path=Path(plan_path),
                    rt=rt,
                    broker=broker,
                    loop=loop,
                    send_sync=send_sync,
                    cancel_event=cancel_event,
                    overrides=overrides,
                    secret_broker=secret_broker,
                    db=db,
                    session_id=session_id,
                )
                prompt_tokens += bptok
                completion_tokens += bctok
                all_tool_calls.extend(build_calls)
            result_msg = protocol.agent_result(report, time.perf_counter() - total_start)
            if plan_path:
                result_msg["plan_path"] = str(plan_path)
            await send_and_persist(
                result_msg,
                msg_type="result",
                content=report,
                tool_calls=all_tool_calls or None,
            )

        elapsed = time.perf_counter() - total_start
        tool_counts = Counter(tc.get("name") for tc in all_tool_calls if tc.get("name"))
        await send_and_persist(
            protocol.agent_summary(
                iterations=1,
                elapsed=round(elapsed, 2),
                tool_calls=len(all_tool_calls),
                tools=dict(tool_counts),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
            msg_type="summary",
        )
        from web.server._hooks import hooks_run_completed

        await hooks_run_completed(
            session_id,
            query=query,
            mode=phase,
            model=profile.model,
            profile="builder",
            duration_ms=int(elapsed * 1000),
            iterations=1,
            tool_count=len(all_tool_calls),
            result_text=(report or "")[:2000],
        )
        _persist_token_usage_raw(db, session_id, prompt_tokens, completion_tokens)
        await _send_context_usage(ws, db, session_id, profile.model)

    except asyncio.CancelledError:
        await send_and_persist(
            protocol.agent_cancelled(time.perf_counter() - total_start),
            msg_type="cancelled",
        )
        raise
    except Exception as exc:
        logger.exception("builder failed")
        await send_and_persist(
            protocol.agent_error(str(exc), recoverable=False),
            msg_type="error",
            content=str(exc),
        )


async def _draft_plan(
    *,
    parsed,
    query: str,
    overrides,
    profile_name: str,
    existing_path: str | None = None,
    rt=None,
    broker=None,
    loop=None,
    send_sync=None,
    cancel_event=None,
    secret_broker=None,
    db=None,
    session_id: str = "",
) -> tuple[str, Path | None, list]:
    from agentforge.agent import AgentLoop
    from agentforge.client import AIClient
    from web.server.ws_endpoint import _attachment_text_block, _strip_wrapping_fence

    gather_txt = ""
    if parsed.target:
        gathered = gather_review_context(parsed.target, instruction=parsed.instruction)
        gather_txt = gathered.preamble
    docs = _attachment_text_block(overrides)
    system = _load_prompt("planner.md")
    user = (
        f"{parsed.instruction}\n\n"
        f"Target repository: `{parsed.target or '(none given — ask or infer, do not use the worker cwd)'}`\n"
        f"Requested branch: `{parsed.branch or '(none)'}`\n\n"
        "Investigate with tools first. Cite `file:line` in Findings. Then write the plan.\n\n"
        f"{gather_txt}{docs}"
    )
    if existing_path and Path(existing_path).is_file():
        user += (
            "\n\n--- current plan ---\n"
            f"{Path(existing_path).read_text(encoding='utf-8')}\n"
            "--- end ---\nRevise this plan per the user. Keep task ids stable where possible.\n"
        )
    client = AIClient(profile=profile_name)
    calls: list = []
    body = ""
    ptok = ctok = 0
    if rt is not None:
        bridge = AgentBridge(
            send_sync,
            broker,
            loop,
            secret_broker=secret_broker,
            db=db,
            session_id=session_id,
            incognito=(overrides or {}).get("incognito", False),
        )
        bridge.setup_registry(rt.registry)

        def _run():
            agent = AgentLoop(
                client,
                rt.registry,
                system_prompt=system,
                tools=_PLAN_TOOLS,
                max_iterations=18,
                verbose=False,
                cancel_event=cancel_event,
                deep_think=bool(client.profile.thinking_budget),
            )
            return agent.run(user)

        ctx = await asyncio.to_thread(_run)
        body, calls, tok = extract_agent_loop_output(ctx)
        ptok, ctok = _token_pair(tok)
    body = _strip_wrapping_fence((body or "").strip())
    created = datetime.now().strftime("%Y-%m-%d-%H-%M")
    markdown = wrap_plan(
        body,
        status="draft",
        target=parsed.target,
        branch=parsed.branch,
        created=created,
    )
    slug = parsed.branch or parsed.instruction[:40] or "plan"
    try:
        if existing_path and Path(existing_path).is_file():
            Path(existing_path).write_text(markdown, encoding="utf-8")
            path = Path(existing_path)
        else:
            path = write_plan_file(markdown, slug=slug, unique=True)
    except Exception:
        logger.exception("plan file write failed")
        path = None
        markdown = markdown + "\n\n(Could not write ~/agent-forge/plans/ — shown in chat only.)\n"
    chat = markdown
    if path:
        chat = markdown + f"\n\n---\nWrote plan to `{path}`\n"
    return chat, path, calls, ptok, ctok


_PLAN_PATH_IN_TEXT = re.compile(
    r"(?:Plan:|Wrote plan to)\s+`([^`]+)`",
    re.IGNORECASE,
)


def _message_meta(msg) -> dict:
    """plan_path lives in metadata_json. ORM ``.metadata`` is SQLAlchemy MetaData."""
    if isinstance(msg, dict):
        meta = msg.get("metadata") or {}
        return meta if isinstance(meta, dict) else {}
    raw = getattr(msg, "metadata_json", None)
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass
    meta = getattr(msg, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _plan_path_from_text(text: str) -> str:
    match = _PLAN_PATH_IN_TEXT.search(text or "")
    if not match:
        return ""
    path = match.group(1).strip()
    return path if path.endswith(".md") else ""


def plan_path_from_message_list(messages: list | None) -> str:
    """Last ``plan_path`` on a result, from ORM rows, dicts, or recap text."""
    for msg in reversed(messages or []):
        meta = _message_meta(msg)
        path = (meta.get("plan_path") or "").strip()
        if path:
            return path
        if isinstance(msg, dict):
            content = msg.get("content") or ""
        else:
            content = getattr(msg, "content", None) or ""
        path = _plan_path_from_text(content)
        if path:
            return path
    return ""


def _plan_path_from_messages(db: ChatDatabase, session_id: str) -> str:
    try:
        return plan_path_from_message_list(db.get_messages(session_id))
    except Exception:
        return ""


async def _execute_plan(
    *,
    plan_path: Path,
    rt: SearchRuntime,
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
    send_sync,
    cancel_event,
    overrides,
    secret_broker,
    db,
    session_id: str,
) -> tuple[str, list]:
    from agentforge.agent import AgentLoop
    from agentforge.client import AIClient

    raw = plan_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)
    target = meta.get("target") or ""
    findings = extract_section(body, "Findings")
    parsed_tasks = parse_tasks(body)
    tasks = merge_all_tasks(parsed_tasks) if len(parsed_tasks) <= 4 else merge_overlapping_tasks(parsed_tasks)
    worker_prompt = _load_prompt("worker.md")
    client = AIClient(profile="coder")
    bridge = AgentBridge(
        send_sync,
        broker,
        loop,
        secret_broker=secret_broker,
        db=db,
        session_id=session_id,
        incognito=(overrides or {}).get("incognito", False),
    )
    bridge.setup_registry(rt.registry)
    # Plan was already approved. Don't block each write_file on a second
    # confirm the UI often never shows (file.diff proposed, no confirm bar).
    from web.server.confirm import ConfirmDecision

    rt.registry.set_confirm_handler(lambda _prompt: ConfirmDecision(confirmed=True, auto_accepted=True))
    if hasattr(broker, "auto_accept"):
        broker.auto_accept = True

    receipts: dict[str, dict[str, str]] = {}
    inner_diff = rt.registry._on_file_diff

    def _capture_diff(payload: dict) -> None:
        if inner_diff is not None:
            inner_diff(payload)
        action = payload.get("action") or ""
        path = (payload.get("path") or "").strip()
        if action not in {"written", "edited"} or not path:
            return
        key = str(Path(path).expanduser())
        rec = receipts.get(key) or {"path": key, "pre_hash": payload.get("pre_hash") or ""}
        rec["post_hash"] = payload.get("post_hash") or rec.get("post_hash") or ""
        rec["diff_text"] = payload.get("diff_text") or rec.get("diff_text") or ""
        receipts[key] = rec

    rt.registry.set_file_diff_handler(_capture_diff)

    if not tasks:
        from agentforge.builder.store import PlanTask

        tasks = [PlanTask(id="T1", title="Execute the plan", files=[], body=body)]

    parallel = not tasks_share_files(tasks) and len(tasks) > 1
    workers = resolve_review_max_workers(4) if parallel else 1
    results: dict[str, str] = {}
    calls_out: list = []
    prompt_tokens = 0
    completion_tokens = 0

    def _run_task(task, prior: str = "") -> tuple[str, str, list]:
        abs_files: list[str] = []
        for rel in task.files:
            if not rel or rel.startswith("__"):
                continue
            p = Path(rel)
            abs_files.append(str(Path(target) / rel) if target and not p.is_absolute() else rel)
        verify = _is_verify_task(task)
        tools = list(_PLAN_TOOLS) if verify else list(_BUILD_TOOLS)
        system = f"{worker_prompt}\n\nRepository: `{target}`\n\n## {task.id} — {task.title}\n{task.body}\n"
        if findings:
            system += f"\n## Plan findings (already verified — do not re-investigate)\n{findings}\n"
        if prior:
            system += f"\n## Already completed\n{prior}\n"
        user = (
            f"Complete {task.id} in `{target}`.\n"
            f"Update these files in place (write_file unique=false): {', '.join(abs_files) or target}\n"
            "read_file the target once, then write. Do not spend the budget re-proving Findings.\n"
        )
        if verify:
            user += "This is a verify task. Do not write files.\n"
        agent = AgentLoop(
            client,
            rt.registry,
            system_prompt=system,
            tools=tools,
            max_iterations=20,
            verbose=False,
            cancel_event=cancel_event,
            deep_think=bool(client.profile.thinking_budget),
        )
        ctx = agent.run(user)
        text, calls, tok = extract_agent_loop_output(ctx)
        ptok, ctok = _token_pair(tok)
        return task.id, text, calls, ptok, ctok

    if send_sync:
        send_sync(
            {
                "type": "agent.iteration",
                "iteration": 0,
                "max_iterations": len(tasks),
                "detail": f"Build: {len(tasks)} task(s)",
                "elapsed": 0,
            }
        )

    if workers == 1:
        prior = ""
        for i, task in enumerate(tasks, start=1):
            if send_sync:
                send_sync(
                    {
                        "type": "agent.iteration",
                        "iteration": i,
                        "max_iterations": len(tasks),
                        "detail": f"{task.id} — {task.title}",
                        "elapsed": 0,
                    }
                )
            tid, text, calls, ptok, ctok = await asyncio.to_thread(_run_task, task, prior)
            results[tid] = text
            calls_out.extend(calls)
            prompt_tokens += ptok
            completion_tokens += ctok
            prior += f"\n### {tid}\n{text}\n"
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="build") as pool:
            futs = {pool.submit(_run_task, t, ""): t.id for t in tasks}
            for fut in as_completed(futs):
                try:
                    tid, text, calls, ptok, ctok = fut.result()
                    results[tid] = text
                    calls_out.extend(calls)
                    prompt_tokens += ptok
                    completion_tokens += ctok
                except Exception as exc:
                    results[futs[fut]] = f"Failed: {exc}"

    lines = ["# Build recap", "", f"Plan: `{plan_path}`", f"Target: `{target}`", ""]
    for task in tasks:
        lines.append(f"## {task.id} — {task.title}")
        lines.append(results.get(task.id, "(no output)"))
        lines.append("")
    recap = "\n".join(lines)
    try:
        plan_path.write_text(stamp_status(raw, "done"), encoding="utf-8")
    except OSError:
        logger.warning("could not stamp plan done: %s", plan_path)
    bundle = None
    try:
        bundle = save_apply_bundle(plan_path, receipts)
    except Exception:
        logger.exception("build apply bundle failed")
    if bundle:
        recap += (
            f"\n\n---\nRecorded {bundle} for undo/redo.\n"
            "Say **undo the build** to restore the pre-build files, or "
            "**apply the changes again** to put them back without re-planning.\n"
        )
    return recap, calls_out, prompt_tokens, completion_tokens
