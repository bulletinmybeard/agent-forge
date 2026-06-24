import asyncio
from unittest.mock import MagicMock

from app.config import BottySettings
from web.server.botty_endpoint import BottyEngine


def _event() -> dict:
    return {
        "status": "success",
        "mode": "search",
        "tools_used": [],
        "query_preview": "remember what we did last time",
    }


def test_botty_analysis_interval_skips_intermediate_runs():
    cfg = BottySettings(
        enabled=True,
        analysis_interval=3,
        max_frequency_seconds=0,
        dismissal_cooldown_seconds=0,
    )
    engine = BottyEngine(MagicMock(), "s1", botty_settings=cfg)

    async def _run():
        n1 = await engine.on_run_completed(_event())
        n2 = await engine.on_run_completed(_event())
        n3 = await engine.on_run_completed(_event())
        return n1, n2, n3

    n1, n2, n3 = asyncio.run(_run())
    assert n1 == []
    assert n2 == []
    assert len(n3) >= 1


def test_botty_dismiss_enters_quiet_period():
    cfg = BottySettings(
        enabled=True,
        analysis_interval=1,
        max_frequency_seconds=0,
        dismissal_cooldown_seconds=60,
    )
    engine = BottyEngine(MagicMock(), "s1", botty_settings=cfg)

    async def _run():
        await engine.on_run_completed(_event())
        quiet = engine.dismiss_nudge("n1")
        after = await engine.on_run_completed(_event())
        return quiet, after

    quiet, after = asyncio.run(_run())
    assert quiet is not None
    assert quiet["type"] == "botty.quiet"
    assert after == []
