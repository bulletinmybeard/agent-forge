"""SQLite-backed job store for agent tasks.

Tracks job lifecycle (pending → running → done/error/cancelled) so that
agentforge-web can check whether a session has an active run on reconnect.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from web.server.queue.models import AgentJob, JobStatus

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
JOB_STORE_DB = _DATA_DIR / "job_store.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id       TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    query        TEXT NOT NULL,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    error        TEXT,
    overrides    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_session  ON agent_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status   ON agent_jobs(status);
"""


class JobStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or JOB_STORE_DB
        self._lock = Lock()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        return conn

    # ── Create ──────────────────────────────────────────────────────

    def create_job(
        self,
        job_id: str,
        session_id: str,
        query: str,
        mode: str,
        overrides: dict[str, Any] | None = None,
    ) -> AgentJob:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO agent_jobs
                   (job_id, session_id, query, mode, status, created_at, overrides)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    session_id,
                    query,
                    mode,
                    JobStatus.PENDING.value,
                    now,
                    json.dumps(overrides) if overrides else None,
                ),
            )
        return AgentJob(
            job_id=job_id,
            session_id=session_id,
            query=query,
            mode=mode,
            status=JobStatus.PENDING,
            created_at=datetime.fromisoformat(now),
            overrides=overrides or {},
        )

    # ── Update ──────────────────────────────────────────────────────

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        error: str | None = None,
    ) -> None:
        updates = ["status = ?"]
        values: list[Any] = [status.value]

        if status == JobStatus.RUNNING:
            updates.append("started_at = ?")
            values.append(datetime.now(timezone.utc).isoformat())
        if status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
            updates.append("completed_at = ?")
            values.append(datetime.now(timezone.utc).isoformat())
        if error is not None:
            updates.append("error = ?")
            values.append(error)

        values.append(job_id)
        with self._lock, self._conn() as conn:
            conn.execute(
                f"UPDATE agent_jobs SET {', '.join(updates)} WHERE job_id = ?",
                values,
            )

    # ── Query ───────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> AgentJob | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def get_active_job(self, session_id: str) -> AgentJob | None:
        """Return the currently running or pending job for a session, if any."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM agent_jobs
                   WHERE session_id = ? AND status IN (?, ?)
                   ORDER BY created_at DESC LIMIT 1""",
                (session_id, JobStatus.PENDING.value, JobStatus.RUNNING.value),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def cancel_active_jobs(self, session_id: str) -> int:
        """Cancel all pending/running jobs for a session (e.g., user hit Stop)."""
        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                """UPDATE agent_jobs SET status = ?, completed_at = ?
                   WHERE session_id = ? AND status IN (?, ?)""",
                (
                    JobStatus.CANCELLED.value,
                    datetime.now(timezone.utc).isoformat(),
                    session_id,
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount

    def active_job_count(self, session_id: str) -> int:
        """Return the number of non-terminal jobs for a session without mutating state.

        Mirrors the filter used by cancel_active_jobs but does not cancel.
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM agent_jobs
                   WHERE session_id = ? AND status IN (?, ?)""",
                (session_id, JobStatus.PENDING.value, JobStatus.RUNNING.value),
            ).fetchone()
        return int(row["n"]) if row else 0

    def cleanup_old_jobs(self, hours: int = 24) -> int:
        """Delete completed/error/cancelled jobs older than *hours*."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                """DELETE FROM agent_jobs
                   WHERE status IN (?, ?, ?) AND completed_at < ?""",
                (
                    JobStatus.DONE.value,
                    JobStatus.ERROR.value,
                    JobStatus.CANCELLED.value,
                    cutoff,
                ),
            )
            return cursor.rowcount

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> AgentJob:
        return AgentJob(
            job_id=row["job_id"],
            session_id=row["session_id"],
            query=row["query"],
            mode=row["mode"],
            status=JobStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            error=row["error"],
            overrides=json.loads(row["overrides"]) if row["overrides"] else {},
        )


# Module-level singleton
job_store = JobStore()
