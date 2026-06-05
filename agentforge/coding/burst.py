"""Parallel LLM-burst helper for coding-mode transforms.

A burst is N independent prompts fired against the same profile in parallel,
bounded by a per-provider concurrency cap. This is what makes ``@coding`` fast: discovery + narrowing are free,
and the only expensive stage (per-file transforms) fans out across the provider's available concurrency
instead of serializing through one chat loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class BurstResult:
    index: int  # position in the input list
    content: str  # LLM response text (empty on error)
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# Concurrency cap resolution
# ---------------------------------------------------------------------------


def _resolve_max_workers(
    profile: str,
    explicit: int | None,
    max_files_per_burst: int | None = None,
) -> int:
    """Pick a worker count for the burst.

    Precedence:
      1. ``explicit`` arg (caller override, e.g., from a test)
      2. ``coding.max_workers_by_provider.<active_provider>`` — dedicated
         per-provider cap for @coding. Agent runs and coding runs hit
         different model profiles, so they deserve separate knobs — a
         burst of 20 small-diff calls has a very different concurrency
         profile than one long agent session.
      3. ``parallel.max_workers_by_provider`` — the generic fallback.
      4. Hardcoded default of 4.

    Also clamps to ``max_files_per_burst`` when set, so a pathologically
    wide config can't spin up more workers than there are files.
    """
    if explicit and explicit > 0:
        return _clamp(explicit, max_files_per_burst)

    try:
        from agentforge.config import get_config

        cfg = get_config()
        cap = cfg.get_by_provider("coding", "max_workers", default=None)
        if cap is None:
            cap = cfg.get_by_provider("parallel", "max_workers", default=4)
        if cap and cap > 0:
            return _clamp(int(cap), max_files_per_burst)
    except Exception as exc:
        logger.debug("[coding.burst] provider-cap lookup failed: %s", exc)
    _ = profile  # kept in signature for forward-compat / log context
    return _clamp(4, max_files_per_burst)


def _clamp(n: int, ceiling: int | None) -> int:
    if ceiling and ceiling > 0 and n > ceiling:
        return int(ceiling)
    return int(n)


# ---------------------------------------------------------------------------
# Burst runner
# ---------------------------------------------------------------------------


async def run_burst_async(
    items: list[Any],
    build_prompt: Callable[[Any], list[dict]],
    *,
    profile: str = "coding",
    max_workers: int | None = None,
    max_files_per_burst: int | None = None,
    client_factory: Callable[[str], Any] | None = None,
    temperature: float | None = None,
    on_item: Callable[..., None] | None = None,
) -> list[BurstResult]:
    """Fan ``build_prompt(item)`` out in parallel against ``profile``.

    Each item → one LLM call → one ``BurstResult``. Ordering is preserved
    (index is the position in ``items``). Failures are isolated — one
    item's error doesn't affect the rest.

    ``on_item`` is an optional progress callback. Fires as:
        on_item("start", index=<int>, total=<int>, item=<T>)
        on_item("done",  index=<int>, total=<int>, item=<T>, result=<BurstResult>)

    Used by the coding runner to push per-file progress into the UI's
    Tool Calls panel live as each burst lands. Exceptions from the
    callback are swallowed so a buggy UI hook never breaks the burst.

    Injecting ``client_factory`` makes the whole thing testable without
    patching globals: tests pass a stub that returns canned responses.
    """
    if not items:
        return []

    cap = _resolve_max_workers(profile, max_workers, max_files_per_burst)
    logger.info(
        "[coding.burst] fan-out: items=%d profile=%s cap=%d",
        len(items),
        profile,
        cap,
    )

    # Lazy import — tests provide their own client_factory so we don't
    # drag AIClient's transitive deps into unrelated test paths.
    if client_factory is None:
        from agentforge.client import AIClient

        def _default_factory(prof: str):
            return AIClient(profile=prof)

        client_factory = _default_factory

    client = client_factory(profile)
    semaphore = asyncio.Semaphore(cap)
    total = len(items)

    def _fire(kind: str, **kw) -> None:
        if on_item is None:
            return
        try:
            on_item(kind, **kw)
        except Exception:
            logger.debug("[coding.burst] on_item %s raised", kind, exc_info=True)

    async def _one(index: int, item: Any) -> BurstResult:
        async with semaphore:
            _fire("start", index=index, total=total, item=item)
            messages = build_prompt(item)
            try:
                resp = await client.achat(messages, stream=False, temperature=temperature)
            except Exception as exc:
                logger.warning("[coding.burst] item %d failed: %s", index, exc)
                result = BurstResult(index=index, content="", error=str(exc))
                _fire("done", index=index, total=total, item=item, result=result)
                return result
            content = (getattr(resp, "content", "") or "").strip()
            result = BurstResult(
                index=index,
                content=content,
                prompt_tokens=getattr(resp, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(resp, "completion_tokens", 0) or 0,
            )
            _fire("done", index=index, total=total, item=item, result=result)
            return result

    results = await asyncio.gather(
        *(_one(i, item) for i, item in enumerate(items)),
        return_exceptions=False,
    )
    return list(results)


def run_burst(
    items: list[Any],
    build_prompt: Callable[[Any], list[dict]],
    *,
    profile: str = "coding",
    max_workers: int | None = None,
    max_files_per_burst: int | None = None,
    client_factory: Callable[[str], Any] | None = None,
    temperature: float | None = None,
) -> list[BurstResult]:
    """Synchronous wrapper — spins up a temporary event loop if one isn't running.

    Callers already inside an event loop (e.g., the runner in ``ws_endpoint._run_coding``)
    should ``await run_burst_async`` directly instead of calling this wrapper.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Use a fresh, throwaway loop so we don't close whatever loop
        # asyncio.get_event_loop() would otherwise return — that would
        # break callers in the same process that rely on the default loop.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                run_burst_async(
                    items,
                    build_prompt,
                    profile=profile,
                    max_workers=max_workers,
                    max_files_per_burst=max_files_per_burst,
                    client_factory=client_factory,
                    temperature=temperature,
                )
            )
        finally:
            loop.close()
    # Running loop — caller should use run_burst_async.
    raise RuntimeError("run_burst() called from inside an active event loop; use run_burst_async() instead.")


__all__ = [
    "BurstResult",
    "run_burst",
    "run_burst_async",
]
