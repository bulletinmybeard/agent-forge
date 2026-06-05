"""SAQ worker settings for per-role tools queues.

Reads ``AGENTFORGE_WORKER_ROLE`` and binds to that role's queue from
``tool_routing.yaml``. Replaces the old ``settings_local_tools`` and
``settings_remote_tools`` modules.

Registers ``execute_tool_saq`` (cross-role tool dispatch) and
``run_agent_job_saq`` (so mode-pinned agent jobs like ``@coding`` can land
on a specific role's queue without spawning a third worker process).

Run::

    AGENTFORGE_WORKER_ROLE=mac  saq -v web.server.queue.settings_tools.settings
    AGENTFORGE_WORKER_ROLE=ally saq -v web.server.queue.settings_tools.settings
"""

from __future__ import annotations

import logging

from web.server.queue.jobs_saq import execute_tool_saq, run_agent_job_saq
from web.server.queue.queues import get_tool_queue_for_role

logger = logging.getLogger(__name__)


def _build_settings() -> dict:
    """Resolve the worker's role at import time and bind to its queue.

    Settings dicts are read once when ``saq`` boots, so this runs in the
    worker process where ``AGENTFORGE_WORKER_ROLE`` is set.
    """
    from agentforge.tools.routing import my_role

    role = my_role()
    queue = get_tool_queue_for_role(role)
    logger.info("Tools SAQ worker role=%s queue=%s", role, queue.name)

    async def startup(ctx: dict) -> None:
        logger.info("Tools SAQ worker (role=%s) starting up — pre-loading SearchRuntime", role)
        try:
            from web.server.ws_endpoint import SearchRuntime

            ctx["runtime"] = SearchRuntime()
            logger.info("Tools SAQ worker (role=%s) ready", role)
        except Exception as exc:
            logger.warning("SearchRuntime preload failed (jobs will lazy-load): %s", exc)

    async def shutdown(ctx: dict) -> None:
        logger.info("Tools SAQ worker (role=%s) shutting down", role)

    async def before_process(ctx: dict) -> None:
        job = ctx.get("job")
        if job:
            logger.info("Starting job: %s (key=%s)", job.function, job.key)

    async def after_process(ctx: dict) -> None:
        job = ctx.get("job")
        if job:
            logger.info("Completed job: %s (status=%s)", job.function, job.status)

    return {
        "queue": queue,
        "functions": [execute_tool_saq, run_agent_job_saq],
        "concurrency": 4,
        "startup": startup,
        "shutdown": shutdown,
        "before_process": before_process,
        "after_process": after_process,
    }


settings = _build_settings()
