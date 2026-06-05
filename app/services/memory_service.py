"""Conversation memory for AgentForge chat sessions.

Adapted from py-ai-agent-system MemoryManager. Stores Q&A pairs per
session so the response refiner can pick up on prior conversation
context (e.g., "write a SQL query for *that*" without re-specifying
the source or table).

Each message stores minimal metadata — just enough for sticky filters
and conversational continuity.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ChatMessage:
    """A single conversation turn."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChatMessage":
        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
        )


class MemoryManager:
    """Session-based conversation memory with optional persistence."""

    def __init__(
        self,
        max_tokens: int | None = None,
        persistence_path: str | None = None,
        persistent: bool = True,
    ):
        self.max_tokens = max_tokens or settings.memory.max_tokens
        self.persistence_path = persistence_path or settings.memory.persistence_path
        self.persistent = persistent

        self.sessions: dict[str, list[ChatMessage]] = {}
        self.lock = threading.RLock()

        if self.persistent:
            self._load_from_disk()

        logger.info(
            "MemoryManager initialised (max_tokens=%d, persistent=%s, path=%s)",
            self.max_tokens,
            self.persistent,
            self.persistence_path,
        )

    def add_message(self, session_id: str, message: ChatMessage) -> None:
        """Append a message to a session and auto-truncate."""
        with self.lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = []

            self.sessions[session_id].append(message)
            self._truncate_session(session_id)

            if self.persistent:
                self._save_to_disk()

    def get_history(self, session_id: str, limit: int | None = None) -> list[ChatMessage]:
        """Return conversation history (newest last)."""
        with self.lock:
            if session_id not in self.sessions:
                return []
            history = self.sessions[session_id]
            if limit:
                return history[-limit:]
            return history.copy()

    def get_context_window(
        self,
        session_id: str,
        max_messages: int | None = None,
    ) -> list[dict[str, str]]:
        """Build a token-aware context window for the LLM.

        Returns a list of {"role": ..., "content": ...} dicts,
        most recent first, that fit within self.max_tokens.
        """
        history = self.get_history(session_id)
        if not history:
            return []

        messages: list[dict[str, str]] = []
        current_tokens = 0

        for msg in reversed(history):
            msg_tokens = len(msg.content) // 4  # rough estimate
            if current_tokens + msg_tokens > self.max_tokens:
                break
            messages.insert(0, {"role": msg.role, "content": msg.content})
            current_tokens += msg_tokens
            if max_messages and len(messages) >= max_messages:
                break

        return messages

    def clear_session(self, session_id: str) -> None:
        """Remove all messages for a session."""
        with self.lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                if self.persistent:
                    self._save_to_disk()

    def get_session_stats(self, session_id: str) -> dict[str, Any]:
        """Basic stats for debugging / /history command."""
        history = self.get_history(session_id)
        if not history:
            return {"exists": False}
        return {
            "exists": True,
            "message_count": len(history),
            "estimated_tokens": sum(len(m.content) // 4 for m in history),
            "start_time": history[0].timestamp.isoformat(),
            "last_message_time": history[-1].timestamp.isoformat(),
        }

    def _truncate_session(self, session_id: str) -> None:
        """Sliding window: drop oldest messages when over token budget."""
        if session_id not in self.sessions:
            return
        messages = self.sessions[session_id]
        total_tokens = sum(len(m.content) // 4 for m in messages)
        while total_tokens > self.max_tokens and len(messages) > 1:
            removed = messages.pop(0)
            total_tokens -= len(removed.content) // 4

    def _save_to_disk(self) -> None:
        try:
            Path(self.persistence_path).parent.mkdir(parents=True, exist_ok=True)
            serialized = {sid: [m.to_dict() for m in msgs] for sid, msgs in self.sessions.items()}
            with open(self.persistence_path, "w") as f:
                json.dump(serialized, f, indent=2)
            logger.debug("Saved %d session(s) to %s", len(self.sessions), self.persistence_path)
        except Exception as e:
            logger.error("Failed to save memory: %s", e)

    def _load_from_disk(self) -> None:
        try:
            if not os.path.exists(self.persistence_path):
                logger.info("No persisted memory found at %s", self.persistence_path)
                return
            with open(self.persistence_path) as f:
                serialized = json.load(f)
            for sid, msgs_data in serialized.items():
                self.sessions[sid] = [ChatMessage.from_dict(m) for m in msgs_data]
            logger.info("Loaded %d session(s) from %s", len(self.sessions), self.persistence_path)
        except Exception as e:
            logger.error("Failed to load memory: %s", e)


memory_manager = MemoryManager()
