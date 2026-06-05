"""Data models for the agent task queue."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class AgentJob:
    """Represents a single agent run enqueued for background execution."""

    job_id: str  # same as the queue task ID
    session_id: str
    query: str
    mode: str  # "agent", "web_search", "logs", "sql", "search", "custom:<id>", etc.
    status: JobStatus = JobStatus.PENDING
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
