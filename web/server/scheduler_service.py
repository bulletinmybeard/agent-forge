"""Scheduler service — APScheduler-backed dynamic job scheduling.

Manages scheduled jobs (create, update, delete, enable/disable) with a
CronTrigger and executes them as subprocess shell commands.  Each run is
recorded in the ``scheduled_job_runs`` table so the UI can display
execution history and failure notifications.

Architecture:
  - APScheduler ``BackgroundScheduler`` with a thread pool (not async —
    commands are blocking subprocess calls)
  - Jobs are stored in the SQLite ``scheduled_jobs`` table (our own model,
    *not* APScheduler's built-in SQLAlchemy job store — gives us full
    control over the schema and avoids APScheduler's pickle serialisation)
  - On startup, all enabled jobs are loaded from DB and registered with
    APScheduler
  - The service exposes sync methods that can be called from async FastAPI
    handlers via ``asyncio.get_event_loop().run_in_executor(...)``
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from .database import ChatDatabase

logger = logging.getLogger(__name__)

# Maximum output stored per run (chars).  Prevents a single noisy job from
# bloating the DB.
_MAX_OUTPUT_CHARS = 50_000

# Command execution timeout — matches the global 10-minute floor.
_COMMAND_TIMEOUT = 600


class SchedulerService:
    """Manages APScheduler lifecycle and scheduled job execution."""

    def __init__(self, db: ChatDatabase) -> None:
        self.db = db
        self._scheduler = BackgroundScheduler(
            job_defaults={
                "coalesce": True,  # merge missed runs into one
                "max_instances": 1,  # no overlapping runs of the same job
                "misfire_grace_time": 300,  # 5 min grace for missed triggers
            },
        )
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load all enabled jobs from DB and start the scheduler."""
        if self._started:
            return

        jobs = self.db.list_scheduled_jobs(enabled_only=True)
        for job in jobs:
            self._add_apscheduler_job(job.id, job.cron)

        self._scheduler.start()
        self._started = True
        logger.info(
            "SchedulerService started — %d enabled job(s) loaded",
            len(jobs),
        )

    def shutdown(self) -> None:
        """Gracefully shut down the scheduler."""
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("SchedulerService shut down")

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def create_job(
        self,
        label: str,
        command: str,
        cron: str,
        cron_human: str | None = None,
        on_failure: str = "notify",
        enabled: bool = True,
    ) -> dict:
        """Create a new scheduled job.  Returns the job dict."""
        job_id = str(uuid.uuid4())

        # Validate the cron expression by attempting to build a trigger
        trigger = self._parse_cron(cron)
        if trigger is None:
            raise ValueError(f"Invalid cron expression: {cron}")

        db_job = self.db.create_scheduled_job(
            job_id=job_id,
            label=label,
            command=command,
            cron=cron,
            cron_human=cron_human,
            on_failure=on_failure,
            enabled=enabled,
        )

        if enabled and self._started:
            self._add_apscheduler_job(job_id, cron)

        logger.info(
            "Scheduled job created: %s — %s (%s)",
            job_id,
            label,
            cron,
        )
        return db_job.to_dict()

    def update_job(self, job_id: str, **fields) -> dict | None:
        """Update a scheduled job.  Reschedules if cron or enabled changed."""
        # If cron is being changed, validate first
        if "cron" in fields:
            trigger = self._parse_cron(fields["cron"])
            if trigger is None:
                raise ValueError(f"Invalid cron expression: {fields['cron']}")

        db_job = self.db.update_scheduled_job(job_id, **fields)
        if db_job is None:
            return None

        # Reschedule in APScheduler
        if self._started:
            self._remove_apscheduler_job(job_id)
            if db_job.enabled:
                self._add_apscheduler_job(job_id, db_job.cron)

        logger.info("Scheduled job updated: %s", job_id)
        return db_job.to_dict()

    def delete_job(self, job_id: str) -> bool:
        """Delete a scheduled job and remove it from APScheduler."""
        if self._started:
            self._remove_apscheduler_job(job_id)
        deleted = self.db.delete_scheduled_job(job_id)
        if deleted:
            logger.info("Scheduled job deleted: %s", job_id)
        return deleted

    def list_jobs(self) -> list[dict]:
        """List all scheduled jobs."""
        jobs = self.db.list_scheduled_jobs()
        return [j.to_dict() for j in jobs]

    def get_job(self, job_id: str) -> dict | None:
        """Get a single scheduled job."""
        job = self.db.get_scheduled_job(job_id)
        return job.to_dict() if job else None

    def get_job_runs(self, job_id: str, limit: int = 20) -> list[dict]:
        """Get recent runs for a job."""
        runs = self.db.get_scheduled_job_runs(job_id, limit=limit)
        return [r.to_dict() for r in runs]

    # ------------------------------------------------------------------
    # Command vetting
    # ------------------------------------------------------------------

    def vet_command(self, command: str) -> dict:
        """Run the command through the safety guard.

        Returns a dict with 'safe' (bool) and 'verdict' (str) fields.
        Reuses the existing command_guard infrastructure.
        """
        try:
            from agentforge.tools.command_guard import get_guard

            guard = get_guard()
            verdict = guard.classify(command)
            return {
                "safe": verdict != "destructive",
                "verdict": verdict,
                "source": guard.last_source,
            }
        except Exception as exc:
            logger.warning("Command guard unavailable: %s — denying command", exc)
            return {"safe": False, "verdict": "unknown", "source": "unavailable"}

    # ------------------------------------------------------------------
    # Job execution (called by APScheduler in a thread)
    # ------------------------------------------------------------------

    def _execute_job(self, job_id: str) -> None:
        """Execute a scheduled job's command and record the result."""
        job = self.db.get_scheduled_job(job_id)
        if not job:
            logger.error("Scheduled job %s not found — skipping execution", job_id)
            return

        if not job.enabled:
            logger.info("Scheduled job %s is disabled — skipping", job_id)
            return

        logger.info("Executing scheduled job: %s (%s)", job.label, job.command)

        # Create a run record
        run = self.db.add_scheduled_job_run(job_id=job_id, status="running")

        # Dispatch to the native host worker so the command runs on macOS
        # (not inside Docker). The worker gives access to terminal-notifier,
        # SSH keys, brew, Docker CLI, etc.
        try:
            from web.server.queue.dispatch_compat import enqueue_scheduled_command

            enqueue_scheduled_command(job_id, run.id, job.command)
            logger.info(
                "Scheduled job %s dispatched to host worker (run %s)",
                job.label,
                run.id,
            )
        except Exception:
            # Fallback: run locally if the host worker is unavailable
            logger.warning(
                "Host worker dispatch failed for job %s — running locally",
                job.label,
            )
            self._execute_job_locally(job, run)

    def _execute_job_locally(self, job, run) -> None:
        """Fallback: execute a scheduled job command locally (inside Docker)."""
        start = time.monotonic()
        try:
            result = subprocess.run(
                job.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT,
            )
            duration = time.monotonic() - start
            status = "success" if result.returncode == 0 else "error"

            output = (result.stdout or "") + (result.stderr or "")
            if len(output) > _MAX_OUTPUT_CHARS:
                output = output[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"

            self.db.complete_scheduled_job_run(
                run_id=run.id,
                status=status,
                exit_code=result.returncode,
                output=output or None,
                error=result.stderr[:10000] if result.stderr and result.returncode != 0 else None,
                duration_s=duration,
            )

            if status == "error":
                logger.warning(
                    "Scheduled job %s failed (exit %d): %s",
                    job.label,
                    result.returncode,
                    (result.stderr or "")[:200],
                )
            else:
                logger.info(
                    "Scheduled job %s completed (%.1fs)",
                    job.label,
                    duration,
                )

        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            self.db.complete_scheduled_job_run(
                run_id=run.id,
                status="error",
                exit_code=-1,
                error=f"Command timed out after {_COMMAND_TIMEOUT}s",
                duration_s=duration,
            )
            logger.error("Scheduled job %s timed out after %ds", job.label, _COMMAND_TIMEOUT)

        except Exception as exc:
            duration = time.monotonic() - start
            self.db.complete_scheduled_job_run(
                run_id=run.id,
                status="error",
                exit_code=-1,
                error=str(exc),
                duration_s=duration,
            )
            logger.exception("Scheduled job %s crashed: %s", job.label, exc)

    # ------------------------------------------------------------------
    # APScheduler helpers
    # ------------------------------------------------------------------

    def _add_apscheduler_job(self, job_id: str, cron: str) -> None:
        """Register a job with APScheduler using a CronTrigger."""
        trigger = self._parse_cron(cron)
        if trigger is None:
            logger.error("Cannot schedule job %s — invalid cron: %s", job_id, cron)
            return

        self._scheduler.add_job(
            func=self._execute_job,
            trigger=trigger,
            args=[job_id],
            id=f"sched_{job_id}",
            replace_existing=True,
        )

    def _remove_apscheduler_job(self, job_id: str) -> None:
        """Remove a job from APScheduler (no-op if not found)."""
        try:
            self._scheduler.remove_job(f"sched_{job_id}")
        except Exception:
            pass  # Job wasn't registered — that's fine

    @staticmethod
    def _parse_cron(cron_expr: str) -> CronTrigger | None:
        """Parse a standard 5-field cron expression into an APScheduler trigger.

        Accepts: ``minute hour day_of_month month day_of_week``
        Returns None if the expression is invalid.
        """
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return None
        try:
            return CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: SchedulerService | None = None


def get_scheduler_service() -> SchedulerService:
    """Return the module-level SchedulerService singleton."""
    if _service is None:
        raise RuntimeError("SchedulerService not initialised — call init_scheduler() first")
    return _service


def init_scheduler(db: ChatDatabase) -> SchedulerService:
    """Create and start the scheduler service singleton."""
    global _service
    _service = SchedulerService(db)
    _service.start()
    return _service


def shutdown_scheduler() -> None:
    """Shut down the scheduler service."""
    global _service
    if _service is not None:
        _service.shutdown()
        _service = None
