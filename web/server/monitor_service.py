"""Monitor service — APScheduler-backed website change detection.

Manages monitor jobs (create, update, delete, enable/disable) with a
CronTrigger.  Each check fetches the target URL, extracts content,
compares against the stored baseline, and notifies on changes.

Architecture mirrors ``scheduler_service.py``:
  - APScheduler ``BackgroundScheduler`` with a thread pool
  - Jobs stored in ``monitor_jobs`` SQLite table (our own model)
  - On startup, all enabled jobs loaded from DB and registered
  - Check execution dispatched to the worker (host-side) for web access
"""

from __future__ import annotations

import concurrent.futures
import datetime
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from .database import ChatDatabase

logger = logging.getLogger(__name__)


def _monitor_config() -> dict:
    """Load the ``monitor`` section from config.yaml (cached after first call)."""
    if not hasattr(_monitor_config, "_cache"):
        import yaml

        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            _monitor_config._cache = cfg.get("monitor", {})
        except Exception:
            _monitor_config._cache = {}
    return _monitor_config._cache


def _effective_extraction_mode(job_mode: str) -> str:
    """Return the extraction mode to use, respecting ``always_use_vision``."""
    if _monitor_config().get("always_use_vision", False):
        return "vision"
    return job_mode


def _normalize_uuid(raw: str) -> str:
    """Normalize a UUID string — strip whitespace & convert unicode dashes.

    LLMs frequently emit non-ASCII hyphens (en-dash U+2013, em-dash U+2014,
    non-breaking hyphen U+2011, minus sign U+2212, figure dash U+2012) when
    reproducing UUIDs.  This converts them all to plain ASCII hyphens so the
    DB lookup succeeds.
    """

    # Replace common dash-like codepoints with ASCII hyphen
    _DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
    cleaned = raw.strip()
    for ch in _DASH_CHARS:
        cleaned = cleaned.replace(ch, "-")
    # Also strip any stray backticks the LLM might wrap around the UUID
    cleaned = cleaned.strip("`'\"\u200b\u200c\u200d\ufeff")
    return cleaned


class MonitorService:
    """Manages monitor job lifecycle and APScheduler integration."""

    def __init__(self, db: ChatDatabase) -> None:
        self.db = db
        self._scheduler = BackgroundScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        )
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load all enabled monitor jobs from DB and start the scheduler."""
        if self._started:
            return

        jobs = self.db.list_monitor_jobs(enabled_only=True)
        for job in jobs:
            self._add_apscheduler_job(job.id, job.cron)

        self._scheduler.start()
        self._started = True
        logger.info(
            "MonitorService started — %d enabled job(s) loaded",
            len(jobs),
        )

    def shutdown(self) -> None:
        """Gracefully shut down the monitor scheduler."""
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("MonitorService shut down")

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def create_job(
        self,
        label: str,
        url: str,
        original_prompt: str | None = None,
        extraction_mode: str = "text",
        css_selector: str | None = None,
        structured_selectors: dict | None = None,
        cron: str = "",
        cron_human: str | None = None,
        notification_method: str = "terminal-notifier",
        webhook_url: str | None = None,
        enabled: bool = True,
    ) -> dict:
        """Create a new monitor job and schedule initial snapshot in a thread.

        Returns the job dict.  The initial snapshot is taken in a background
        thread to avoid Playwright's sync-in-asyncio conflict — the extractor
        uses the sync Playwright API and must not run on the asyncio event loop.
        """
        job_id = str(uuid.uuid4())

        trigger = self._parse_cron(cron)
        if trigger is None:
            raise ValueError(f"Invalid cron expression: {cron}")

        # Validate extraction mode
        if extraction_mode not in ("text", "markdown", "rendered", "vision"):
            raise ValueError(f"Invalid extraction mode: {extraction_mode}")

        db_job = self.db.create_monitor_job(
            job_id=job_id,
            label=label,
            url=url,
            original_prompt=original_prompt,
            extraction_mode=extraction_mode,
            css_selector=css_selector,
            structured_selectors=structured_selectors,
            cron=cron,
            cron_human=cron_human,
            notification_method=notification_method,
            webhook_url=webhook_url,
            enabled=enabled,
        )

        # Take initial snapshot in a separate thread so Playwright sync API
        # doesn't collide with the running asyncio event loop.
        snapshot_result: dict = {"error": "Snapshot not yet taken"}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self._take_snapshot,
                    job_id,
                    url,
                    extraction_mode,
                    css_selector,
                    original_prompt,
                    structured_selectors,
                )
                snapshot_result = future.result(timeout=60)
        except Exception as exc:
            snapshot_result = {"error": str(exc)}
            logger.warning("Initial snapshot thread failed for %s: %s", url, exc)

        if enabled and self._started:
            self._add_apscheduler_job(job_id, cron, run_now=True)

        logger.info(
            "Monitor job created: %s — %s (%s, mode=%s)",
            job_id,
            label,
            cron,
            extraction_mode,
        )

        result = db_job.to_dict()
        result["initial_snapshot"] = snapshot_result
        return result

    def update_job(self, job_id: str, **fields) -> dict | None:
        """Update a monitor job.  Reschedules if cron or enabled changed."""
        job_id = _normalize_uuid(job_id)
        # Resolve potentially-fabricated IDs
        resolved = self._resolve_to_db_job(job_id)
        if resolved:
            job_id = resolved.id

        if "cron" in fields:
            trigger = self._parse_cron(fields["cron"])
            if trigger is None:
                raise ValueError(f"Invalid cron expression: {fields['cron']}")

        if "extraction_mode" in fields and fields["extraction_mode"] not in ("text", "markdown", "rendered", "vision"):
            raise ValueError(f"Invalid extraction mode: {fields['extraction_mode']}")

        db_job = self.db.update_monitor_job(job_id, **fields)
        if db_job is None:
            return None

        if self._started:
            self._remove_apscheduler_job(job_id)
            if db_job.enabled:
                self._add_apscheduler_job(job_id, db_job.cron)

        logger.info("Monitor job updated: %s", job_id)
        return db_job.to_dict()

    def delete_job(self, job_id: str) -> bool:
        """Delete a monitor job, its scheduler entry, and screenshot files."""
        job_id = _normalize_uuid(job_id)
        # Resolve potentially-fabricated IDs
        resolved = self._resolve_to_db_job(job_id)
        if resolved:
            job_id = resolved.id

        if self._started:
            self._remove_apscheduler_job(job_id)
        deleted = self.db.delete_monitor_job(job_id)
        if deleted:
            logger.info("Monitor job deleted: %s", job_id)
            # Clean up *all* screenshot files for this job from disk.
            # Uses a glob on the job_id prefix so it catches both DB-tracked
            # check screenshots and the initial snapshot screenshot (check_id=0)
            # which is never stored in a DB row.
            self._cleanup_screenshot_files(job_id)
        return deleted

    def _cleanup_screenshot_files(self, job_id: str) -> None:
        """Remove all screenshot files for a monitor job from disk.

        Screenshot filenames follow the pattern ``{job_id[:8]}_*.png``.
        This covers both check screenshots (stored in DB) and the initial
        snapshot screenshot (``check_id=0``, not stored in any DB row).
        """
        screenshot_dir = Path(__file__).resolve().parents[2] / "data" / "uploads" / "monitor" / "screenshots"
        if not screenshot_dir.is_dir():
            return
        prefix = job_id[:8]
        removed = 0
        for fp in screenshot_dir.glob(f"{prefix}_*.png"):
            try:
                fp.unlink()
                removed += 1
            except OSError as exc:
                logger.debug("Could not remove screenshot %s: %s", fp, exc)
        if removed:
            logger.info("Removed %d screenshot file(s) for deleted monitor job %s", removed, job_id)

    def list_jobs(self) -> list[dict]:
        """List all monitor jobs."""
        jobs = self.db.list_monitor_jobs()
        return [j.to_dict() for j in jobs]

    def get_job(self, job_id: str) -> dict | None:
        """Get a single monitor job.

        First tries exact UUID match.  If not found, falls back to fuzzy
        matching by label substring or URL substring — this handles the common
        case where the LLM fabricates a UUID instead of copying the real one.
        """
        job_id = _normalize_uuid(job_id)
        job = self.db.get_monitor_job(job_id)
        if job:
            return job.to_dict()

        # Fuzzy fallback — search all jobs for label/URL substring match
        resolved = self._resolve_job_id(job_id)
        if resolved:
            return resolved.to_dict()
        return None

    def _resolve_job_id(self, job_id: str):
        """Try to resolve a possibly-wrong job_id to a real MonitorJob.

        LLMs frequently fabricate UUIDs instead of copying the real ones.
        This method searches by:
          1. Exact ID match (already handled by caller)
          2. URL containing a recognisable product/page ID from the fake UUID
          3. Label substring match (case-insensitive)
        """
        all_jobs = self.db.list_monitor_jobs()
        if not all_jobs:
            return None

        # Strip non-hex chars from the fake ID to extract any product number
        clean_id = job_id.replace("-", "").replace("_", "").lstrip("0")

        for job in all_jobs:
            # Check if the fake UUID digits appear in the job's URL
            # (LLMs often derive fake UUIDs from product IDs in URLs)
            if clean_id and len(clean_id) >= 6 and clean_id in job.url.replace("-", ""):
                logger.info(
                    "Fuzzy-resolved monitor ID '%s' → '%s' (%s) via URL match",
                    job_id,
                    job.id,
                    job.label,
                )
                return job

        # Try label substring (case-insensitive)
        lower_id = job_id.lower().replace("-", " ").replace("_", " ")
        for job in all_jobs:
            if lower_id in job.label.lower() or job.label.lower() in lower_id:
                logger.info(
                    "Fuzzy-resolved monitor ID '%s' → '%s' (%s) via label match",
                    job_id,
                    job.id,
                    job.label,
                )
                return job

        return None

    def get_job_checks(self, job_id: str, limit: int = 20) -> list[dict]:
        """Get recent checks for a job."""
        job_id = _normalize_uuid(job_id)
        checks = self.db.get_monitor_checks(job_id, limit=limit)
        return [c.to_dict() for c in checks]

    def _resolve_to_db_job(self, job_id: str):
        """Get a MonitorJob ORM object by exact ID or fuzzy fallback."""
        job = self.db.get_monitor_job(job_id)
        if job:
            return job
        return self._resolve_job_id(job_id)

    def check_now(self, job_id: str) -> dict | None:
        """Trigger an immediate check for a monitor job.

        For ``rendered`` mode, dispatches to the worker on the host where
        Playwright is installed.  For ``text``/``markdown``, runs locally in a
        thread to avoid Playwright sync-in-asyncio conflict.

        Returns the check result dict, or None if job not found.
        """
        job_id = _normalize_uuid(job_id)
        job = self._resolve_to_db_job(job_id)
        if not job:
            return None
        # Use the real ID from here on
        job_id = job.id

        # For rendered mode, prefer the worker (host-side Playwright)
        if job.extraction_mode == "rendered":
            check = self.db.add_monitor_check(job_id=job_id, status="running")
            try:
                from web.server.queue.dispatch_compat import enqueue_monitor_check

                enqueue_monitor_check(job_id, check.id)
                logger.info("check_now dispatched to host worker: %s (check %d)", job.label, check.id)
                # Return immediately — the worker will update the check record.
                # The UI won't get the result inline, but the check will run.
                return {"status": "dispatched", "check_id": check.id, "note": "Check dispatched to host worker"}
            except Exception:
                logger.warning("Host worker dispatch failed for check_now %s — falling back to local", job.label)
                return self._execute_check_locally(job, check)

        # text/markdown modes can run inside Docker
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._execute_check, job_id)
                return future.result(timeout=120)
        except Exception as exc:
            logger.exception("check_now failed for %s: %s", job_id, exc)
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # Initial snapshot
    # ------------------------------------------------------------------

    def _take_snapshot(
        self,
        job_id: str,
        url: str,
        extraction_mode: str,
        css_selector: str | None,
        original_prompt: str | None = None,
        structured_selectors: dict | None = None,
    ) -> dict:
        """Take the initial baseline snapshot for a new monitor job.

        For ``rendered`` mode, falls back to ``text`` mode if Playwright is
        unavailable (e.g., inside Docker).  The scheduled checks will use the
        worker on the host where Playwright is installed.
        """
        from .monitor_extractors import (
            capture_check_screenshot,
            extract,
            extract_structured,
            save_screenshot_b64,
        )

        upload_dir = str(Path(__file__).resolve().parents[2] / "data" / "uploads")
        screenshot_path: str | None = None

        # Extract content — request a screenshot from the sidecar in the same
        # browser session so we get a screenshot of the real page, not a bot wall.
        effective_mode = _effective_extraction_mode(extraction_mode)
        result = extract(
            url,
            mode=effective_mode,
            css_selector=css_selector,
            original_prompt=original_prompt,
            screenshot=True,
        )

        # Save sidecar screenshot if available; fall back to separate local capture.
        if result.get("screenshot_b64"):
            try:
                screenshot_path = save_screenshot_b64(
                    screenshot_b64=result["screenshot_b64"],
                    job_id=job_id,
                    check_id=0,
                    upload_dir=upload_dir,
                )
            except Exception as exc:
                logger.debug("Sidecar screenshot save failed: %s", exc)

        if not screenshot_path:
            try:
                screenshot_path = capture_check_screenshot(
                    url=url,
                    job_id=job_id,
                    check_id=0,
                    upload_dir=upload_dir,
                )
            except Exception as exc:
                logger.warning("Initial screenshot failed for %s: %s", url, exc)

        # If rendered mode failed due to Playwright, fall back to text for
        # the initial snapshot.  Scheduled checks via the worker will use
        # the original mode.
        if "error" in result and extraction_mode == "rendered":
            logger.info(
                "Rendered extraction failed for initial snapshot — falling back to text mode for %s",
                url,
            )
            result = extract(url, mode="text", css_selector=css_selector)

        if "error" in result:
            logger.warning("Initial snapshot failed for %s: %s", url, result["error"])
            return {"error": result["error"], "screenshot_path": screenshot_path}

        # Step 1b: Structured multi-selector extraction (if configured)
        structured_content: dict | None = None
        if structured_selectors:
            try:
                structured_content = extract_structured(
                    url=url,
                    structured_selectors=structured_selectors,
                    mode=effective_mode,
                    original_prompt=original_prompt,
                )
                logger.info(
                    "Structured extraction for %s: %s",
                    url,
                    {k: v[:40] if v else None for k, v in structured_content.items()},
                )
            except Exception as exc:
                logger.warning("Structured extraction failed for %s: %s", url, exc)

        snap = self.db.create_monitor_snapshot(
            job_id=job_id,
            content=result["content"],
            content_hash=result["content_hash"],
            extraction_mode=extraction_mode,
            css_selector_used=css_selector,
            word_count=result.get("word_count"),
            structured_content=structured_content,
        )

        logger.info(
            "Initial snapshot stored for %s: hash=%s, words=%d",
            url,
            result["content_hash"][:12],
            result.get("word_count", 0),
        )

        return {
            "snapshot_id": snap.id,
            "content_hash": result["content_hash"],
            "word_count": result.get("word_count", 0),
            "structured_content": structured_content,
            "screenshot_path": screenshot_path,
        }

    # ------------------------------------------------------------------
    # Check execution (called by APScheduler in a thread)
    # ------------------------------------------------------------------

    def _execute_job(self, job_id: str) -> None:
        """Called by APScheduler — dispatch check to the worker."""
        job = self.db.get_monitor_job(job_id)
        if not job:
            logger.error("Monitor job %s not found — skipping", job_id)
            return

        if not job.enabled:
            logger.info("Monitor job %s is disabled — skipping", job_id)
            return

        logger.info("Executing monitor check: %s (%s)", job.label, job.url)

        check = self.db.add_monitor_check(job_id=job_id, status="running")

        # Dispatch to the worker
        try:
            from web.server.queue.dispatch_compat import enqueue_monitor_check

            enqueue_monitor_check(job_id, check.id)
            logger.info(
                "Monitor check %s dispatched to host worker (check %d)",
                job.label,
                check.id,
            )
        except Exception as exc:
            # For rendered/vision modes, don't fall back to local — Playwright
            # isn't available inside Docker.  Mark the check as errored instead.
            effective_mode = _effective_extraction_mode(job.extraction_mode)
            if effective_mode in ("rendered", "vision"):
                logger.error(
                    "Worker dispatch failed for rendered monitor %s — cannot run locally: %s",
                    job.label,
                    exc,
                )
                self.db.complete_monitor_check(
                    check_id=check.id,
                    status="error",
                    error=f"Worker unavailable — rendered mode requires host-side Playwright. "
                    f"Is the worker running? Error: {exc}",
                    duration_s=0,
                )
            else:
                logger.warning(
                    "Worker dispatch failed for monitor %s — running locally",
                    job.label,
                )
                self._execute_check_locally(job, check)

    def _execute_check(self, job_id: str) -> dict:
        """Execute a monitor check synchronously (for check_now)."""
        job = self.db.get_monitor_job(job_id)
        if not job:
            return {"error": "Job not found"}

        check = self.db.add_monitor_check(job_id=job_id, status="running")
        return self._execute_check_locally(job, check)

    def _execute_check_locally(self, job, check) -> dict:
        """Run a monitor check locally (inside Docker or fallback)."""
        from .monitor_differ import compute_diff, generate_heuristic_summary, quick_check
        from .monitor_extractors import (
            capture_check_screenshot,
            compute_structured_diff,
            extract,
            extract_structured,
            save_screenshot_b64,
        )
        from .monitor_notifier import notify

        start = time.monotonic()
        upload_dir = str(Path(__file__).resolve().parents[2] / "data" / "uploads")
        structured_selectors = getattr(job, "structured_selectors", None)

        try:
            # Step 1: Extract current content — request a screenshot from the
            # sidecar in the same browser session.  This avoids launching a
            # separate Playwright session (which would hit bot walls again).
            effective_mode = _effective_extraction_mode(job.extraction_mode)
            result = extract(
                job.url,
                mode=effective_mode,
                css_selector=job.css_selector,
                original_prompt=getattr(job, "original_prompt", None),
                screenshot=True,
            )

            # Step 1a: Save the screenshot — prefer the sidecar's screenshot
            # (taken from the real page), fall back to a separate local capture
            # only if the sidecar didn't provide one.
            screenshot_path = None
            if result.get("screenshot_b64"):
                try:
                    screenshot_path = save_screenshot_b64(
                        screenshot_b64=result["screenshot_b64"],
                        job_id=job.id,
                        check_id=check.id,
                        upload_dir=upload_dir,
                    )
                except Exception as exc:
                    logger.debug("Sidecar screenshot save failed: %s", exc)

            if not screenshot_path:
                try:
                    screenshot_path = capture_check_screenshot(
                        url=job.url,
                        job_id=job.id,
                        check_id=check.id,
                        upload_dir=upload_dir,
                    )
                except Exception as exc:
                    logger.debug("Audit screenshot failed (non-fatal): %s", exc)
            if "error" in result:
                duration = time.monotonic() - start
                self.db.complete_monitor_check(
                    check_id=check.id,
                    status="error",
                    error=result["error"],
                    duration_s=duration,
                    screenshot_path=screenshot_path,
                )
                return {"status": "error", "error": result["error"]}

            current_content = result["content"]
            current_hash = result["content_hash"]

            # Step 1b: Structured multi-selector extraction (if configured)
            structured_content: dict | None = None
            if structured_selectors:
                try:
                    structured_content = extract_structured(
                        url=job.url,
                        structured_selectors=structured_selectors,
                        mode=effective_mode,
                        original_prompt=getattr(job, "original_prompt", None),
                    )
                except Exception as exc:
                    logger.warning("Structured extraction failed: %s", exc)

            # Step 2: Get previous snapshot
            prev_snap = self.db.get_latest_snapshot(job.id)
            if not prev_snap:
                # No previous snapshot — store this as baseline
                self.db.create_monitor_snapshot(
                    job_id=job.id,
                    content=current_content,
                    content_hash=current_hash,
                    extraction_mode=job.extraction_mode,
                    css_selector_used=job.css_selector,
                    word_count=result.get("word_count"),
                    structured_content=structured_content,
                )
                duration = time.monotonic() - start
                self.db.complete_monitor_check(
                    check_id=check.id,
                    status="unchanged",
                    current_hash=current_hash,
                    duration_s=duration,
                    screenshot_path=screenshot_path,
                )
                return {"status": "unchanged", "note": "First check — baseline stored"}

            # Step 3: Quick hash comparison
            # Also check structured content for field-level changes even if
            # the full-page hash didn't change (targeted selectors may differ)
            structured_diff_result = None
            if structured_selectors and structured_content:
                prev_structured = getattr(prev_snap, "structured_content", None)
                structured_diff_result = compute_structured_diff(prev_structured, structured_content)

            hash_changed = quick_check(prev_snap.content_hash, current_hash)

            if not hash_changed and not structured_diff_result:
                duration = time.monotonic() - start
                self.db.complete_monitor_check(
                    check_id=check.id,
                    status="unchanged",
                    prev_hash=prev_snap.content_hash,
                    current_hash=current_hash,
                    duration_s=duration,
                    screenshot_path=screenshot_path,
                )
                return {"status": "unchanged"}

            # Step 4: Content changed — compute diff
            diff = compute_diff(prev_snap.content, current_content)
            summary = generate_heuristic_summary(diff, job.url)

            # Enhance summary with structured field changes
            if structured_diff_result:
                field_changes = []
                for field, change in structured_diff_result.items():
                    old_v = change.get("old") or "(empty)"
                    new_v = change.get("new") or "(empty)"
                    field_changes.append(f"{field}: {old_v} → {new_v}")
                if field_changes:
                    summary = "Field changes: " + "; ".join(field_changes) + ". " + (summary or "")

            # Step 5: Store new snapshot
            self.db.create_monitor_snapshot(
                job_id=job.id,
                content=current_content,
                content_hash=current_hash,
                extraction_mode=job.extraction_mode,
                css_selector_used=job.css_selector,
                word_count=result.get("word_count"),
                structured_content=structured_content,
            )

            # Prune old snapshots
            self.db.delete_old_snapshots(job.id, keep_count=10)

            # Step 6: Complete check record
            duration = time.monotonic() - start
            self.db.complete_monitor_check(
                check_id=check.id,
                status="changed",
                prev_hash=prev_snap.content_hash,
                current_hash=current_hash,
                diff_summary=summary,
                diff_lines_added=diff.lines_added,
                diff_lines_removed=diff.lines_removed,
                structured_diff=structured_diff_result,
                duration_s=duration,
                screenshot_path=screenshot_path,
            )

            # Step 7: Notify
            notify(
                label=job.label,
                url=job.url,
                status="changed",
                diff_summary=summary,
                notification_method=job.notification_method,
                webhook_url=job.webhook_url,
            )

            logger.info(
                "Monitor check: %s CHANGED (+%d/-%d lines, %.1fs)",
                job.label,
                diff.lines_added,
                diff.lines_removed,
                duration,
            )

            return {
                "status": "changed",
                "diff_summary": summary,
                "structured_diff": structured_diff_result,
                "lines_added": diff.lines_added,
                "lines_removed": diff.lines_removed,
                "duration_s": round(duration, 2),
            }

        except Exception as exc:
            duration = time.monotonic() - start
            self.db.complete_monitor_check(
                check_id=check.id,
                status="error",
                error=str(exc),
                duration_s=duration,
                screenshot_path=screenshot_path,
            )
            logger.exception("Monitor check failed for %s: %s", job.label, exc)
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # APScheduler helpers
    # ------------------------------------------------------------------

    def _add_apscheduler_job(self, job_id: str, cron: str, run_now: bool = False) -> None:
        """Register a monitor job with APScheduler.

        If *run_now* is True, the first execution fires immediately (within
        a few seconds) and then continues on the regular cron schedule.
        This is used after job creation so the first real check runs right
        away instead of waiting for the next cron tick.
        """
        trigger = self._parse_cron(cron)
        if trigger is None:
            logger.error("Cannot schedule monitor %s — invalid cron: %s", job_id, cron)
            return

        kwargs: dict = {
            "func": self._execute_job,
            "trigger": trigger,
            "args": [job_id],
            "id": f"monitor_{job_id}",
            "replace_existing": True,
        }

        if run_now:
            # Schedule the first execution immediately (next scheduler tick)
            kwargs["next_run_time"] = datetime.datetime.now(tz=datetime.timezone.utc)

        self._scheduler.add_job(**kwargs)

    def _remove_apscheduler_job(self, job_id: str) -> None:
        """Remove a monitor job from APScheduler."""
        try:
            self._scheduler.remove_job(f"monitor_{job_id}")
        except Exception:
            pass

    @staticmethod
    def _parse_cron(cron_expr: str) -> CronTrigger | None:
        """Parse a 5-field cron expression into an APScheduler trigger."""
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

_service: MonitorService | None = None


def get_monitor_service() -> MonitorService:
    """Return the module-level MonitorService singleton."""
    if _service is None:
        raise RuntimeError("MonitorService not initialised — call init_monitor() first")
    return _service


def init_monitor(db: ChatDatabase) -> MonitorService:
    """Create and start the monitor service singleton.

    Also configures the Price AgentForge sidecar integration if the
    ``monitor.sidecar`` section exists in config.yaml.
    """
    global _service

    # Configure sidecar before starting the service
    sidecar_cfg = _monitor_config().get("sidecar", {})
    if sidecar_cfg.get("enabled", False) and sidecar_cfg.get("url"):
        from .monitor_extractors import configure_sidecar

        configure_sidecar(
            url=sidecar_cfg["url"],
            timeout=sidecar_cfg.get("timeout", 60),
        )

    _service = MonitorService(db)
    _service.start()
    return _service


def shutdown_monitor() -> None:
    """Shut down the monitor service."""
    global _service
    if _service is not None:
        _service.shutdown()
        _service = None
