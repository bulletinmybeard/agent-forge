"""REST API — session, message, and file upload endpoints for the AgentForge chat SPA.

Provides CRUD for chat sessions, read access to message history, and
multi-file upload for attaching files to queries.

Adapted from py-mini-ai-framework: profile endpoints now expose
agentforge's Ollama role/profile configuration instead of
py-mini-ai-framework's AIProfile objects.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import List

import ollama
from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from .database import ChatDatabase

logger = logging.getLogger(__name__)

# ── Command note title generation ─────────────────────────────────────────────

_TITLE_SYSTEM = """\
You generate short, descriptive titles for saved command notes.
Rules:
- Max 8 words
- Format: <verb> <subject> (<tool_name>)  — e.g., "Optimize test-image-1.png for web (image_optimize)"
- Use the actual filename or key value from the query, not the full path
- If multiple tools were used, pick the most meaningful one for the parenthetical
- Output the title only — no quotes, no explanation, nothing else
Examples:
  Query: "List all GitHub repos with visibility"  Tool: gh_command  → List GitHub repos by visibility (gh_command)
  Query: "Convert test-image-1.png to WebP"  Tool: image_convert  → Convert test-image-1.png to WebP (image_convert)
  Query: "Show recent git log with merges on master"  Tool: shell  → Show recent merge history on master (shell)
  Query: "Check running docker containers"  Tool: docker_ps  → List running Docker containers (docker_ps)\
"""


async def _generate_note_title(query: str, calls: list[dict]) -> str:
    """Call the cloud-light Ollama profile to generate a short descriptive title.

    Falls back to the raw query (truncated) on any error so saving never fails.
    """
    tool_names = ", ".join(dict.fromkeys(c.get("name", "") for c in calls if c.get("name")))
    user_msg = f"Query: {query.strip()}\nTool(s): {tool_names}"

    try:
        from app.config import settings

        role = settings.ollama.get_role("query_refinement")  # cloud-light profile
        client = ollama.AsyncClient(
            host=role.profile.host,
            headers=role.profile.headers,
        )
        resp = await client.chat(
            model=role.profile.model,
            messages=[
                {"role": "system", "content": _TITLE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            options={"num_predict": 32, "temperature": 0.2},
        )
        title = resp["message"]["content"].strip().strip('"').strip("'")
        # Safety: if the model returned multiple lines or something huge, take first line
        title = title.splitlines()[0].strip() if title else ""
        if title:
            return title
    except Exception as exc:
        logger.warning("Note title generation failed: %s", exc)

    # Fallback: truncate the raw query
    fallback = query.strip()
    return fallback[:120] if fallback else (tool_names or "Command note")


router = APIRouter(prefix="/api")

# Shared state — set at startup from app.py lifespan
_db: ChatDatabase | None = None
_upload_base: Path | None = None
_max_file_size: int = 50 * 1024 * 1024  # 50 MB default
_max_files: int = 25


def set_database(db: ChatDatabase) -> None:
    """Set the shared database reference (called from app.py lifespan)."""
    global _db
    _db = db


def set_upload_config(base_path: Path, max_size_mb: int = 50, max_files: int = 10) -> None:
    """Configure upload destination and limits (called from app.py lifespan)."""
    global _upload_base, _max_file_size, _max_files
    _upload_base = base_path
    _max_file_size = max_size_mb * 1024 * 1024
    _max_files = max_files
    _upload_base.mkdir(parents=True, exist_ok=True)
    logger.info("Upload directory: %s (max %d MB, max %d files)", _upload_base, max_size_mb, max_files)


def get_db() -> ChatDatabase:
    if _db is None:
        raise RuntimeError("Database not initialised")
    return _db


# -- Welcome message -----------------------------------------------------------

import re as _re
from datetime import datetime as _dt

_USER_CONTEXT_CACHE: dict[str, str] = {}  # key: "name" → first name

# Upload guards. session_id becomes a directory name, so constrain it (no
# traversal). Active-content extensions are blocked because uploads are served
# from the same origin (/uploads/...) and would otherwise enable stored XSS.
_SAFE_SESSION_RE = _re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_BLOCKED_UPLOAD_EXTS = frozenset(
    {".html", ".htm", ".xhtml", ".xht", ".shtml", ".svg", ".svgz", ".xml", ".js", ".mjs", ".swf", ".vbs"}
)


def _parse_user_name() -> str:
    """Extract the first name from user_context.md (cached)."""
    if "name" in _USER_CONTEXT_CACHE:
        return _USER_CONTEXT_CACHE["name"]

    name = ""
    try:
        # Try multiple known locations
        for candidate in [
            Path(__file__).resolve().parents[2] / "user_context.md",
            Path(__file__).resolve().parents[2] / "data" / "user_context.md",
        ]:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
                m = _re.search(r"^-\s*Name:\s*(.+)", text, _re.MULTILINE)
                if m:
                    full_name = m.group(1).strip()
                    name = full_name.split()[0] if full_name else ""
                break
    except Exception:
        pass

    _USER_CONTEXT_CACHE["name"] = name
    return name


def _time_greeting(now: _dt | None = None) -> str:
    """Return a time-of-day greeting phrase."""
    now = now or _dt.now()
    hour = now.hour
    if hour < 6:
        return "Burning the midnight oil"
    elif hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    elif hour < 21:
        return "Good evening"
    else:
        return "Good evening"


def _day_flavour(now: _dt | None = None) -> str:
    """Return a friendly day-aware quip (optional subtitle)."""
    now = now or _dt.now()
    weekday = now.strftime("%A")

    flavours = {
        "Monday": "Fresh week, fresh start.",
        "Tuesday": "Let's keep the momentum going.",
        "Wednesday": "Halfway through the week already.",
        "Thursday": "Almost there — one more push.",
        "Friday": "Happy Friday! Let's wrap things up.",
        "Saturday": "Weekend mode — working on something fun?",
        "Sunday": "Sunday session — nice and quiet.",
    }
    return flavours.get(weekday, "")


@router.get("/welcome")
async def get_welcome():
    """Return a personalized, time-aware welcome message."""
    now = _dt.now()
    name = _parse_user_name()
    greeting = _time_greeting(now)
    flavour = _day_flavour(now)

    if name:
        headline = f"{greeting}, {name}"
    else:
        headline = f"{greeting}"

    return {
        "headline": headline,
        "subtitle": flavour,
        "name": name,
        "time": now.strftime("%H:%M"),
        "day": now.strftime("%A"),
    }


# -- Request/Response models ---------------------------------------------------


class SessionUpdate(BaseModel):
    title: str


class CommandNoteCreate(BaseModel):
    session_id: str | None = None
    title: str
    commands: list[dict]
    message_ts: str | None = None


# -- Session endpoints ---------------------------------------------------------


@router.get("/sessions")
async def list_sessions(limit: int = 50, offset: int = 0, source: str = "web"):
    """List sessions ordered by most recently created.

    Each session dict includes ``has_active_job: bool`` so the frontend can
    disable new-chat navigation while a worker job is in flight.

    ``source`` filters by originating client (default ``"web"`` — the Agent Chat
    UI). External-app sessions (sysbar, ask-page, ...) are hidden unless asked
    for: ``?source=all`` lists every source, ``?source=sysbar`` a single one.
    """
    from .queue.store import job_store as _job_store

    sources = None if source == "all" else (source,)
    db = get_db()
    sessions = db.list_sessions(limit=limit, offset=offset, sources=sources)
    result = []
    for s in sessions:
        d = s.to_dict()
        d["has_active_job"] = _job_store.get_active_job(s.id) is not None
        result.append(d)
    return result


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a single session by ID."""
    db = get_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@router.get("/sessions/{session_id}/token-usage")
async def get_session_token_usage(session_id: str):
    """Get real token usage for a session (prompt, completion, total)."""
    db = get_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    sd = session.to_dict()
    return {
        "session_id": session_id,
        "prompt_tokens": sd.get("prompt_tokens", 0) or 0,
        "completion_tokens": sd.get("completion_tokens", 0) or 0,
        "total_tokens": sd.get("total_tokens", 0) or 0,
    }


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    limit: int = 0,
    before: int | None = None,
):
    """Get messages for a session, ordered by sequence.

    Without query params, returns ALL messages (backwards-compatible).
    With ``limit``, returns a paginated response::

        { "messages": [...], "has_more": true, "oldest_sequence": 5 }
    """
    db = get_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if limit > 0:
        messages, has_more = db.get_messages_page(
            session_id,
            limit=limit,
            before_sequence=before,
        )
        msg_dicts = [m.to_dict() for m in messages]
        oldest_seq = messages[0].sequence if messages else 0
        return {
            "messages": msg_dicts,
            "has_more": has_more,
            "oldest_sequence": oldest_seq,
        }

    # Legacy: return flat array of all messages
    messages = db.get_messages(session_id)
    return [m.to_dict() for m in messages]


@router.get("/sessions/{session_id}/messages/around")
async def get_messages_around(
    session_id: str,
    ts: int,
    window: int = 25,
):
    """Return *window* messages centred on the message nearest to *ts* (epoch ms).

    Used by the frontend to anchor-load history when the URL contains a
    ``#msg-{_ts}`` fragment.  Response shape::

        {
            "messages": [...],
            "has_more_before": true,
            "has_more_after": false,
            "oldest_sequence": 12,
            "newest_sequence": 36,
        }
    """
    db = get_db()
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages, has_more_before, has_more_after = db.get_messages_around(
        session_id,
        epoch_ms=ts,
        window=window,
    )
    msg_dicts = [m.to_dict() for m in messages]
    return {
        "messages": msg_dicts,
        "has_more_before": has_more_before,
        "has_more_after": has_more_after,
        "oldest_sequence": messages[0].sequence if messages else 0,
        "newest_sequence": messages[-1].sequence if messages else 0,
    }


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate):
    """Rename a session."""
    db = get_db()
    session = db.update_session(session_id, title=body.title)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and all its messages."""
    db = get_db()
    deleted = db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": True}


# -- Job status endpoint -------------------------------------------------------


@router.get("/sessions/{session_id}/job")
async def get_active_job(session_id: str):
    """Check if a session has an active (pending/running) worker job.

    Used by the frontend on reconnect to decide whether to resume polling.
    Returns the job info or 404 if no active job exists.
    """
    from .queue.store import job_store

    job = job_store.get_active_job(session_id)
    if not job:
        raise HTTPException(status_code=404, detail="No active job")
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "mode": job.mode,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
    }


# -- Internal endpoints (called by the native worker) --------------------
#
# The native worker process cannot safely share SQLite files with the Docker
# container (macOS/Linux file locking incompatibility across bind mounts).
# Instead the worker calls these lightweight HTTP endpoints so that agentforge-web
# — the sole owner of all SQLite files — handles every DB write.

internal = APIRouter(prefix="/internal")


class _EventBody(BaseModel):
    msg: dict
    role: str = "assistant"
    msg_type: str | None = None
    content: str | None = None
    tool_calls: list[dict] | None = None
    is_incognito: bool = False


class _StatusBody(BaseModel):
    status: str  # "running" | "done" | "error" | "cancelled"
    error: str | None = None


@internal.get("/sessions/{session_id}/history", status_code=200)
async def get_session_history(session_id: str):
    """Return raw conversation messages for the worker to build multi-turn context.

    Returns unprocessed query/result messages so the runner can apply its own
    sliding window via _build_conversation_history(). We do NOT call
    _build_conversation_history here -- that would double-process the history
    since the runner applies it again on the _NullDatabase stubs.
    """
    try:
        db = get_db()
        messages = db.get_messages(session_id)

        # Return raw role+content pairs for query and result messages.
        # The runner's _build_conversation_history will score, window, and
        # inject memory/facts on top of these.
        history = []
        for msg in messages:
            if msg.type in ("query", "result") and msg.content:
                history.append(
                    {
                        "role": "user" if msg.type == "query" else "assistant",
                        "content": msg.content,
                    }
                )
        return {"history": history}
    except Exception as exc:
        logger.warning("Failed to load history for session %s: %s", session_id, exc)
        return {"history": [], "error": str(exc)}


@internal.post("/sessions/{session_id}/update", status_code=200)
async def update_session_from_worker(session_id: str, body: dict = Body(...)):
    """Update session metadata (title, profile, model) from the worker.

    Called by _NullDatabase.update_session() in jobs_common.py so that auto-generated
    titles and profile overrides are persisted in agentforge-web's database.
    """
    db = get_db()
    if db.get_session(session_id):
        db.update_session(session_id, **body)
    return {"ok": True}


@internal.post("/sessions/{session_id}/token-usage", status_code=200)
async def add_token_usage_from_worker(session_id: str, body: dict = Body(...)):
    """Increment cumulative token counters for a session from the worker.

    Called by _NullDatabase.add_token_usage() in jobs_common.py so that Bedrock /
    Ollama token counts are persisted in agentforge-web's database even when the
    agent runs inside a worker process.
    """
    db = get_db()
    prompt_tokens = int(body.get("prompt_tokens", 0) or 0)
    completion_tokens = int(body.get("completion_tokens", 0) or 0)
    if prompt_tokens > 0 or completion_tokens > 0:
        db.add_token_usage(session_id, prompt_tokens, completion_tokens)
    return {"ok": True}


# Event types that should NOT be mirrored into the Redis replay buffer.
# These are point-in-time user interactions — replaying them on reload
# re-opens a modal the user already answered. The durable record of a
# resolved confirmation lives in SQLite via the runner's send_and_persist
# path (when that's wired); the ephemeral buffer must stay focused on
# visual state that survives a reload cleanly (tool.call panels, research
# progress, context.usage).
_BROADCAST_NO_REPLAY_TYPES = frozenset(
    {
        "confirm.request",
        "confirm.response",
        # Masked sudo-password prompt — interactive, must never replay on reload
        # (and the value must never hit the buffer).
        "secret.request",
        "secret.response",
    }
)


@internal.post("/sessions/{session_id}/broadcast", status_code=200)
async def broadcast_worker_event(session_id: str, body: dict = Body(...)):
    """Broadcast an ephemeral protocol event to any live browser WS — no DB persist.

    Used for transient UI-only events (tool.call, tool.calls.flush,
    research.progress, research.activity) during worker runs. These events drive
    the live ToolCallsPanel animation but are never stored in SQLite (the durable
    tool_calls record is written separately by send_and_persist).

    In addition to forwarding to the live WS, we mirror the event into a
    Redis-backed per-session buffer so that a page reload during or shortly
    after the run can replay the visual state via
    ``/internal/sessions/{id}/events/replay`` — no SQLite bloat, 1h TTL.

    Events in ``_BROADCAST_NO_REPLAY_TYPES`` are forwarded live but NOT
    mirrored to the buffer — they're interactions (confirm dialogs) that
    would re-pop on reload and confuse the user.
    """
    from . import state
    from .session_event_buffer import get_session_event_buffer

    ws = state.active_ws.get(session_id)
    if ws:
        try:
            await ws.send_json(body)
        except Exception:
            state.active_ws.pop(session_id, None)

    # Fire-and-forget buffer record — never blocks or fails the broadcast.
    # Skip the types that shouldn't replay (see comment above).
    if body.get("type") not in _BROADCAST_NO_REPLAY_TYPES:
        try:
            await get_session_event_buffer().record(session_id, body)
        except Exception:
            pass

    return {"ok": True}


@internal.post("/sessions/{session_id}/event", status_code=200)
async def push_worker_event(session_id: str, body: _EventBody):
    """Persist a protocol event from the worker and broadcast to any live WS.

    Called by HttpCallbackSocket in jobs_common.py for every send_and_persist call.
    """
    from . import state

    db = get_db()

    # Ensure the session row exists (worker may start before first WS connect)
    if not db.get_session(session_id):
        db.create_session(session_id)

    db.add_message(
        session_id=session_id,
        role=body.role,
        msg_type=body.msg_type or body.msg.get("type", "unknown"),
        content=body.content,
        metadata=body.msg,
        tool_calls=body.tool_calls,
        is_incognito=body.is_incognito,
    )

    # Broadcast to any connected browser client
    ws = state.active_ws.get(session_id)
    if ws:
        try:
            await ws.send_json(body.msg)
        except Exception:
            # Client disconnected — that's fine, message is already persisted
            state.active_ws.pop(session_id, None)

    return {"ok": True}


@internal.get("/sessions/{session_id}/confirm/{request_id}", status_code=200)
async def poll_confirm_response(session_id: str, request_id: str):
    """Poll for a confirmation response from the browser.

    Called repeatedly by HttpConfirmationBroker (jobs_common.py) while waiting for
    the user to click Confirm / Deny in the browser UI.

    Returns {"ready": False} while the user hasn't responded yet.
    Returns {"ready": True, "confirmed": bool, "auto_accept": bool} once they have.
    The response is consumed on first read (pop) so the worker doesn't see it twice.
    """
    from . import state

    key = f"{session_id}:{request_id}"
    response = state.confirm_responses.pop(key, None)
    if response is None:
        return {"ready": False}
    return {"ready": True, "confirmed": response["confirmed"], "auto_accept": response.get("auto_accept", False)}


@internal.get("/sessions/{session_id}/secret/{request_id}", status_code=200)
async def poll_secret_response(session_id: str, request_id: str):
    """Poll for a masked sudo-password response from the browser.

    Called repeatedly by WorkerSecretProvider (jobs_common.py) while a sudo
    command on the native local worker waits for the user to enter (or cancel)
    the password in the browser UI.

    Returns {"ready": False} until the user responds. Once they do, returns
    {"ready": True, "value": "..."} or {"ready": True, "cancelled": True}.
    Consumed on first read (pop) and never logged — the value is memory-only.
    """
    from . import state

    key = f"{session_id}:{request_id}"
    response = state.secret_responses.pop(key, None)
    if response is None:
        return {"ready": False}
    if response.get("cancelled"):
        return {"ready": True, "cancelled": True}
    return {"ready": True, "value": response.get("value")}


@internal.post("/sessions/{session_id}/messages/{msg_type}/metadata", status_code=200)
async def update_message_metadata(session_id: str, msg_type: str, body: dict = Body(...)):
    """Update metadata on a persisted message.

    Called by _NullDatabase.update_message_metadata() in jobs_common.py so
    the worker can persist research progress without touching SQLite.
    """
    db = get_db()
    ok = db.update_message_metadata(session_id, msg_type, body)
    return {"ok": ok}


@internal.post("/jobs/{job_id}/status", status_code=200)
async def update_job_status(job_id: str, body: _StatusBody):
    """Update job status from the worker (running / done / error)."""
    from .queue.models import JobStatus
    from .queue.store import job_store

    status_map = {
        "running": JobStatus.RUNNING,
        "done": JobStatus.DONE,
        "error": JobStatus.ERROR,
        "cancelled": JobStatus.CANCELLED,
    }
    status = status_map.get(body.status)
    if not status:
        raise HTTPException(status_code=400, detail=f"Unknown status: {body.status}")

    job_store.update_status(job_id, status, error=body.error)
    return {"ok": True}


@internal.get("/jobs/{job_id}/cancelled", status_code=200)
async def check_job_cancelled(job_id: str):
    """Return whether this job has been cancelled.  Polled by the worker.

    Also returns ``done=true`` when the job is in any terminal state
    (DONE, ERROR, CANCELLED) so the worker can skip duplicate execution.
    """
    from .queue.models import JobStatus
    from .queue.store import job_store

    _TERMINAL = {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}
    job = job_store.get_job(job_id)
    cancelled = job is not None and job.status == JobStatus.CANCELLED
    done = job is not None and job.status in _TERMINAL
    return {"cancelled": cancelled, "done": done}


@internal.post("/scheduler/runs/{run_id}/complete", status_code=200)
async def complete_scheduled_run(run_id: str, body: dict):
    """Called by the worker to report a scheduled command's result."""
    db = get_db()
    db.complete_scheduled_job_run(
        run_id=run_id,
        status=body.get("status", "error"),
        exit_code=body.get("exit_code", -1),
        output=body.get("output"),
        error=body.get("error"),
        duration_s=body.get("duration_s", 0),
    )
    return {"ok": True}


# -- Internal monitor endpoints (called by the worker) -------------------------


@internal.post("/monitor/checks/{check_id}/complete", status_code=200)
async def complete_monitor_check(check_id: str, body: dict):
    """Called by the worker to report a monitor check result."""
    db = get_db()
    db.complete_monitor_check(
        check_id=int(check_id),
        status=body.get("status", "error"),
        prev_hash=body.get("prev_hash"),
        current_hash=body.get("current_hash"),
        diff_summary=body.get("diff_summary"),
        diff_lines_added=body.get("diff_lines_added"),
        diff_lines_removed=body.get("diff_lines_removed"),
        structured_diff=body.get("structured_diff"),
        error=body.get("error"),
        duration_s=body.get("duration_s", 0),
        screenshot_path=body.get("screenshot_path"),
    )
    return {"ok": True}


@internal.get("/monitor/jobs/{job_id}/latest-snapshot", status_code=200)
async def get_latest_monitor_snapshot(job_id: str):
    """Called by the worker to get the latest snapshot for comparison."""
    db = get_db()
    snap = db.get_latest_snapshot(job_id)
    if not snap:
        return {"content": None, "content_hash": None}
    return {
        "id": snap.id,
        "content": snap.content,
        "content_hash": snap.content_hash,
        "structured_content": snap.structured_content,
        "extraction_mode": snap.extraction_mode,
        "word_count": snap.word_count,
    }


@internal.post("/monitor/jobs/{job_id}/snapshots", status_code=201)
async def create_monitor_snapshot(job_id: str, body: dict):
    """Called by the worker to store a new snapshot."""
    db = get_db()
    snap = db.create_monitor_snapshot(
        job_id=job_id,
        content=body["content"],
        content_hash=body["content_hash"],
        extraction_mode=body.get("extraction_mode", "text"),
        css_selector_used=body.get("css_selector_used"),
        word_count=body.get("word_count"),
        structured_content=body.get("structured_content"),
    )
    # Prune old snapshots
    db.delete_old_snapshots(job_id, keep_count=10)
    return {"id": snap.id}


# -- Command Note endpoints ----------------------------------------------------


@router.get("/commands")
async def list_command_notes(limit: int = 200, offset: int = 0):
    """List all saved command notes, newest first."""
    db = get_db()
    notes = db.list_command_notes(limit=limit, offset=offset)
    return [n.to_dict() for n in notes]


@router.post("/commands", status_code=201)
async def create_command_note(body: CommandNoteCreate):
    """Save a new command note. Title is generated via LLM if a raw query is provided."""
    db = get_db()
    # Generate a descriptive title from the user's query + tool calls
    title = await _generate_note_title(body.title or "", body.commands)
    note = db.create_command_note(
        title=title,
        commands=body.commands,
        session_id=body.session_id,
        message_ts=body.message_ts,
    )
    return note.to_dict()


@router.get("/commands/session/{session_id}")
async def get_session_command_notes(session_id: str):
    """Get all command notes for a specific session (for add/remove toggle state)."""
    db = get_db()
    notes = db.get_session_command_notes(session_id)
    return [n.to_dict() for n in notes]


@router.delete("/commands/{note_id}")
async def delete_command_note(note_id: int):
    """Delete a saved command note."""
    db = get_db()
    deleted = db.delete_command_note(note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Command note not found")
    return {"deleted": True}


# -- Session Instructions endpoint --------------------------------------------


@router.get("/sessions/{session_id}/instructions")
async def list_session_instructions(session_id: str):
    """List all active instructions for a session (session-scoped + global)."""
    db = get_db()
    instrs = db.get_session_instructions(session_id)
    return [i.to_dict() for i in instrs]


@router.delete("/instructions/{instruction_id}")
async def delete_instruction(instruction_id: int):
    """Delete a single instruction by ID."""
    db = get_db()
    deleted = db.delete_session_instruction(instruction_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Instruction not found")
    return {"deleted": True}


@router.delete("/sessions/{session_id}/instructions")
async def clear_instructions(session_id: str, global_too: bool = False):
    """Clear all instructions for a session. Pass global_too=true to also remove globals."""
    db = get_db()
    count = db.clear_session_instructions(session_id, global_too=global_too)
    return {"cleared": count}


# -- Agents endpoint -----------------------------------------------------------


@router.get("/agents")
async def list_agents():
    """List all available agents — built-in modes and user-defined custom agents.

    Used by the Help modal to render an up-to-date agents/modes reference.
    Built-in modes are returned in canonical order; custom agents follow.
    """
    from .ws_endpoint import get_runtime

    # Aliases must match the _*_ALIASES sets in ws_endpoint.py — any alias
    # listed here that isn't in the matching set will silently not work.
    built_in = [
        {
            "id": "search",
            "type": "built-in",
            "aliases": ["@docs"],
            "description": "Local RAG search over your indexed knowledge base (Qdrant)",
            "profile": "cloud-light",
        },
        {
            "id": "web_search",
            "type": "built-in",
            "aliases": ["@search"],
            "description": "Internet search — web pages, documentation, media lookups",
            "profile": "cloud-heavy",
        },
        {
            "id": "agent",
            "type": "built-in",
            "aliases": ["@agent"],
            "description": "General-purpose agent with filesystem, Docker, SSH and more",
            "profile": "cloud-heavy",
        },
        {
            "id": "logs",
            "type": "built-in",
            "aliases": ["@logs"],
            "description": "Log analysis — diagnoses errors, explains messages, proposes fixes",
            "profile": "log-analyzer",
        },
        {
            "id": "discover",
            "type": "built-in",
            "aliases": ["@discover"],
            "description": "System discovery — scope, investigate, and create a cleanup plan",
            "profile": "cloud-heavy",
        },
        {
            "id": "sql",
            "type": "built-in",
            "aliases": ["@sql"],
            "description": "Natural-language SQL generation against indexed schemas",
            "profile": "cloud-heavy",
        },
        {
            "id": "pipeline",
            "type": "built-in",
            "aliases": ["@pipeline"],
            "description": "Structured multi-step workflow with typed tools (read_file, execute_sql, save/load_result, git, etc.) — no shell",
            "profile": "thinker",
        },
        {
            "id": "scheduler",
            "type": "built-in",
            "aliases": ["@scheduler"],
            "description": "Schedule recurring tasks — health checks, backups, data pulls",
            "profile": "cloud-light",
        },
        {
            "id": "monitor",
            "type": "built-in",
            "aliases": ["@monitor"],
            "description": "Website change monitoring — detect content changes and send notifications",
            "profile": "cloud-light",
        },
        {
            "id": "review",
            "type": "built-in",
            "aliases": ["@review"],
            "description": "Parallel code review — 4 specialist sub-agents (error handling, type design, test coverage, code quality)",
            "profile": "cloud-heavy",
        },
        {
            "id": "research",
            "type": "built-in",
            "aliases": ["@research"],
            "description": "Parallel multi-agent web research — planner decomposes query, sub-agents run in parallel, findings merged into a report",
            "profile": "cloud-heavy",
        },
        {
            "id": "chat",
            "type": "built-in",
            "aliases": ["@chat"],
            "description": "Direct LLM conversation — no tools, no Qdrant (default fallback)",
            "profile": "cloud-light",
        },
    ]

    custom: list[dict] = []
    try:
        rt = get_runtime()
        for agent in rt.list_custom_agents():
            custom.append(
                {
                    "id": agent["id"],
                    "type": "custom",
                    "aliases": agent.get("aliases", []),
                    "description": agent.get("description", agent["id"]),
                    "profile": agent.get("profile", "cloud-heavy"),
                    "tools": agent.get("tools", []),
                    "max_iterations": agent.get("max_iterations", 10),
                }
            )
    except RuntimeError:
        # Runtime not yet initialised (server still starting up)
        pass

    return {"agents": built_in + custom}


# -- Profile endpoints ---------------------------------------------------------


@router.get("/profiles")
async def get_profiles(include_abstract: bool = False):
    """Return the list of AI profiles for the UI.

    Profiles live in ``framework-config.yaml`` under ``ai.profiles``. Default
    behaviour excludes abstract model profiles (``abstract: true``) so the
    main UI dropdown only surfaces role profiles (``agent``, ``fast``,
    ``cloud-light``, …). Pass ``?include_abstract=true`` to include abstract
    profiles too — used by the multi-provider prompt lab which wants to pick
    individual ``bedrock-claude-*`` / ``deepinfra-*`` / ``openrouter-*`` abstracts
    directly.
    """
    from app.config import settings as af_settings

    # Resolved profile info (model + temp + max_tokens + provider + abstract).
    profiles = af_settings.ollama.list_selectable_profiles(include_abstract=include_abstract)

    # Build the role → profile-name map from config.yaml's ollama.model_roles so
    # the UI can surface the per-pipeline-step assignments.
    import yaml

    config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    roles: dict[str, str] = {}
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        yaml_roles = cfg.get("ollama", {}).get("model_roles", {})
        for role_name, role_val in yaml_roles.items():
            if isinstance(role_val, dict):
                roles[role_name] = role_val.get("profile", "")
            elif isinstance(role_val, str):
                roles[role_name] = role_val

    default_profile = roles.get("answer_generation", "cloud-heavy")

    return {
        "default_profile": default_profile,
        "profiles": profiles,
        "roles": roles,  # extra info for the UI
    }


@router.get("/providers")
async def get_providers():
    """Return the set of AI providers the user can pick from for a new chat.

    - ``available`` — every provider declared anywhere in ``framework-config.yaml``
      (the union of ``declared_provider`` across all profiles).
    - ``configured`` — providers that have an active submap in
      ``ai.provider_override_map``, i.e. switching to them actually rewrites
      role profiles. Ollama is always included as the source.
    - ``default`` — the singleton's resolved provider override
      (``AGENTFORGE_PROVIDER`` env var or ``ai.provider_override`` YAML key), or
      ``None`` if no global override is active. The frontend uses this as
      the placeholder for the "use server default" choice.

    Degrades gracefully when the framework ``ConfigManager`` can't init (e.g.,
    a dangling profile reference in YAML): returns Ollama-only so the UI keeps
    working. Mirrors the try/except pattern in ``app/config.py``'s
    ``list_selectable_profiles``.
    """
    from agentforge.config import get_config as _get_fw_config
    from app.config import settings as af_settings

    # Profile listing already swallows ConfigManager init failures and returns
    # a usable shape via the legacy local merger — safe to call directly.
    profiles = af_settings.ollama.list_selectable_profiles(include_abstract=True)
    available = sorted({(info.get("declared_provider") or "ollama").lower() for info in profiles.values()})

    # Group abstract profile name -> model string by declared provider.
    # Used by the frontend ModelSelector to filter its dropdown to models
    # that match the currently selected provider.
    models_by_provider: dict[str, dict[str, str]] = {}
    for name, info in profiles.items():
        if not info.get("abstract"):
            continue
        prov = (info.get("declared_provider") or "ollama").lower()
        model = info.get("model")
        if not model:
            continue
        models_by_provider.setdefault(prov, {})[name] = model

    try:
        fw = _get_fw_config()
    except Exception as exc:  # noqa: BLE001 — degraded mode covers all init errors
        logger.warning(
            "/api/providers: ConfigManager unavailable (%s); returning ollama-only",
            exc,
        )
        return {
            "available": available or ["ollama"],
            "configured": ["ollama"],
            "models": models_by_provider,
            "default": None,
        }

    configured: list[str] = []
    for prov in available:
        if prov == "ollama":
            configured.append(prov)
            continue
        # A provider is "configured" if the YAML map can route at least one
        # profile to it. This implicitly checks that the matching abstract
        # profiles + credentials exist (otherwise the map entries would have
        # been dropped at startup).
        if fw._compute_active_map(prov):
            configured.append(prov)

    return {
        "available": available,
        "configured": configured,
        "models": models_by_provider,
        "default": fw._provider_override,
    }


# -- Multi-provider prompt lab -------------------------------------------------

# Shared PromptLabDatabase instance — populated by app.py lifespan via
# init_prompt_lab_db(). When None (e.g., DB failed to init), the /run
# endpoint still works but runs aren't persisted and /runs returns [].
_prompt_lab_db = None


def init_prompt_lab_db(db) -> None:
    """Store the shared PromptLabDatabase handle. Called from app.py startup."""
    global _prompt_lab_db
    _prompt_lab_db = db


class PromptLabRequest(BaseModel):
    """Request body for ``POST /api/prompt-lab/run``."""

    prompt: str
    system: str | None = None
    profiles: list[str]
    # Optional vision input. Each entry is base64 image data (no
    # `data:image/...;base64,` prefix). Framework backends translate to
    # provider format: Bedrock + Ollama support inline images; OpenAI-
    # compat strips with a warning.
    images: list[str] | None = None


class PromptLabResult(BaseModel):
    """Single-profile outcome from a prompt-lab run."""

    profile: str
    provider: str = ""
    model: str = ""
    content: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None


class PromptLabResponse(BaseModel):
    """Response body for ``POST /api/prompt-lab/run``."""

    run_id: str | None = None  # null only when DB persistence fails
    total_latency_ms: int
    created_at: str | None = None
    results: list[PromptLabResult]
    # Set only when prompt refinement actually rewrote the prompt (else null).
    original_prompt: str | None = None
    refined_prompt: str | None = None


@router.post("/prompt-lab/run", response_model=PromptLabResponse)
async def run_prompt_lab(req: PromptLabRequest) -> PromptLabResponse:
    """Run the same prompt across multiple AI profiles in parallel.

    Each profile is resolved fresh via :class:`agentforge.client.AIClient` so
    ``ai.provider_override`` and per-profile credentials are honoured. Errors
    are isolated per profile — a failing profile returns a result with the
    ``error`` field set while the others complete normally.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if not req.profiles:
        raise HTTPException(status_code=400, detail="at least one profile is required")

    from agentforge.client import AIClient

    from .prompt_refiner import refine_prompt

    # Refine the opening prompt once (when enabled). The same refined text feeds
    # every profile. No-op + returns the original when refinement is off.
    refine = await refine_prompt(req.prompt)
    effective_prompt = refine.refined

    messages: list[dict] = []
    if req.system and req.system.strip():
        messages.append({"role": "system", "content": req.system.strip()})
    user_msg: dict = {"role": "user", "content": effective_prompt}
    # Attach images on the user message if provided. Framework backends
    # accept either raw bytes or base64 strings — we always pass base64
    # since the wire format from clients is JSON.
    if req.images:
        user_msg["images"] = req.images
    messages.append(user_msg)

    async def _run_one(name: str) -> PromptLabResult:
        start = time.perf_counter()
        try:
            client = AIClient(profile=name)
            resp = await client.achat(messages, stream=False)
            return PromptLabResult(
                profile=name,
                provider=(client.profile.provider or "").lower(),
                model=client.profile.model or "",
                content=resp.content or "",
                latency_ms=int((time.perf_counter() - start) * 1000),
                prompt_tokens=int(resp.prompt_tokens or 0),
                completion_tokens=int(resp.completion_tokens or 0),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — surface per-profile errors
            logger.warning("prompt-lab profile %s failed: %s", name, exc)
            return PromptLabResult(
                profile=name,
                latency_ms=int((time.perf_counter() - start) * 1000),
                error=str(exc),
            )

    total_start = time.perf_counter()
    results = await asyncio.gather(*[_run_one(n) for n in req.profiles])
    total_latency_ms = int((time.perf_counter() - total_start) * 1000)

    # Persist — failures here are logged but not fatal; the caller still
    # gets the live results so the UI always renders.
    run_id: str | None = None
    created_at: str | None = None
    if _prompt_lab_db is not None:
        try:
            run = _prompt_lab_db.save_run(
                system=req.system,
                prompt=req.prompt,
                total_latency_ms=total_latency_ms,
                results=[r.model_dump() for r in results],
            )
            run_id = run.id
            created_at = run.created_at.isoformat() if run.created_at else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("prompt-lab save_run failed: %s", exc)

    return PromptLabResponse(
        run_id=run_id,
        total_latency_ms=total_latency_ms,
        created_at=created_at,
        results=list(results),
        original_prompt=refine.original if refine.changed else None,
        refined_prompt=refine.refined if refine.changed else None,
    )


@router.get("/prompt-lab/runs")
async def list_prompt_lab_runs(limit: int = 20):
    """Recent-runs list for the history dropdown.

    Each entry has id, created_at, prompt (truncated), profile_count,
    total_latency_ms. Full results are not included — fetch via
    ``GET /api/prompt-lab/runs/{id}`` when needed.
    """
    if _prompt_lab_db is None:
        return {"runs": []}
    runs = _prompt_lab_db.list_runs(limit=limit)
    entries = []
    for r in runs:
        data = r.to_dict(include_results=False)
        # Truncate the prompt for the dropdown — full text is available via /runs/{id}.
        if data.get("prompt") and len(data["prompt"]) > 140:
            data["prompt"] = data["prompt"][:140] + "…"
        entries.append(data)
    return {"runs": entries}


@router.get("/prompt-lab/runs/{run_id}")
async def get_prompt_lab_run(run_id: str):
    """Fetch a single persisted run + all its per-profile results."""
    if _prompt_lab_db is None:
        raise HTTPException(status_code=503, detail="prompt_lab DB unavailable")
    run = _prompt_lab_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"No prompt-lab run with id {run_id}")
    return run.to_dict()


# -- Knowledge metadata endpoint -----------------------------------------------


@router.get("/knowledge")
async def get_knowledge():
    """Return indexed knowledge sources and document count.

    Proxies agentforge's /indexer/sources and /indexer/documents so the
    React frontend can display "Known sources: ..." and "Known documents: N"
    without needing a separate connection to the main service.
    """
    try:
        from app.config import settings as af_settings
        from app.services.indexer_service import indexer_service
    except Exception as exc:
        logger.warning("Could not import indexer_service: %s", exc)
        return {
            "sources": [],
            "source_count": 0,
            "documents": [],
            "document_count": 0,
            "stoplist": [],
            "error": str(exc),
        }

    try:
        sources = indexer_service.discover_sources()
    except Exception as exc:
        logger.warning("discover_sources failed: %s", exc)
        sources = []

    try:
        documents = indexer_service.discover_documents()
    except Exception as exc:
        logger.warning("discover_documents failed: %s", exc)
        documents = []

    unique_doc_names = sorted({d.get("document_name", "") for d in documents if d.get("document_name")})

    # Build the name→info lookup the same way agentforge_chat.py does
    source_list = []
    for src in sources:
        name = src.get("source_name", src.get("api_name", ""))
        if name:
            source_list.append(
                {
                    "source_name": name,
                    "source_type": src.get("source_type", ""),
                    "chunk_count": src.get("chunk_count", 0),
                }
            )

    stoplist = []
    try:
        if hasattr(af_settings.chunking, "document_lookup_stoplist"):
            stoplist = af_settings.chunking.document_lookup_stoplist
    except Exception:
        pass

    return {
        "sources": source_list,
        "source_count": len(source_list),
        "documents": unique_doc_names,
        "document_count": len(unique_doc_names),
        "stoplist": stoplist,
    }


# -- Upload limits endpoint ----------------------------------------------------


@router.get("/upload-limits")
async def upload_limits():
    """Return file upload constraints and model context info for client-side budget checks."""
    from .ws_endpoint import _DEFAULT_CONTEXT_SIZE, _MODEL_CONTEXT_SIZES

    return {
        "max_file_size_bytes": _max_file_size,
        "max_file_size_mb": _max_file_size // (1024 * 1024),
        "max_files_per_request": _max_files,
        "default_context_tokens": _DEFAULT_CONTEXT_SIZE,
        "model_context_sizes": _MODEL_CONTEXT_SIZES,
    }


# -- File upload endpoint ------------------------------------------------------


@router.post("/upload/{session_id}")
async def upload_files(
    session_id: str,
    files: List[UploadFile] = File(...),
):
    """Upload one or more files for a chat session.

    Files are saved to ``data/uploads/{session_id}/`` and the response
    returns the list of saved file paths (absolute) so the search pipeline
    can reference them.
    """
    if _upload_base is None:
        raise HTTPException(status_code=500, detail="Upload not configured")

    if not _SAFE_SESSION_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")

    if len(files) > _max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files — maximum {_max_files} per request",
        )

    session_dir = _upload_base / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []

    for upload in files:
        # Read in chunks and abort as soon as the cap is exceeded, so a multi-GB
        # body can't exhaust memory before the size check (the old `await
        # upload.read()` buffered the whole body first).
        buf = bytearray()
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _max_file_size:
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{upload.filename}' exceeds {_max_file_size // (1024 * 1024)} MB limit",
                )
        content = bytes(buf)

        safe_name = Path(upload.filename).name
        if not safe_name:
            safe_name = "unnamed_file"

        if Path(safe_name).suffix.lower() in _BLOCKED_UPLOAD_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"File type not allowed: '{safe_name}' (active-content types are blocked)",
            )

        dest = session_dir / safe_name
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 1
            while dest.exists():
                dest = session_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        dest.write_bytes(content)

        file_info: dict = {
            "name": safe_name,
            "path": str(dest.resolve()),
            "size": len(content),
            "content_type": upload.content_type or "application/octet-stream",
        }

        # PDF pre-extraction
        if dest.suffix.lower() == ".pdf":
            extracted_path = _extract_pdf(dest)
            if extracted_path:
                file_info["extracted_path"] = str(extracted_path.resolve())
                logger.info("PDF extracted → %s", extracted_path)

        # Image tagging
        ct = (upload.content_type or "").lower()
        if ct.startswith("image/") or dest.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
        }:
            file_info["is_image"] = True
            file_info["url"] = f"/uploads/{session_id}/{dest.name}"

        saved.append(file_info)
        logger.info("Saved upload: %s (%d bytes) → %s", safe_name, len(content), dest)

    return {"files": saved}


# -- Audit log endpoints -------------------------------------------------------


@router.get("/audit/tools")
async def audit_tool_executions(
    session_id: str | None = None,
    tool_name: str | None = None,
    since_minutes: int | None = None,
    count: int = 100,
):
    """Query the tool execution audit log (Redis Streams)."""
    from .audit_log import get_audit_log

    audit = get_audit_log()
    if not audit:
        raise HTTPException(status_code=503, detail="Audit log not available")
    since_ms = None
    if since_minutes:
        since_ms = str(int((time.time() - since_minutes * 60) * 1000))
    entries = await audit.query_tool_executions(
        session_id=session_id,
        tool_name=tool_name,
        since_ms=since_ms,
        count=count,
    )
    return {"entries": entries, "count": len(entries)}


@router.get("/audit/runs")
async def audit_agent_runs(
    session_id: str | None = None,
    since_minutes: int | None = None,
    count: int = 100,
):
    """Query the agent run audit log (Redis Streams)."""
    from .audit_log import get_audit_log

    audit = get_audit_log()
    if not audit:
        raise HTTPException(status_code=503, detail="Audit log not available")
    since_ms = None
    if since_minutes:
        since_ms = str(int((time.time() - since_minutes * 60) * 1000))
    entries = await audit.query_agent_runs(
        session_id=session_id,
        since_ms=since_ms,
        count=count,
    )
    return {"entries": entries, "count": len(entries)}


@router.get("/audit/stats")
async def audit_stats(since_minutes: int | None = None):
    """Aggregated audit stats: total calls, error rate, top tools."""
    from .audit_log import get_audit_log

    audit = get_audit_log()
    if not audit:
        raise HTTPException(status_code=503, detail="Audit log not available")
    since_ms = None
    if since_minutes:
        since_ms = str(int((time.time() - since_minutes * 60) * 1000))
    return await audit.stats(since_ms=since_ms)


# -- Result store endpoints ----------------------------------------------------


@router.get("/results/{session_id}")
async def list_session_results(session_id: str):
    """List all cached result labels for a session (metadata only, no data)."""
    from .result_store import get_result_store

    store = get_result_store()
    if not store:
        raise HTTPException(status_code=503, detail="Result store not available")
    return {"session_id": session_id, "results": store.get_summary(session_id)}


@router.get("/results/{session_id}/{label}")
async def get_session_result(session_id: str, label: str):
    """Retrieve a specific cached result by label."""
    from .result_store import get_result_store

    store = get_result_store()
    if not store:
        raise HTTPException(status_code=503, detail="Result store not available")
    result = store.get(session_id, label)
    if not result:
        raise HTTPException(status_code=404, detail=f"No result with label '{label}'")
    return result


@router.delete("/results/{session_id}")
async def clear_session_results(session_id: str):
    """Clear all cached results for a session."""
    from .result_store import get_result_store

    store = get_result_store()
    if not store:
        raise HTTPException(status_code=503, detail="Result store not available")
    count = store.clear_session(session_id)
    return {"session_id": session_id, "deleted": count}


# -- Schema cache endpoints (global / cross-session) --------------------------


@router.get("/schemas")
async def list_cached_schemas():
    """List all database schemas currently cached in Redis.

    These are global (not session-scoped) — any chat session can use them.
    """
    from agentforge.tools.sql_schema_tool import get_all_cached_schemas

    schemas = get_all_cached_schemas()
    summary = []
    for db_name, schema in schemas.items():
        summary.append(
            {
                "database": db_name,
                "dialect": schema.get("dialect", "unknown"),
                "table_count": schema.get("table_count", 0),
                "total_columns": schema.get("total_columns", 0),
                "view_count": len(schema.get("views", [])),
            }
        )
    return {"cached_schemas": summary}


@router.get("/schemas/{database}")
async def get_cached_schema(database: str, format: str = "json"):
    """Retrieve a cached database schema."""
    from agentforge.tools.sql_schema_tool import _compact_schema
    from agentforge.tools.sql_schema_tool import get_cached_schema as _get

    schema = _get(database)
    if not schema:
        raise HTTPException(status_code=404, detail=f"No cached schema for '{database}'")
    if format == "compact":
        return {"database": database, "compact": _compact_schema(schema)}
    return {"database": database, "schema": schema}


@router.delete("/schemas/{database}")
async def invalidate_cached_schema(database: str):
    """Invalidate (delete) the cached schema for a database."""
    from agentforge.tools.sql_schema_tool import _cache_key, _get_redis

    r = _get_redis()
    if not r:
        raise HTTPException(status_code=503, detail="Redis not available")
    deleted = r.delete(_cache_key(database))
    return {"database": database, "deleted": bool(deleted)}


# -- Location endpoint ---------------------------------------------------------


@router.get("/location")
async def get_location(request: Request, lat: float | None = None, lon: float | None = None):
    """Resolve the user's location for context injection.

    Two modes:
    - GPS:  ``?lat=X&lon=Y`` — Nominatim reverse geocoding + offline timezone
    - IP:   no params       — DbIP-City-lite .mmdb lookup from client IP

    Returns: {city, country, timezone, lat, lon, local_time, source}
    """
    from .location_service import resolve_from_coords, resolve_from_ip

    if lat is not None and lon is not None:
        # GPS mode — caller provides coordinates from browser geolocation API
        result = await resolve_from_coords(lat, lon)
        if result:
            return result
        raise HTTPException(status_code=503, detail="Reverse geocoding unavailable")

    # IP fallback — extract client IP from request.
    # X-Forwarded-For is client-spoofable; this is used only for best-effort
    # geolocation context (not auth/authz), so trusting the proxy header here is
    # acceptable. Do NOT reuse this value for any security decision.
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else None)
        or "127.0.0.1"
    )
    # Strip IPv6-mapped IPv4 prefix (::ffff:1.2.3.4 → 1.2.3.4)
    if client_ip.startswith("::ffff:"):
        client_ip = client_ip[7:]

    result = resolve_from_ip(client_ip)
    if result:
        return result

    raise HTTPException(
        status_code=503,
        detail="IP location unavailable — DbIP database not installed. Run scripts/download_dbip.sh",
    )


# ---------------------------------------------------------------------------
# Prompt presets
# ---------------------------------------------------------------------------

_PRESETS_PATH = Path(__file__).parent.parent.parent / "data" / "prompt_presets.yaml"


@router.get("/presets")
async def get_presets():
    """Return prompt presets from data/prompt_presets.yaml.

    Reads the file on every request so edits take effect immediately
    without restarting the server.

    Returns a list of objects: [{name, message, mode?}]
    Returns [] if the file does not exist or cannot be parsed.
    """
    if not _PRESETS_PATH.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(_PRESETS_PATH.read_text()) or {}
        presets = data.get("presets", [])
        # Normalise: ensure each entry has at least name + message
        return [p for p in presets if isinstance(p, dict) and p.get("name") and p.get("message")]
    except Exception as exc:
        logger.warning("Failed to load prompt_presets.yaml: %s", exc)
        return []


# -- Scheduler endpoints -------------------------------------------------------


class SchedulerJobCreate(BaseModel):
    label: str
    command: str
    cron: str
    cron_human: str | None = None
    on_failure: str = "notify"
    enabled: bool = True


class SchedulerJobUpdate(BaseModel):
    label: str | None = None
    command: str | None = None
    cron: str | None = None
    cron_human: str | None = None
    on_failure: str | None = None
    enabled: bool | None = None


@router.get("/scheduler/jobs")
async def list_scheduler_jobs():
    """List all scheduled jobs."""
    from .scheduler_service import get_scheduler_service

    svc = get_scheduler_service()
    return {"jobs": svc.list_jobs()}


@router.get("/scheduler/jobs/{job_id}")
async def get_scheduler_job(job_id: str):
    """Get a single scheduled job."""
    from .scheduler_service import get_scheduler_service

    svc = get_scheduler_service()
    job = svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    return job


@router.post("/scheduler/jobs", status_code=201)
async def create_scheduler_job(body: SchedulerJobCreate):
    """Create a new scheduled job."""
    from .scheduler_service import get_scheduler_service

    svc = get_scheduler_service()

    # Vet the command through the safety guard
    guard_result = svc.vet_command(body.command)
    if not guard_result["safe"]:
        raise HTTPException(
            status_code=400,
            detail=f"Command rejected by safety guard (verdict: {guard_result['verdict']})",
        )

    try:
        job = svc.create_job(
            label=body.label,
            command=body.command,
            cron=body.cron,
            cron_human=body.cron_human,
            on_failure=body.on_failure,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return job


@router.put("/scheduler/jobs/{job_id}")
async def update_scheduler_job(job_id: str, body: SchedulerJobUpdate):
    """Update a scheduled job."""
    from .scheduler_service import get_scheduler_service

    svc = get_scheduler_service()
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # If command is being changed, vet it
    if "command" in fields:
        guard_result = svc.vet_command(fields["command"])
        if not guard_result["safe"]:
            raise HTTPException(
                status_code=400,
                detail=f"Command rejected by safety guard (verdict: {guard_result['verdict']})",
            )

    try:
        job = svc.update_job(job_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    return job


@router.delete("/scheduler/jobs/{job_id}")
async def delete_scheduler_job(job_id: str):
    """Delete a scheduled job."""
    from .scheduler_service import get_scheduler_service

    svc = get_scheduler_service()
    deleted = svc.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    return {"deleted": True}


@router.get("/scheduler/jobs/{job_id}/runs")
async def get_scheduler_job_runs(job_id: str, limit: int = 20):
    """Get recent runs for a scheduled job."""
    from .scheduler_service import get_scheduler_service

    svc = get_scheduler_service()
    job = svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    runs = svc.get_job_runs(job_id, limit=limit)
    return {"runs": runs}


# -- Monitor endpoints ---------------------------------------------------------


class MonitorJobCreate(BaseModel):
    label: str
    url: str
    original_prompt: str | None = None
    extraction_mode: str = "text"
    css_selector: str | None = None
    cron: str
    cron_human: str | None = None
    notification_method: str = "terminal-notifier"
    webhook_url: str | None = None
    enabled: bool = True


class MonitorJobUpdate(BaseModel):
    label: str | None = None
    url: str | None = None
    extraction_mode: str | None = None
    css_selector: str | None = None
    cron: str | None = None
    cron_human: str | None = None
    notification_method: str | None = None
    webhook_url: str | None = None
    enabled: bool | None = None


@router.get("/monitor/jobs")
async def list_monitor_jobs():
    """List all monitor jobs."""
    from .monitor_service import get_monitor_service

    svc = get_monitor_service()
    return {"jobs": svc.list_jobs()}


@router.get("/monitor/jobs/{job_id}")
async def get_monitor_job(job_id: str):
    """Get a single monitor job."""
    from .monitor_service import get_monitor_service

    svc = get_monitor_service()
    job = svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Monitor job not found")
    return job


@router.post("/monitor/jobs", status_code=201)
async def create_monitor_job(body: MonitorJobCreate):
    """Create a new monitor job."""
    from .monitor_service import get_monitor_service

    svc = get_monitor_service()
    try:
        job = svc.create_job(
            label=body.label,
            url=body.url,
            original_prompt=body.original_prompt,
            extraction_mode=body.extraction_mode,
            css_selector=body.css_selector,
            cron=body.cron,
            cron_human=body.cron_human,
            notification_method=body.notification_method,
            webhook_url=body.webhook_url,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return job


@router.put("/monitor/jobs/{job_id}")
async def update_monitor_job(job_id: str, body: MonitorJobUpdate):
    """Update a monitor job."""
    from .monitor_service import get_monitor_service

    svc = get_monitor_service()
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        job = svc.update_job(job_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not job:
        raise HTTPException(status_code=404, detail="Monitor job not found")
    return job


@router.delete("/monitor/jobs/{job_id}")
async def delete_monitor_job(job_id: str):
    """Delete a monitor job."""
    from .monitor_service import get_monitor_service

    svc = get_monitor_service()
    deleted = svc.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Monitor job not found")
    return {"deleted": True}


@router.get("/monitor/jobs/{job_id}/checks")
async def get_monitor_job_checks(job_id: str, limit: int = 20):
    """Get recent checks for a monitor job."""
    from .monitor_service import get_monitor_service

    svc = get_monitor_service()
    job = svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Monitor job not found")
    checks = svc.get_job_checks(job_id, limit=limit)
    return {"checks": checks}


@router.post("/monitor/jobs/{job_id}/check")
async def trigger_monitor_check(job_id: str):
    """Trigger an immediate check for a monitor job."""
    from .monitor_service import get_monitor_service

    svc = get_monitor_service()
    result = svc.check_now(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Monitor job not found")
    return result


# -- PDF extraction helper -----------------------------------------------------


def _extract_pdf(pdf_path: Path) -> Path | None:
    """Extract text from a PDF and save as ``.extracted.md`` alongside it."""
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None

    extracted = pdf_path.with_suffix(".extracted.md")

    if pdfplumber:
        try:
            pages: list[str] = []
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    tables = page.extract_tables()
                    table_text = ""
                    if tables:
                        for table in tables:
                            rows = []
                            for row in table:
                                cells = [str(c).strip() if c else "" for c in row]
                                rows.append(" | ".join(cells))
                            table_text += "\n".join(rows) + "\n"

                    text = page.extract_text() or ""
                    content = table_text.strip() if table_text.strip() else text.strip()
                    if content:
                        pages.append(f"--- Page {i} ---\n{content}")

            if not pages:
                logger.warning("PDF has pages but no extractable text: %s", pdf_path.name)
                return None

            result = f"# {pdf_path.name}\n\n" + "\n\n".join(pages)
            extracted.write_text(result, encoding="utf-8")
            return extracted
        except Exception as exc:
            logger.warning("PDF extraction failed for %s: %s", pdf_path.name, exc)
            return None

    # Fallback: CLI pdftotext
    try:
        proc = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            extracted.write_text(f"# {pdf_path.name}\n\n{proc.stdout}", encoding="utf-8")
            return extracted
    except Exception as exc:
        logger.warning("CLI pdftotext failed for %s: %s", pdf_path.name, exc)

    return None


# ── Skills listing endpoint ───────────────────────────────────────────────────


@router.get("/skills")
async def list_skills():
    """Return all available skills from skills.yaml."""
    from .ws_endpoint import _runtime, _runtime_ready

    try:
        await asyncio.wait_for(_runtime_ready.wait(), timeout=10)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Runtime not ready yet")

    rt = _runtime
    if rt is None:
        raise HTTPException(503, "Runtime not initialised")

    skills = rt.list_skills()
    return {
        "skills": [
            {
                "id": s["id"],
                "description": s["description"],
                "aliases": s["aliases"],
                "keywords": s["keywords"],
                "modes": s["modes"],
                "disable_for_modes": s.get("disable_for_modes", []),
                "auto_detect": s["auto_detect"],
                "priority": s["priority"],
                "has_instructions": bool(s.get("instruction_text")),
                "instruction_length": len(s.get("instruction_text", "")),
            }
            for s in skills
        ],
        "max_skills": rt.skills_max,
    }


# ── Dry-Run / Prompt Test endpoint ────────────────────────────────────────────


class DryRunRequest(BaseModel):
    query: str
    session_id: str | None = None
    last_mode: str = "chat"


def _trace_heuristic(
    query: str,
    rt,
    last_mode: str = "chat",
) -> list[dict]:
    """Replay the heuristic classifier logic and record which sub-checks fired."""
    from .ws_endpoint import (
        _AGENT_KEYWORDS,
        _AGENT_PATTERNS,
    )
    from .ws_endpoint import (
        _strip_custom_prefix as _scp,
    )
    from .ws_endpoint import (
        _strip_mode_prefix as _smp,
    )

    sub: list[dict] = []
    query_lower = query.lower()

    # 1. Agent availability
    if not rt.agent_available:
        sub.append(
            {
                "id": "agent_avail",
                "label": "Agent Available",
                "status": "skipped",
                "detail": "Agent not available — forced chat",
            }
        )
        return sub
    sub.append(
        {"id": "agent_avail", "label": "Agent Available", "status": "active", "detail": "Agent runtime is available"}
    )

    # 2. Custom agent alias
    _, custom_mode = _scp(query, rt)
    if custom_mode:
        sub.append(
            {
                "id": "custom_alias",
                "label": "Custom Agent Alias",
                "status": "active",
                "detail": f"Matched custom agent: {custom_mode}",
            }
        )
        return sub
    sub.append(
        {
            "id": "custom_alias",
            "label": "Custom Agent Alias",
            "status": "skipped",
            "detail": "No custom agent alias matched",
        }
    )

    # 3. Prefix check
    _, forced_mode = _smp(query)
    if forced_mode:
        sub.append(
            {
                "id": "prefix_check",
                "label": "Prefix Detection",
                "status": "active",
                "detail": f"Prefix matched → '{forced_mode}'",
            }
        )
        return sub
    sub.append(
        {"id": "prefix_check", "label": "Prefix Detection", "status": "skipped", "detail": "No mode prefix found"}
    )

    # 4. Generic @source check
    if query_lower.lstrip().startswith("@"):
        sub.append(
            {
                "id": "generic_at",
                "label": "Generic @source",
                "status": "active",
                "detail": "Starts with @ — routed to search (deprecated)",
            }
        )
        return sub
    sub.append({"id": "generic_at", "label": "Generic @source", "status": "skipped", "detail": "No @ prefix"})

    # 5. Keyword matching
    words = set(_re.sub(r"[^\w\s/~.]", "", query_lower).split())
    hits = words & _AGENT_KEYWORDS
    if len(hits) >= 2:
        sub.append(
            {
                "id": "keywords",
                "label": "Keyword Match",
                "status": "active",
                "detail": f"{len(hits)} agent keywords: {', '.join(sorted(hits))}",
            }
        )
        return sub
    sub.append(
        {
            "id": "keywords",
            "label": "Keyword Match",
            "status": "skipped",
            "detail": f"{len(hits)} keyword(s) matched (need ≥2): {', '.join(sorted(hits)) or '—'}",
        }
    )

    # 6. Pattern matching
    matched_pattern = None
    for pattern in _AGENT_PATTERNS:
        if _re.search(pattern, query_lower):
            matched_pattern = pattern
            break
    if matched_pattern:
        sub.append(
            {
                "id": "patterns",
                "label": "Regex Pattern",
                "status": "active",
                "detail": f"Pattern matched: {matched_pattern}",
            }
        )
        return sub
    sub.append(
        {
            "id": "patterns",
            "label": "Regex Pattern",
            "status": "skipped",
            "detail": f"0 of {len(_AGENT_PATTERNS)} patterns matched",
        }
    )

    # 7. Sticky mode
    sticky_modes = {"web_search", "logs", "sql", "scheduler", "monitor"}
    if last_mode in sticky_modes:
        _FOLLOWUP_PRONOUNS = _re.compile(r"\bthem\b|\bthose\b|\bthese\b|\bthey\b|\bits?\b|\bthat\b")
        _FOLLOWUP_PHRASES = _re.compile(
            r"\bwhat\s+about\b|\bhow\s+about\b|\band\s+what\s+about\b"
            r"|\bwhat\s+if\b|\bwhat\s+else\b|\bhow\s+else\b"
            r"|\band\s+\w+\?$|\bwhich\b|\bwhere\b"
        )
        has_followup = bool(_FOLLOWUP_PRONOUNS.search(query_lower) or _FOLLOWUP_PHRASES.search(query_lower))

        if len(words) <= 15 and (has_followup or len(words) <= 10):
            sub.append(
                {
                    "id": "sticky_mode",
                    "label": "Sticky Mode (Tier 1)",
                    "status": "active",
                    "detail": f"Short follow-up ({len(words)} words) — staying in '{last_mode}'",
                }
            )
            return sub

        _CONTEXT_REF = _re.compile(
            r"\bfrom\s+that\b|\bfrom\s+the\s+list\b|\bfrom\s+those\b"
            r"|\bfrom\s+above\b|\babove\s+list\b"
            r"|\bthe\s+highest\b|\bthe\s+lowest\b|\bthe\s+best\b|\bthe\s+top\b"
            r"|\bpick\b.*\bfrom\b|\bchoose\b.*\bfrom\b|\bselect\b.*\bfrom\b"
        )
        _ACTION_VERBS = _re.compile(
            r"\bsave\b|\bstore\b|\bwrite\b|\bdownload\b|\bexport\b"
            r"|\bsearch\b|\bfind\b|\blook\s?up\b|\bfetch\b|\bget\b"
            r"|\bcreate\b|\bgenerate\b|\bmake\b"
        )
        _FILE_PATH = _re.compile(r"~/|/[Uu]sers/|/home/|\bdownloads?\b|\bdesktop\b|\bdocuments?\b|\.\w{1,5}\b")
        has_context = bool(has_followup or _CONTEXT_REF.search(query_lower))
        has_action = bool(_ACTION_VERBS.search(query_lower) or _FILE_PATH.search(query_lower))

        if has_context and has_action:
            sub.append(
                {
                    "id": "sticky_mode",
                    "label": "Sticky Mode (Tier 2)",
                    "status": "active",
                    "detail": f"Context ref + action verb — staying in '{last_mode}'",
                }
            )
            return sub

        sub.append(
            {
                "id": "sticky_mode",
                "label": "Sticky Mode",
                "status": "skipped",
                "detail": f"Last was '{last_mode}' but no follow-up signals",
            }
        )
    elif last_mode == "agent" and len(words) <= 15:
        _SEARCH_SIGNALS = {"what", "how", "why", "explain", "describe", "define", "documentation"}
        has_search = bool(words & _SEARCH_SIGNALS)
        has_hint = bool(hits) or bool(
            _re.search(
                r"~/|/[Uu]sers/|/home/|/opt/|/var/|/tmp/|\.\w{1,5}$"
                r"|\bnow\b|\balso\b|\bthen\b|\band\b|\bsame\b|\bagain\b"
                r"|\bthem\b|\bthose\b|\bthese\b|\bthey\b|\bits?\b|\bthat\b"
                r"|\bdelete\b|\bremove\b|\bclean\b|\bback\s?up\b|\barchive\b"
                r"|\bmove\b|\bcopy\b|\brename\b|\bopen\b|\bshow\b|\bdo\b",
                query_lower,
            )
        )
        if has_hint and not has_search:
            sub.append(
                {
                    "id": "sticky_agent",
                    "label": "Sticky Agent",
                    "status": "active",
                    "detail": "Agent follow-up — hints present, no search signals",
                }
            )
            return sub
        if len(words) <= 8 and not has_search:
            sub.append(
                {
                    "id": "sticky_agent",
                    "label": "Sticky Agent",
                    "status": "active",
                    "detail": f"Very short follow-up ({len(words)} words)",
                }
            )
            return sub
        sub.append(
            {
                "id": "sticky_agent",
                "label": "Sticky Agent",
                "status": "skipped",
                "detail": f"Agent follow-up check failed (search_signals={has_search}, hints={has_hint})",
            }
        )
    else:
        sub.append(
            {
                "id": "sticky_mode",
                "label": "Sticky Mode",
                "status": "skipped",
                "detail": f"Last mode '{last_mode}' — not a sticky candidate",
            }
        )

    # 8. Default
    sub.append(
        {
            "id": "default",
            "label": "Default Fallback",
            "status": "active",
            "detail": "No rules matched — defaulting to 'chat'",
        }
    )
    return sub


@router.post("/dry-run")
async def dry_run(body: DryRunRequest):
    """Trace the full prompt classification pipeline without executing anything.

    Returns a structured trace of each step: mode detection, classification,
    profile routing, query refinement, dispatch decision, and conversation
    history assembly — with timing for each step.
    """
    from .ws_endpoint import (
        _build_conversation_history,
        _classify_mode,
        _classify_mode_heuristic,
        _is_worker_mode,
        _resolve_skills,
        _runtime,
        _runtime_ready,
        _strip_custom_prefix,
        _strip_mode_prefix,
    )

    # Wait for runtime (max 10s)
    try:
        await asyncio.wait_for(_runtime_ready.wait(), timeout=10)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Runtime not ready yet")

    rt = _runtime
    if rt is None:
        raise HTTPException(503, "Runtime not initialised")

    db = _db
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "Query is required")

    steps: list[dict] = []
    total_start = time.perf_counter()

    # ── Step 1: Mode prefix detection (pure, sync) ────────────────────────
    t0 = time.perf_counter()
    cleaned_text, forced_mode = _strip_mode_prefix(query)
    steps.append(
        {
            "step": "mode_prefix",
            "label": "Mode Prefix Detection",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": {
                "forced_mode": forced_mode,
                "cleaned_text": cleaned_text,
                "had_prefix": forced_mode is not None,
            },
        }
    )

    # ── Step 2: Custom agent detection (pure, sync) ───────────────────────
    t0 = time.perf_counter()
    custom_cleaned, custom_mode = _strip_custom_prefix(query, rt)
    custom_agent_name = None
    if custom_mode:
        agent_id = custom_mode.split(":", 1)[-1]
        agent_cfg = rt.get_custom_agent_by_id(agent_id) if hasattr(rt, "get_custom_agent_by_id") else None
        custom_agent_name = agent_cfg.get("name", agent_id) if agent_cfg else agent_id
    steps.append(
        {
            "step": "custom_agent",
            "label": "Custom Agent Detection",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": {
                "detected": custom_mode is not None,
                "mode": custom_mode,
                "agent_name": custom_agent_name,
                "cleaned_text": custom_cleaned if custom_mode else None,
            },
        }
    )

    # ── Step 3: Heuristic classification (pure, sync — no LLM) ───────────
    t0 = time.perf_counter()
    heuristic_mode, _heuristic_conf = _classify_mode_heuristic(query, rt, last_mode=body.last_mode)
    # Build sub-step trace showing which checks fired
    heuristic_sub_steps = _trace_heuristic(query, rt, last_mode=body.last_mode)
    steps.append(
        {
            "step": "heuristic",
            "label": "Heuristic Classification",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": {
                "mode": heuristic_mode,
                "sub_steps": heuristic_sub_steps,
            },
        }
    )

    # ── Step 4: Full LLM classification (async — includes LLM call) ──────
    t0 = time.perf_counter()
    llm_mode = None
    llm_error = None
    try:
        llm_mode = await _classify_mode(
            query,
            rt,
            last_mode=body.last_mode,
            db=db,
            session_id=body.session_id,
        )
    except Exception as exc:
        llm_error = str(exc)
    steps.append(
        {
            "step": "llm_classifier",
            "label": "LLM Intent Classifier",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": {
                "mode": llm_mode,
                "error": llm_error,
                "used_explicit_prefix": forced_mode is not None,
                "skipped_llm": forced_mode is not None or custom_mode is not None,
            },
        }
    )

    # Final resolved mode — with arbitration trace
    final_mode = custom_mode or llm_mode or heuristic_mode or "chat"
    arb_sub_steps: list[dict] = []
    if custom_mode:
        arb_sub_steps.append(
            {
                "id": "custom_agent",
                "label": "Custom Agent",
                "status": "active",
                "detail": f"Custom agent '{custom_agent_name}' wins — highest priority",
            }
        )
        arb_sub_steps.append(
            {"id": "llm_result", "label": "LLM Classifier", "status": "skipped", "detail": "Overridden by custom agent"}
        )
        arb_sub_steps.append(
            {
                "id": "heuristic_result",
                "label": "Heuristic",
                "status": "skipped",
                "detail": "Overridden by custom agent",
            }
        )
    elif forced_mode:
        arb_sub_steps.append(
            {
                "id": "prefix_match",
                "label": "Prefix Match",
                "status": "active",
                "detail": f"Explicit prefix forced mode to '{forced_mode}'",
            }
        )
        arb_sub_steps.append(
            {
                "id": "llm_result",
                "label": "LLM Classifier",
                "status": "skipped" if llm_mode == forced_mode else "alternate",
                "detail": f"LLM said '{llm_mode}'" if llm_mode else "LLM skipped (prefix fast-path)",
            }
        )
        arb_sub_steps.append(
            {
                "id": "heuristic_result",
                "label": "Heuristic",
                "status": "skipped" if heuristic_mode == forced_mode else "alternate",
                "detail": f"Heuristic said '{heuristic_mode}'",
            }
        )
    elif llm_mode and llm_mode != heuristic_mode:
        arb_sub_steps.append(
            {
                "id": "llm_result",
                "label": "LLM Classifier",
                "status": "active",
                "detail": f"LLM classified as '{llm_mode}' — wins over heuristic",
            }
        )
        arb_sub_steps.append(
            {
                "id": "heuristic_result",
                "label": "Heuristic",
                "status": "alternate",
                "detail": f"Heuristic said '{heuristic_mode}' — overridden by LLM",
            }
        )
    elif llm_mode:
        arb_sub_steps.append(
            {
                "id": "llm_result",
                "label": "LLM Classifier",
                "status": "active",
                "detail": f"LLM and heuristic agree on '{llm_mode}'",
            }
        )
        arb_sub_steps.append(
            {
                "id": "heuristic_result",
                "label": "Heuristic",
                "status": "active",
                "detail": f"Heuristic agrees: '{heuristic_mode}'",
            }
        )
    else:
        arb_sub_steps.append(
            {
                "id": "llm_result",
                "label": "LLM Classifier",
                "status": "error" if llm_error else "skipped",
                "detail": f"LLM error: {llm_error}" if llm_error else "LLM returned no result",
            }
        )
        arb_sub_steps.append(
            {
                "id": "heuristic_result",
                "label": "Heuristic",
                "status": "active" if heuristic_mode else "skipped",
                "detail": f"Heuristic fallback: '{heuristic_mode}'" if heuristic_mode else "No heuristic match",
            }
        )
        if not heuristic_mode:
            arb_sub_steps.append(
                {
                    "id": "default_fallback",
                    "label": "Default Fallback",
                    "status": "active",
                    "detail": "No classifier matched — defaulting to 'chat'",
                }
            )

    # Insert arbitration as a real step so the frontend can access sub_steps
    steps.append(
        {
            "step": "arbitration",
            "label": "Arbitration",
            "elapsed_ms": 0,
            "result": {
                "mode": final_mode,
                "winner": "custom_agent"
                if custom_mode
                else (
                    "prefix" if forced_mode else ("llm" if llm_mode else ("heuristic" if heuristic_mode else "default"))
                ),
                "sub_steps": arb_sub_steps,
            },
        }
    )

    # ── Step 5: Profile routing (async — LLM call for agent modes) ────────
    t0 = time.perf_counter()
    profile_result: dict = {"profile": None, "reason": None, "error": None}
    worker_modes = {"agent", "web_search", "logs", "sql", "discover", "pipeline"}
    base_mode = final_mode.split(":", 1)[-1] if final_mode.startswith("custom:") else final_mode
    profile_sub_steps: list[dict] = []

    if base_mode in worker_modes or final_mode.startswith("custom:"):
        profile_sub_steps.append(
            {
                "id": "worker_check",
                "label": "Worker Mode Check",
                "status": "active",
                "detail": f"Mode '{base_mode}' requires worker — eligible for ProfileRouter",
            }
        )
        try:
            if forced_mode == "agent" or final_mode.startswith("custom:"):
                profile_result["profile"] = "agent"
                if forced_mode == "agent":
                    profile_result["reason"] = "Explicit @agent prefix — skipping ProfileRouter"
                    profile_sub_steps.append(
                        {
                            "id": "prefix_bypass",
                            "label": "Prefix Bypass",
                            "status": "active",
                            "detail": "@agent prefix detected — skip ProfileRouter, use agent profile directly",
                        }
                    )
                else:
                    profile_result["reason"] = f"Custom agent mode ({custom_agent_name}) — using agent profile"
                    profile_sub_steps.append(
                        {
                            "id": "custom_bypass",
                            "label": "Custom Agent Bypass",
                            "status": "active",
                            "detail": f"Custom agent '{custom_agent_name}' — skip ProfileRouter",
                        }
                    )
                profile_sub_steps.append(
                    {
                        "id": "profile_router",
                        "label": "ProfileRouter LLM",
                        "status": "skipped",
                        "detail": "Bypassed — not needed",
                    }
                )
            else:
                profile_sub_steps.append(
                    {
                        "id": "prefix_bypass",
                        "label": "Prefix Bypass",
                        "status": "skipped",
                        "detail": "No @agent prefix — proceeding to ProfileRouter",
                    }
                )
                try:
                    from agentforge.client import AIClient
                    from agentforge.router import ProfileRouter

                    router_client = AIClient(profile="tool")
                    prof_router = ProfileRouter(router_client)
                    route = await asyncio.to_thread(prof_router.select, query)
                    profile_result["profile"] = route.profile
                    profile_result["reason"] = route.reason
                    profile_sub_steps.append(
                        {
                            "id": "profile_router",
                            "label": "ProfileRouter LLM",
                            "status": "active",
                            "detail": f"Selected profile: {route.profile} — {route.reason}",
                        }
                    )
                except Exception:
                    profile_result["profile"] = "agent"
                    profile_result["reason"] = "ProfileRouter unavailable — defaulting to agent"
                    profile_sub_steps.append(
                        {
                            "id": "profile_router",
                            "label": "ProfileRouter LLM",
                            "status": "error",
                            "detail": "ProfileRouter unavailable — fell back to agent",
                        }
                    )
        except Exception as exc:
            profile_result["error"] = str(exc)
    else:
        profile_sub_steps.append(
            {
                "id": "worker_check",
                "label": "Worker Mode Check",
                "status": "skipped",
                "detail": f"Mode '{base_mode}' is non-worker — no ProfileRouter needed",
            }
        )
        profile_result["reason"] = f"Mode '{base_mode}' does not use ProfileRouter"

    profile_result["sub_steps"] = profile_sub_steps
    steps.append(
        {
            "step": "profile_routing",
            "label": "Profile Routing",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": profile_result,
        }
    )

    # ── Step 6: Model resolution ──────────────────────────────────────────
    t0 = time.perf_counter()
    model_info: dict = {"model": None, "profile_name": None, "host": None}
    try:
        from app.config import settings as af_settings

        target_profile = profile_result.get("profile")
        if target_profile:
            # Use _resolve_profile() which reads from YAML config
            resolved = af_settings.ollama._resolve_profile(target_profile)
            model_info["model"] = resolved.model
            model_info["profile_name"] = resolved.name
            model_info["host"] = str(resolved.host) if resolved.host else None
        else:
            role = af_settings.ollama.get_role("answer_generation")
            model_info["model"] = role.profile.model
            model_info["profile_name"] = role.profile.name
            model_info["host"] = str(role.profile.host) if role.profile.host else None
    except Exception as exc:
        model_info["error"] = str(exc)
    steps.append(
        {
            "step": "model_resolution",
            "label": "Model Resolution",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": model_info,
        }
    )

    # ── Step 7: Dispatch decision ─────────────────────────────────────────
    is_worker = _is_worker_mode(final_mode)
    dispatch_target = "worker (SAQ)" if is_worker else "inline (asyncio)"
    runner_map = {
        "scheduler": "_run_scheduler",
        "monitor": "_run_monitor",
        "search": "_run_search",
        "chat": "_run_chat",
    }
    runner = "_run_agent_job (worker)" if is_worker else runner_map.get(base_mode, "_run_chat")
    steps.append(
        {
            "step": "dispatch",
            "label": "Dispatch Decision",
            "elapsed_ms": 0,
            "result": {
                "mode": final_mode,
                "dispatch": dispatch_target,
                "runner": runner,
                "is_worker": is_worker,
            },
        }
    )

    # ── Step 8: Conversation history (read-only DB + Qdrant) ──────────────
    t0 = time.perf_counter()
    history_info: dict = {"turns": 0, "has_facts": False, "has_memories": False, "error": None}
    if db and body.session_id:
        try:
            history = _build_conversation_history(db, body.session_id, query=cleaned_text)
            if history:
                history_info["turns"] = len([m for m in history if m.get("role") == "user"])
                history_info["has_facts"] = any("[Known Facts]" in m.get("content", "") for m in history)
                history_info["has_memories"] = any("[Relevant context" in m.get("content", "") for m in history)
                history_info["total_messages"] = len(history)
        except Exception as exc:
            history_info["error"] = str(exc)
    else:
        history_info["note"] = "No session_id provided — history not loaded"
    steps.append(
        {
            "step": "conversation_history",
            "label": "Conversation History",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": history_info,
        }
    )

    # ── Step 9: Tool resolution ─────────────────────────────────────────
    t0 = time.perf_counter()
    tool_info: dict = {"tools": [], "count": 0, "source": None, "error": None}
    tool_sub_steps: list[dict] = []
    try:
        from .ws_endpoint import _LOG_ANALYSIS_TOOLS, _WEB_SEARCH_TOOLS

        if base_mode == "web_search":
            tool_info["tools"] = list(_WEB_SEARCH_TOOLS)
            tool_info["count"] = len(_WEB_SEARCH_TOOLS)
            tool_info["source"] = "_WEB_SEARCH_TOOLS (fixed set)"
            tool_sub_steps.append(
                {
                    "id": "mode_check",
                    "label": "Mode → Tool Set",
                    "status": "active",
                    "detail": f"web_search mode → fixed tool set ({len(_WEB_SEARCH_TOOLS)} tools)",
                }
            )
            tool_sub_steps.append(
                {
                    "id": "profile_lookup",
                    "label": "Profile Lookup",
                    "status": "skipped",
                    "detail": "Not needed — fixed tool set",
                }
            )
        elif base_mode == "logs":
            tool_info["tools"] = list(_LOG_ANALYSIS_TOOLS)
            tool_info["count"] = len(_LOG_ANALYSIS_TOOLS)
            tool_info["source"] = "_LOG_ANALYSIS_TOOLS (fixed set)"
            tool_sub_steps.append(
                {
                    "id": "mode_check",
                    "label": "Mode → Tool Set",
                    "status": "active",
                    "detail": f"logs mode → fixed tool set ({len(_LOG_ANALYSIS_TOOLS)} tools)",
                }
            )
            tool_sub_steps.append(
                {
                    "id": "profile_lookup",
                    "label": "Profile Lookup",
                    "status": "skipped",
                    "detail": "Not needed — fixed tool set",
                }
            )
        elif base_mode in ("search", "chat", "scheduler", "monitor"):
            tool_info["tools"] = []
            tool_info["count"] = 0
            tool_info["source"] = f"Mode '{base_mode}' does not use tools"
            tool_sub_steps.append(
                {
                    "id": "mode_check",
                    "label": "Mode → Tool Set",
                    "status": "skipped",
                    "detail": f"Mode '{base_mode}' has no tools",
                }
            )
        elif rt.agent_available:
            selected_profile = profile_result.get("profile") or "agent"
            tool_names = rt.agent_profiles.get(selected_profile, [])
            tool_info["tools"] = list(tool_names)
            tool_info["count"] = len(tool_names)
            tool_info["source"] = f"agent_profiles['{selected_profile}']"
            tool_sub_steps.append(
                {
                    "id": "mode_check",
                    "label": "Mode → Tool Set",
                    "status": "active",
                    "detail": "Agent mode — requires profile-based tool lookup",
                }
            )
            tool_sub_steps.append(
                {
                    "id": "profile_lookup",
                    "label": "Profile Lookup",
                    "status": "active",
                    "detail": f"Profile '{selected_profile}' → {len(tool_names)} tools",
                }
            )
        else:
            tool_info["source"] = "Agent tools not available"
            tool_sub_steps.append(
                {
                    "id": "mode_check",
                    "label": "Mode → Tool Set",
                    "status": "error",
                    "detail": "Agent runtime not available",
                }
            )
    except Exception as exc:
        tool_info["error"] = str(exc)
    tool_info["sub_steps"] = tool_sub_steps
    steps.append(
        {
            "step": "tools",
            "label": "Tool Resolution",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": tool_info,
        }
    )

    # ── Step 10: Skill resolution ────────────────────────────────────────
    t0 = time.perf_counter()
    skill_info: dict = {"skills": [], "count": 0, "detection": [], "error": None}
    skill_sub_steps: list[dict] = []
    try:
        _, matched_skills, skill_promoted_mode = _resolve_skills(cleaned_text, rt, final_mode)

        # Sub-step: alias pass
        alias_matches = []
        keyword_matches = []
        if matched_skills:
            for s in matched_skills:
                detection_method = (
                    "alias" if any(a in query.lower() for a in s.get("aliases", [])) else "auto-detect (keywords)"
                )
                skill_info["skills"].append(
                    {
                        "id": s["id"],
                        "description": s.get("description", ""),
                        "detection": detection_method,
                        "has_instructions": bool(s.get("instruction_text")),
                        "instruction_length": len(s.get("instruction_text", "")),
                        "modes": s.get("modes", []),
                    }
                )
                skill_info["detection"].append(detection_method)
                if detection_method == "alias":
                    alias_matches.append(s["id"])
                else:
                    keyword_matches.append(s["id"])
            skill_info["count"] = len(matched_skills)
        else:
            skill_info["note"] = "No skills matched for this query/mode combination"

        # Build sub-steps
        available_count = len(rt.skills_by_id) if rt.skills_by_id else 0
        alias_count = len(rt.skills) if rt.skills else 0
        skill_sub_steps.append(
            {
                "id": "alias_scan",
                "label": "Alias Scan",
                "status": "active" if alias_matches else "skipped",
                "detail": (
                    f"Matched: {', '.join(alias_matches)}"
                    if alias_matches
                    else f"Scanned {alias_count} aliases — no match"
                ),
            }
        )
        skill_sub_steps.append(
            {
                "id": "keyword_scan",
                "label": "Keyword Auto-Detect",
                "status": "active" if keyword_matches else "skipped",
                "detail": (
                    f"Matched: {', '.join(keyword_matches)}"
                    if keyword_matches
                    else f"Scanned {available_count} skills — no keyword match (need ≥2)"
                ),
            }
        )

        if skill_promoted_mode and matched_skills:
            skill_info["mode_promotion"] = {
                "from": final_mode,
                "to": skill_promoted_mode,
                "reason": "Skill alias requires a different mode",
            }
            final_mode = skill_promoted_mode
            skill_sub_steps.append(
                {
                    "id": "mode_promotion",
                    "label": "Mode Promotion",
                    "status": "active",
                    "detail": f"Skill requires promotion: {skill_info['mode_promotion']['from']} → {skill_promoted_mode}",
                }
            )
        else:
            skill_sub_steps.append(
                {
                    "id": "mode_promotion",
                    "label": "Mode Promotion",
                    "status": "skipped",
                    "detail": "No promotion needed — mode is compatible",
                }
            )

    except Exception as exc:
        skill_info["error"] = str(exc)
    skill_info["sub_steps"] = skill_sub_steps
    steps.append(
        {
            "step": "skills",
            "label": "Skill Resolution",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "result": skill_info,
        }
    )

    total_elapsed = round((time.perf_counter() - total_start) * 1000, 2)

    return {
        "query": query,
        "final_mode": final_mode,
        "total_elapsed_ms": total_elapsed,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Screenshot serving (explicit endpoint — bypasses static file mount)
# ---------------------------------------------------------------------------


@router.get("/screenshots/{filename}")
async def serve_screenshot(filename: str):
    """Serve a captured screenshot by filename.

    Explicit endpoint that bypasses the /uploads/ static mount, ensuring
    correct Content-Type and cache headers regardless of reverse proxy config.
    Uses the same _upload_base configured by app.py lifespan.
    """
    from fastapi.responses import FileResponse

    # Use the authoritative upload base set by app.py (same as the static mount)
    if _upload_base is None:
        raise HTTPException(status_code=503, detail="Upload directory not configured")

    screenshots_dir = _upload_base / "screenshots"
    filepath = screenshots_dir / filename

    logger.debug(
        "serve_screenshot: _upload_base=%s, screenshots_dir=%s, filepath=%s, exists=%s",
        _upload_base,
        screenshots_dir,
        filepath,
        filepath.exists(),
    )

    # Security: prevent path traversal
    try:
        resolved = filepath.resolve()
        if not str(resolved).startswith(str(screenshots_dir.resolve())):
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=403, detail="Forbidden")

    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Screenshot not found (looked in {screenshots_dir})",
        )

    return FileResponse(
        filepath,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )
