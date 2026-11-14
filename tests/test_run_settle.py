"""Interrupt settle waits for the cancelled card before the next prompt."""

import asyncio
import tempfile
from pathlib import Path

from web.server.queue.models import JobStatus
from web.server.queue.store import JobStore
from web.server.run_settle import INLINE_RUN, WAIT_JOB, settle_inflight_run


def _run(coro):
    return asyncio.run(coro)


def test_idle_task_does_not_emit_a_card():
    emitted = []
    suppressed = []

    async def scenario():
        async def done():
            return None

        task = asyncio.create_task(done(), name=INLINE_RUN)
        await task
        await settle_inflight_run(
            task,
            cancel_inline=lambda: None,
            cancel_jobs=lambda: ["stale-job"],
            cancelled_after=lambda: False,
            emit_cancelled=_record(emitted),
            suppress_jobs=suppressed.extend,
            timeout=0.2,
        )

    _run(scenario())
    assert emitted == []
    assert suppressed == ["stale-job"]


def test_inline_cancel_is_persisted_by_the_task():
    stored = []

    async def scenario():
        async def runner():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stored.append("cancelled")

        task = asyncio.create_task(runner(), name=INLINE_RUN)
        await asyncio.sleep(0)
        emitted = []
        await settle_inflight_run(
            task,
            cancel_inline=lambda: None,
            cancel_jobs=lambda: [],
            cancelled_after=lambda: bool(stored),
            emit_cancelled=_record(emitted),
            suppress_jobs=lambda _ids: None,
            timeout=2,
        )
        assert emitted == []

    _run(scenario())
    assert stored == ["cancelled"]


def test_wait_job_is_not_cancelled_when_the_card_is_late():
    emitted = []

    async def scenario():
        release = asyncio.Event()

        async def waiter():
            await release.wait()

        task = asyncio.create_task(waiter(), name=WAIT_JOB)
        await asyncio.sleep(0)
        await settle_inflight_run(
            task,
            cancel_inline=lambda: None,
            cancel_jobs=lambda: ["job-1"],
            cancelled_after=lambda: False,
            emit_cancelled=_record(emitted),
            suppress_jobs=lambda _ids: None,
            timeout=0.05,
        )
        assert not task.done()
        release.set()
        await task

    _run(scenario())
    assert emitted == ["emit"]


def test_worker_wait_synthesizes_a_card_and_suppresses_the_job():
    emitted = []
    suppressed = []

    async def scenario():
        release = asyncio.Event()

        async def waiter():
            await release.wait()

        task = asyncio.create_task(waiter(), name=WAIT_JOB)
        await asyncio.sleep(0)

        def cancel_jobs():
            release.set()
            return ["job-1"]

        await settle_inflight_run(
            task,
            cancel_inline=lambda: None,
            cancel_jobs=cancel_jobs,
            cancelled_after=lambda: False,
            emit_cancelled=_record(emitted),
            suppress_jobs=suppressed.extend,
            timeout=0.15,
        )

    _run(scenario())
    assert emitted == ["emit"]
    assert suppressed == ["job-1"]


def test_done_does_not_overwrite_cancelled():
    with tempfile.TemporaryDirectory() as tmp:
        store = JobStore(Path(tmp) / "jobs.db")
        store.create_job("j1", "s1", "q", "agent")
        store.update_status("j1", JobStatus.RUNNING)
        assert store.cancel_active_jobs("s1") == ["j1"]
        store.update_status("j1", JobStatus.DONE)
        store.update_status("j1", JobStatus.RUNNING)
        job = store.get_job("j1")
        assert job is not None
        assert job.status == JobStatus.CANCELLED


def _record(bucket):
    async def emit():
        bucket.append("emit")

    return emit
