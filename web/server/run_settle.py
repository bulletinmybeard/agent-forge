"""Wait out an in-flight chat run before the next prompt is classified.

A follow-up that interrupts the current turn has to land after that turn's
``agent.cancelled`` card. Cancelling the task and continuing immediately lets
the card arrive in the middle of the next turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

INLINE_RUN = "inline-run"
WAIT_JOB = "wait-job"


async def settle_inflight_run(
    task: asyncio.Task | None,
    *,
    cancel_inline: Callable[[], None],
    cancel_jobs: Callable[[], list[str]],
    cancelled_after: Callable[[], bool],
    emit_cancelled: Callable[[], Awaitable[None]],
    suppress_jobs: Callable[[list[str]], None],
    timeout: float = 8.0,
) -> None:
    """Stop the current run and return once its cancelled card is stored.

    Inline tasks are cancelled and awaited so their handler can persist the
    card. Worker wait-tasks are left running: ``cancel_jobs`` flips the job
    row and the waiter exits on its own. ``wait_for`` would cancel that
    waiter on timeout, so it is shielded. When no card shows up before
    *timeout*, ``emit_cancelled`` writes one and those job ids are suppressed
    so a late worker card is dropped.

    A finished task with a leftover job row is not a turn the user can see.
    Those ids are suppressed and no card is written.
    """
    cancel_inline()
    job_ids = cancel_jobs() or []
    task_running = task is not None and not task.done()
    if not task_running:
        if job_ids:
            suppress_jobs(job_ids)
        return

    wait_job = task.get_name() == WAIT_JOB
    if not wait_job:
        task.cancel()
    try:
        # wait_for cancels its awaitable on timeout. Shield the waiter so a
        # slow worker poll still observes CANCELLED and skips run.idle.
        awaited = asyncio.shield(task) if wait_job else task
        await asyncio.wait_for(awaited, timeout)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        logger.info("inflight run did not exit within %.1fs", timeout)
    except Exception:
        logger.debug("inflight run ended during settle", exc_info=True)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cancelled_after():
        if loop.time() >= deadline:
            await emit_cancelled()
            if job_ids:
                suppress_jobs(job_ids)
            return
        await asyncio.sleep(0.05)
