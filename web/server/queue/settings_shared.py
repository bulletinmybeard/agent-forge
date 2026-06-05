"""SAQ worker settings for the shared queue (`agentforge:shared`).

Replaces the old ``settings_local`` / ``settings_remote`` pair. One module,
both hosts. The queue is shared across roles — agent jobs, monitor checks,
and scheduled commands compete between local and remote workers, same as before.

Functions are registered unconditionally; runtime guards inside each job
skip work that doesn't belong to the host. The cron schedule
(``prune_memory_saq``) is registered unconditionally too — SAQ's
``unique=True`` keeps a single host from running the same minute twice.

Run::

    saq -v web.server.queue.settings_shared.settings

Set ``AGENTFORGE_WORKER_ROLE`` so cross-role tool dispatch knows where this
worker lives (``mac``, ``ally``, ...).
"""

from __future__ import annotations

import logging

from saq import CronJob

from web.server.queue.jobs_saq import (
    execute_tool_saq,
    prune_memory_saq,
    run_agent_job_saq,
    run_monitor_check_saq,
    run_scheduled_command_saq,
)
from web.server.queue.queues import get_queue

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    """Initialise per-worker shared state.

    Pre-load the SearchRuntime so the first agent job doesn't pay a 5-10s
    cold start. Stored in ``ctx["runtime"]`` so jobs read it instead of
    triggering an in-job lazy load.
    """
    from agentforge.tools.routing import my_role

    role = my_role()
    logger.info("Shared SAQ worker starting up (role=%s) — pre-loading SearchRuntime", role)
    try:
        from web.server.ws_endpoint import SearchRuntime

        ctx["runtime"] = SearchRuntime()
        logger.info("Shared SAQ worker ready (role=%s, SearchRuntime preloaded)", role)
    except Exception as exc:
        logger.warning("SearchRuntime preload failed (jobs will lazy-load): %s", exc)


async def shutdown(ctx: dict) -> None:
    logger.info("Shared SAQ worker shutting down")


async def before_process(ctx: dict) -> None:
    job = ctx.get("job")
    if job:
        logger.info("Starting job: %s (key=%s)", job.function, job.key)


async def after_process(ctx: dict) -> None:
    job = ctx.get("job")
    if job:
        logger.info("Completed job: %s (status=%s)", job.function, job.status)


settings = {
    "queue": get_queue(),
    "functions": [
        run_agent_job_saq,
        execute_tool_saq,
        run_monitor_check_saq,
        run_scheduled_command_saq,
        prune_memory_saq,
    ],
    # Nightly retention sweep at 04:00 (worker TZ). ``unique=True`` keeps the
    # competing-consumer setup from running the same sweep twice.
    "cron_jobs": [
        CronJob(prune_memory_saq, cron="0 4 * * *", unique=True),
    ],
    "concurrency": 4,
    "startup": startup,
    "shutdown": shutdown,
    "before_process": before_process,
    "after_process": after_process,
}
