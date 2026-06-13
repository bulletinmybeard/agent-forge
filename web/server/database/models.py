"""SQLAlchemy 2.0 models for chat sessions and messages.

Follows the same patterns as py-rentwatch-dev: naive local datetimes,
DeclarativeBase, cascade deletes, expunge-before-return.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


def _now() -> datetime:
    """Naive local datetime — avoids SQLite UTC conversion issues."""
    return datetime.now()


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True)  # UUIDv7 from client
    title = Column(String(255), nullable=False, default="New chat")
    profile = Column(String(50), nullable=True)  # last-used routed profile
    model = Column(String(100), nullable=True)  # last-used model name
    # Per-session AI provider override. Stamped at session creation, never
    # mutated afterwards — switching requires a new chat. NULL = use the
    # global default (singleton ConfigManager._provider_override).
    provider_override = Column(String(32), nullable=True)
    # Where the session originated. "web" = the Agent Chat UI; external clients
    # send their own value so the human sidebar can filter them out. Stamped at
    # creation, never mutated.
    source = Column(String(32), nullable=False, default="web")
    message_count = Column(Integer, nullable=False, default=0)
    # Cumulative token usage — updated after each model call
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.sequence",
    )

    def __repr__(self) -> str:
        return f"<ChatSession(id='{self.id}', title='{self.title}', messages={self.message_count})>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "profile": self.profile,
            "model": self.model,
            "provider_override": self.provider_override,
            "source": self.source or "web",
            "message_count": self.message_count,
            "prompt_tokens": self.prompt_tokens or 0,
            "completion_tokens": self.completion_tokens or 0,
            "total_tokens": self.total_tokens or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String(20), nullable=False)  # "user", "assistant", "system"
    type = Column(String(30), nullable=False)  # maps to frontend component type
    content = Column(Text, nullable=True)  # primary text (query or result)
    metadata_json = Column(Text, nullable=True)  # JSON blob for type-specific data
    tool_calls_json = Column(Text, nullable=True)  # JSON array of tool calls [{name, args}, ...]
    sequence = Column(Integer, nullable=False, default=0)
    is_incognito = Column(Boolean, nullable=False, default=False)
    is_volatile = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_now, nullable=False)

    session = relationship("ChatSession", back_populates="messages")

    def __repr__(self) -> str:
        return f"<ChatMessage(session='{self.session_id}', type='{self.type}', seq={self.sequence})>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "type": self.type,
            "content": self.content,
            "metadata": json.loads(self.metadata_json) if self.metadata_json else None,
            "tool_calls": json.loads(self.tool_calls_json) if self.tool_calls_json else None,
            "sequence": self.sequence,
            "is_incognito": self.is_incognito,
            "is_volatile": self.is_volatile,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserFact(Base):
    """A structured fact extracted from conversation (user preferences, system details, etc.)."""

    __tablename__ = "user_facts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fact_type = Column(String(30), nullable=False)  # "preference", "system", "entity", "decision"
    key = Column(String(255), nullable=False, unique=True)  # dedupe key, e.g., "preferred_download_dir"
    value = Column(Text, nullable=False)  # the fact content
    source_session = Column(String(36), nullable=True)  # session where fact was extracted
    confidence = Column(Float, nullable=False, default=0.8)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    def __repr__(self) -> str:
        return f"<UserFact(key='{self.key}', type='{self.fact_type}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "fact_type": self.fact_type,
            "key": self.key,
            "value": self.value,
            "source_session": self.source_session,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SessionInstruction(Base):
    """A user-authored instruction stored for a specific session or globally.

    Created via ``#remember <text>`` in chat.  Persists to SQLite so it
    survives page reloads and reconnects.  Injected as a
    ``[Your Instructions]`` system block before every LLM call.

    Scope:
      - ``session_id`` set  → session-scoped (only active for that session)
      - ``session_id`` None → global (injected for every session)
    """

    __tablename__ = "session_instructions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)

    def __repr__(self) -> str:
        scope = self.session_id or "global"
        return f"<SessionInstruction(scope='{scope}', text='{self.text[:40]}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "text": self.text,
            "scope": "session" if self.session_id else "global",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CommandNote(Base):
    """A saved 'command note' — all tool calls from a single agent run."""

    __tablename__ = "command_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(500), nullable=False, default="Untitled")
    commands_json = Column(Text, nullable=False)  # JSON array of {name, args} tool calls
    message_ts = Column(String(50), nullable=True)  # _ts of the tool_calls panel (for add/remove matching)
    created_at = Column(DateTime, default=_now, nullable=False)

    def __repr__(self) -> str:
        return f"<CommandNote(id={self.id}, title='{self.title[:40]}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "title": self.title,
            "commands": json.loads(self.commands_json) if self.commands_json else [],
            "message_ts": self.message_ts,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ScheduledJob(Base):
    """A user-defined scheduled job backed by APScheduler."""

    __tablename__ = "scheduled_jobs"

    id = Column(String(36), primary_key=True)  # UUID
    label = Column(String(500), nullable=False)
    command = Column(Text, nullable=False)
    cron = Column(String(100), nullable=False)  # standard cron expression
    cron_human = Column(String(255), nullable=True)  # human-readable schedule
    on_failure = Column(String(20), nullable=False, default="notify")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String(20), nullable=True)  # "success" | "error"

    runs = relationship(
        "ScheduledJobRun",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ScheduledJobRun.started_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<ScheduledJob(id='{self.id}', label='{self.label}', cron='{self.cron}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "command": self.command,
            "cron": self.cron,
            "cron_human": self.cron_human,
            "on_failure": self.on_failure,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_status": self.last_status,
        }


class ScheduledJobRun(Base):
    """A single execution record for a scheduled job."""

    __tablename__ = "scheduled_job_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("scheduled_jobs.id"), nullable=False)
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running")  # "running" | "success" | "error"
    exit_code = Column(Integer, nullable=True)
    output = Column(Text, nullable=True)  # stdout/stderr (truncated)
    error = Column(Text, nullable=True)
    duration_s = Column(Float, nullable=True)

    job = relationship("ScheduledJob", back_populates="runs")

    def __repr__(self) -> str:
        return f"<ScheduledJobRun(job='{self.job_id}', status='{self.status}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "error": self.error,
            "duration_s": round(self.duration_s, 2) if self.duration_s else None,
        }


# -- Website Monitor -----------------------------------------------------------


class Connection(Base):
    """An authenticated external service connection (Gmail, Drive, etc.)."""

    __tablename__ = "connections"

    id = Column(String(36), primary_key=True)
    connector_type = Column(String(50), nullable=False)
    label = Column(String(255), nullable=False)
    account_identifier = Column(String(255), nullable=True)
    encrypted_tokens = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    last_used_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    def __repr__(self) -> str:
        return f"<Connection(id='{self.id}', type='{self.connector_type}', label='{self.label}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "connector_type": self.connector_type,
            "label": self.label,
            "account_identifier": self.account_identifier,
            "status": self.status,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MonitorJob(Base):
    """A website monitoring job — periodic content change detection."""

    __tablename__ = "monitor_jobs"

    id = Column(String(36), primary_key=True)  # UUID
    label = Column(String(500), nullable=False)
    url = Column(String(2048), nullable=False)
    original_prompt = Column(Text, nullable=True)  # user's NL request — used by vision fallback
    extraction_mode = Column(String(20), nullable=False, default="text")  # "text" | "markdown" | "rendered" | "vision"
    css_selector = Column(String(500), nullable=True)  # optional CSS selector to target (legacy single selector)
    structured_selectors = Column(JSON, nullable=True)  # named selectors: {"price": ".buy-block-price", "title": "h1"}
    cron = Column(String(100), nullable=False)
    cron_human = Column(String(255), nullable=True)
    notification_method = Column(
        String(30), nullable=False, default="terminal-notifier"
    )  # "terminal-notifier" | "webhook" | "both"
    webhook_url = Column(String(2048), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    last_check_at = Column(DateTime, nullable=True)
    last_status = Column(String(20), nullable=True)  # "unchanged" | "changed" | "error"

    snapshots = relationship(
        "MonitorSnapshot",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="MonitorSnapshot.created_at.desc()",
    )
    checks = relationship(
        "MonitorCheck",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="MonitorCheck.started_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<MonitorJob(id='{self.id}', label='{self.label}', url='{self.url}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "original_prompt": self.original_prompt,
            "extraction_mode": self.extraction_mode,
            "css_selector": self.css_selector,
            "structured_selectors": self.structured_selectors,
            "cron": self.cron,
            "cron_human": self.cron_human,
            "notification_method": self.notification_method,
            "webhook_url": self.webhook_url,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
            "last_status": self.last_status,
        }


class MonitorSnapshot(Base):
    """A stored content snapshot for a monitored page."""

    __tablename__ = "monitor_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("monitor_jobs.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)  # cleaned/normalized content (flat text or combined)
    content_hash = Column(String(64), nullable=False)  # SHA-256
    structured_content = Column(
        JSON, nullable=True
    )  # per-field extraction: {"price": "€ 719,-", "title": "iPhone 17e"}
    extraction_mode = Column(String(20), nullable=False)
    css_selector_used = Column(String(500), nullable=True)
    word_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)

    job = relationship("MonitorJob", back_populates="snapshots")

    def __repr__(self) -> str:
        return f"<MonitorSnapshot(job='{self.job_id}', hash='{self.content_hash[:12]}...')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "content_hash": self.content_hash,
            "structured_content": self.structured_content,
            "extraction_mode": self.extraction_mode,
            "css_selector_used": self.css_selector_used,
            "word_count": self.word_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class MonitorCheck(Base):
    """A single execution record for a monitor check."""

    __tablename__ = "monitor_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("monitor_jobs.id", ondelete="CASCADE"), nullable=False)
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running")  # "running" | "unchanged" | "changed" | "error"
    prev_hash = Column(String(64), nullable=True)
    current_hash = Column(String(64), nullable=True)
    diff_summary = Column(Text, nullable=True)  # human-readable change description
    diff_lines_added = Column(Integer, nullable=True)
    diff_lines_removed = Column(Integer, nullable=True)
    structured_diff = Column(JSON, nullable=True)  # per-field changes: {"price": {"old": "€ 719,-", "new": "€ 699,-"}}
    error = Column(Text, nullable=True)
    duration_s = Column(Float, nullable=True)
    screenshot_path = Column(String(500), nullable=True)  # relative path to PNG screenshot

    job = relationship("MonitorJob", back_populates="checks")

    def __repr__(self) -> str:
        return f"<MonitorCheck(job='{self.job_id}', status='{self.status}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status,
            "prev_hash": self.prev_hash,
            "current_hash": self.current_hash,
            "diff_summary": self.diff_summary,
            "diff_lines_added": self.diff_lines_added,
            "diff_lines_removed": self.diff_lines_removed,
            "structured_diff": self.structured_diff,
            "error": self.error,
            "duration_s": round(self.duration_s, 2) if self.duration_s else None,
            "screenshot_path": self.screenshot_path,
        }
