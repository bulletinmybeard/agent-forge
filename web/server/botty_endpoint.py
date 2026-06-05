"""WebSocket endpoint for Botty — the proactive knowledge companion.

Botty monitors session events in real-time and proactively surfaces relevant
nudges (suggestions, cross-session insights, data availability alerts) to the user.

The Botty endpoint is a separate WebSocket channel from the main chat endpoint,
allowing the frontend to manage Botty independently of the chat conversation.

WebSocket Protocol (Botty-specific events):

Server → Client:
    botty.nudge — {type, nudge_id, message, action_type, related_sessions?, reasoning?}
    botty.status — {type, phase, momentum, message_count}
    botty.recall — {type, results: [{session_id, query, preview, score, timestamp}]}
    botty.quiet — {type, reason, resume_after_seconds}

Client → Server:
    botty.query — {type, text} — user asks Botty directly
    botty.dismiss — {type, nudge_id} — user dismisses a nudge
    botty.helpful — {type, nudge_id} — user marks nudge as helpful
    botty.search — {type, query} — user searches past sessions
    ping — heartbeat

Architecture:
    - Two concurrent async tasks:
      a. _observe_events() — subscribes to Redis session events, calls engine.on_run_completed()
      b. _handle_client() — listens for client messages (dismiss, helpful, search, query, ping)
    - Database injection via set_database(db) setter pattern
    - Graceful disconnect handling
    - UUID generation for nudge_ids
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .database import ChatDatabase
from .session_events import SessionEventSubscriber

logger = logging.getLogger(__name__)

router = APIRouter()

# Global database reference (set via set_database at app startup)
_db: ChatDatabase | None = None


def set_database(db: ChatDatabase) -> None:
    """Set the shared database reference (called from app.py lifespan)."""
    global _db
    _db = db


def get_db() -> ChatDatabase:
    """Get the shared database reference, raising if not initialised."""
    if _db is None:
        raise RuntimeError("Database not initialised")
    return _db


class BottyEngine:
    """Botty inference engine — produces nudges and recalls from session events.

    The engine observes session lifecycle events (run_completed, run_error, etc.)
    and generates proactive nudges:
        - Cross-session insight nudges
        - Data availability alerts
        - Helpful suggestions based on context
        - Reminders of related past work

    Nudges are minimal and actionable (max 1-2 per session to avoid noise).
    """

    def __init__(self, db: ChatDatabase, session_id: str) -> None:
        """Initialize the engine with database and current session context."""
        self.db = db
        self.session_id = session_id
        self.nudge_queue: list[dict[str, Any]] = []
        self.momentum: int = 0  # Energy level (0-100)
        self.phase: str = "observe"  # observe, recall, suggest
        self.message_count: int = 0

    async def on_run_completed(
        self,
        event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Process a run_completed event and generate nudges if applicable."""
        nudges: list[dict[str, Any]] = []

        # Only process if this is a successful completion
        if event.get("status") != "success":
            return nudges

        try:
            run_session_id = event.get("session_id", "")
            mode = event.get("mode", "search")
            tools_used = event.get("tools_used", [])
            query_preview = event.get("query_preview", "")

            # If this event is from a different session, consider cross-session insights
            if run_session_id and run_session_id != self.session_id:
                nudges.extend(await self._generate_cross_session_nudge(run_session_id, mode, query_preview, tools_used))

            # Generate data availability nudges
            if mode == "search":
                nudges.extend(await self._generate_data_nudges(query_preview))

            # Generate helpful suggestion nudges
            nudges.extend(await self._generate_suggestion_nudges(mode, tools_used, query_preview))

            self.momentum = min(100, self.momentum + 5)
            self.message_count += 1

        except Exception as exc:
            logger.warning("Error processing run_completed event: %s", exc)

        return nudges

    async def _generate_cross_session_nudge(
        self,
        run_session_id: str,
        mode: str,
        query_preview: str,
        tools_used: list[str],
    ) -> list[dict[str, Any]]:
        """Generate a nudge about related work in other sessions.

        Returns a nudge only if there's a meaningful cross-session connection.
        """
        nudges: list[dict[str, Any]] = []

        # Simple heuristic: if the query mentions similar keywords
        # to our current session, surface the connection
        try:
            # Avoid too many nudges per session
            if self.momentum > 60:
                return nudges

            # Try to find a pattern (in a real system, this would use semantic similarity)
            if len(query_preview) > 10:
                nudge_id = str(uuid.uuid4())
                nudges.append(
                    {
                        "type": "botty.nudge",
                        "nudge_id": nudge_id,
                        "message": f"Related work in another session: {query_preview[:50]}...",
                        "action_type": "switch_session",
                        "related_sessions": [run_session_id],
                        "reasoning": f"Another session ran in {mode} mode with similar tools",
                    }
                )
                self.momentum = min(100, self.momentum + 10)

        except Exception as exc:
            logger.debug("Error generating cross-session nudge: %s", exc)

        return nudges

    async def _generate_data_nudges(
        self,
        query_preview: str,
    ) -> list[dict[str, Any]]:
        """Generate nudges about available data or insights.

        Returns nudges when relevant data might be useful (e.g.,, past search results).
        """
        nudges: list[dict[str, Any]] = []

        try:
            if not query_preview or len(query_preview) < 5:
                return nudges

            # Simple check: if query mentions "history", "past", or "again",
            # suggest recalling similar sessions
            keywords = ["history", "past", "again", "before", "last time", "remember"]
            if any(kw in query_preview.lower() for kw in keywords):
                nudge_id = str(uuid.uuid4())
                nudges.append(
                    {
                        "type": "botty.nudge",
                        "nudge_id": nudge_id,
                        "message": "Found similar work in your history — want to recall it?",
                        "action_type": "recall_similar",
                        "reasoning": "Query mentions historical context",
                    }
                )
                self.momentum = min(100, self.momentum + 8)

        except Exception as exc:
            logger.debug("Error generating data nudges: %s", exc)

        return nudges

    async def _generate_suggestion_nudges(
        self,
        mode: str,
        tools_used: list[str],
        query_preview: str,
    ) -> list[dict[str, Any]]:
        """Generate helpful suggestion nudges based on context.

        Returns nudges for things like "try the agent mode" or "use web search".
        """
        nudges: list[dict[str, Any]] = []

        try:
            # Avoid suggestion fatigue
            if self.momentum > 70:
                return nudges

            # Suggest agent mode if user is stuck in search mode
            if mode == "search" and "how do I" in query_preview.lower():
                nudge_id = str(uuid.uuid4())
                nudges.append(
                    {
                        "type": "botty.nudge",
                        "nudge_id": nudge_id,
                        "message": "Tip: Try @agent mode for step-by-step help.",
                        "action_type": "suggest_mode",
                        "reasoning": "Query asks 'how to' — agent mode may be more helpful",
                    }
                )
                self.momentum = min(100, self.momentum + 5)

            # Suggest web search if no tools were used
            if not tools_used and mode == "search":
                nudge_id = str(uuid.uuid4())
                nudges.append(
                    {
                        "type": "botty.nudge",
                        "nudge_id": nudge_id,
                        "message": "Tip: Try @search for real-time web results.",
                        "action_type": "suggest_mode",
                        "reasoning": "Local search didn't use any tools — web search might help",
                    }
                )
                self.momentum = min(100, self.momentum + 5)

        except Exception as exc:
            logger.debug("Error generating suggestion nudges: %s", exc)

        return nudges

    def dismiss_nudge(self, nudge_id: str) -> None:
        """Mark a nudge as dismissed (reduces momentum)."""
        self.momentum = max(0, self.momentum - 5)
        logger.debug("Dismissed nudge %s", nudge_id)

    def mark_helpful(self, nudge_id: str) -> None:
        """Mark a nudge as helpful (increases momentum and future likelihood)."""
        self.momentum = min(100, self.momentum + 15)
        logger.debug("Marked nudge %s as helpful", nudge_id)

    async def search_sessions(self, query: str) -> list[dict[str, Any]]:
        """Search for sessions matching the query.

        Returns a list of sessions with similarity scores.
        """
        results: list[dict[str, Any]] = []

        try:
            # In a real system, this would use semantic search or full-text search
            # For now, simple keyword matching against recent sessions
            if not query:
                return results

            # Get all sessions (in production, limit to recent N)
            # This is a placeholder — in real implementation, query the DB properly
            logger.debug("Searching for sessions matching: %s", query)

            # Dummy result for now
            results.append(
                {
                    "session_id": uuid.uuid4().hex,
                    "query": query,
                    "preview": f"Session discussing {query[:30]}...",
                    "score": 0.75,
                    "timestamp": "2026-04-02T10:30:00Z",
                }
            )

        except Exception as exc:
            logger.warning("Error searching sessions: %s", exc)

        return results

    def get_status(self) -> dict[str, Any]:
        """Return current engine status."""
        return {
            "type": "botty.status",
            "phase": self.phase,
            "momentum": self.momentum,
            "message_count": self.message_count,
        }


# ---------------------------------------------------------------------------
# WebSocket Handler
# ---------------------------------------------------------------------------


@router.websocket("/ws/botty")
async def websocket_botty(ws: WebSocket, session_id: str | None = None) -> None:
    """WebSocket endpoint for Botty companion channel.

    Handles a separate channel from the main chat WS, allowing Botty to
    proactively surface nudges and communicate independently.

    Query parameters:
        session_id: Optional session ID to associate Botty with a specific session

    Connection flow:
        1. Accept WS connection
        2. Spawn two concurrent tasks:
           a. _observe_events() — listen to Redis for session events
           b. _handle_client() — listen to client messages
        3. On disconnect, cancel both tasks and clean up

    """
    # Optional API-key auth (off unless security.api_keys is set) — same gate as
    # /ws/chat. When a key is supplied via Sec-WebSocket-Protocol it must be
    # echoed back on accept().
    from app.security import negotiate_ws

    _ws_authorized, _ws_subprotocol = negotiate_ws(ws)
    if not _ws_authorized:
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept(subprotocol=_ws_subprotocol)

    db = get_db()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    engine = BottyEngine(db, session_id or "")

    # Use provided session_id if it exists, otherwise it may be set later
    if session_id:
        existing = db.get_session(session_id)
        if not existing:
            session_id = None

    logger.info("Botty WebSocket connected — session %s", session_id or "(new)")

    # Track active tasks for clean shutdown
    observe_task: asyncio.Task | None = None
    handle_task: asyncio.Task | None = None

    try:
        # Start both tasks concurrently
        observe_task = asyncio.create_task(_observe_events(ws, engine, redis_url, session_id or ""))
        handle_task = asyncio.create_task(_handle_client(ws, engine, db))

        # Wait for either task to complete or WebSocket to disconnect
        # Both tasks run concurrently; if one fails, we catch the exception
        done, pending = await asyncio.wait(
            [observe_task, handle_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # Check for exceptions in completed tasks
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass  # Expected on shutdown
            except Exception as exc:
                logger.warning("Task failed: %s", exc)

    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)

    finally:
        # Clean up both tasks
        if observe_task and not observe_task.done():
            observe_task.cancel()
            try:
                await observe_task
            except asyncio.CancelledError:
                pass

        if handle_task and not handle_task.done():
            handle_task.cancel()
            try:
                await handle_task
            except asyncio.CancelledError:
                pass

        logger.info("Botty WebSocket closed — session %s", session_id or "(unknown)")


async def _observe_events(
    ws: WebSocket,
    engine: BottyEngine,
    redis_url: str,
    session_id: str,
) -> None:
    """Observe session events via Redis Pub/Sub and send nudges to client.

    Subscribes to:
        - agentforge:sessions — all session lifecycle events
        - agentforge:session:{session_id} — events targeted at this session

    On each run_completed event, calls engine.on_run_completed() and sends
    any generated nudges to the client.
    """
    subscriber = SessionEventSubscriber(redis_url=redis_url)
    channels = ["agentforge:sessions"]
    if session_id:
        channels.append(f"agentforge:session:{session_id}")

    try:
        # Connect and subscribe
        await subscriber.subscribe(*channels)
        logger.info("Botty event observer subscribed to: %s", channels)

        # Listen for events
        async for event in subscriber.events():
            # Skip keepalive (None) ticks
            if not event:
                continue

            # Skip non-session events
            event_type = event.get("event_type")
            if event_type != "run_completed":
                continue

            try:
                # Let engine process the event
                nudges = await engine.on_run_completed(event)

                # Send each nudge to the client
                for nudge in nudges:
                    try:
                        await ws.send_json(nudge)
                    except WebSocketDisconnect:
                        # Peer left — unwind so the finally block unsubscribes.
                        return
                    except Exception as exc:
                        logger.warning("Failed to send nudge: %s", exc)

            except WebSocketDisconnect:
                return
            except Exception as exc:
                logger.warning("Error processing event: %s", exc)

    except WebSocketDisconnect:
        # Normal client disconnect — handled by the finally block.
        pass
    except Exception as exc:
        logger.warning("Event observer error: %s", exc)

    finally:
        try:
            await subscriber.unsubscribe()
        except Exception as exc:
            logger.debug("Error unsubscribing: %s", exc)


async def _handle_client(ws: WebSocket, engine: BottyEngine, db: ChatDatabase) -> None:
    """Handle client-initiated messages (ping, dismiss, helpful, search, query).

    Client message types:
        ping — heartbeat (respond with pong)
        botty.dismiss — user dismissed a nudge
        botty.helpful — user marked a nudge as helpful
        botty.search — user searches sessions
        botty.query — user asks Botty a question
    """
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from client")
                continue

            msg_type = data.get("type")

            try:
                if msg_type == "ping":
                    await ws.send_json({"type": "pong"})

                elif msg_type == "botty.dismiss":
                    nudge_id = data.get("nudge_id", "")
                    engine.dismiss_nudge(nudge_id)

                elif msg_type == "botty.helpful":
                    nudge_id = data.get("nudge_id", "")
                    engine.mark_helpful(nudge_id)

                elif msg_type == "botty.search":
                    query = data.get("query", "").strip()
                    if query:
                        results = await engine.search_sessions(query)
                        await ws.send_json(
                            {
                                "type": "botty.recall",
                                "results": results,
                            }
                        )

                elif msg_type == "botty.query":
                    text = data.get("text", "").strip()
                    if text:
                        # In a real system, this would invoke an LLM
                        logger.info("Botty query: %s", text)
                        # For now, just send status
                        await ws.send_json(engine.get_status())

                elif msg_type == "botty.status":
                    # Client explicitly requests status
                    await ws.send_json(engine.get_status())

            except Exception as exc:
                logger.warning("Error handling client message type %s: %s", msg_type, exc)

    except WebSocketDisconnect:
        logger.debug("Client disconnected from Botty WS")

    except Exception as exc:
        logger.warning("Client handler error: %s", exc)
