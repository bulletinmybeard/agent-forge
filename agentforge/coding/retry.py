"""Verify-retry loop for ``@coding``.

After ``code_apply`` writes the first-pass diffs, this helper re-runs ``code_verify``
against on-disk state. If sites still match the ``reverify_pattern``.
Usually because the LLM saw a multi-line JSX element as several "context" lines around a single "edit-target" line
and missed one — we burst again on the surviving sites with an augmented instruction. Same ``burst_id`` across retries,
so ``code_undo`` reverts every pass in one go.

Lives in its own module because the loop has enough moving parts to deserve dedicated tests,
and the natural test surface is "inject fake transform/apply/verify callables
and assert the loop behaves." Putting it inside ``_run_coding`` made that essentially impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


@dataclass
class RetryResult:
    attempts: int = 0
    additional_applied: list[dict] = field(default_factory=list)
    surviving_sites: list[dict] = field(default_factory=list)
    file_diff_events: list[dict] = field(default_factory=list)


def _build_retry_hits(surviving_sites: list[dict]) -> list[dict]:
    return [
        {
            "file": s["file"],
            "line": s["line"],
            "text": s["text"],
            "ctx_before": [],
            "ctx_after": [],
        }
        for s in surviving_sites
    ]


def _augment_instruction(base: str, surviving_sites: list[dict]) -> str:
    miss_list = "\n".join(f"  {s['file']}:{s['line']}: {s['text']}" for s in surviving_sites)
    return (
        base + f"\n\nIMPORTANT: a previous attempt missed {len(surviving_sites)} "
        f"site(s). Edit ONLY these lines:\n" + miss_list
    )


def run_verify_retry(
    *,
    applied: list[dict],
    instruction: str,
    reverify_pattern: str | None,
    reverify_path: str,
    reverify_glob: str,
    burst_id: str,
    session_id: str,
    max_retries: int,
    base_profile: str,
    retry_profile: str,
    transform_fn: Callable[..., list[dict]],
    apply_fn: Callable[..., dict],
    verify_fn: Callable[..., dict],
) -> RetryResult:
    """Run the post-apply verify-retry loop synchronously.

    Caller wraps this in ``asyncio.to_thread`` from the runner.
    Returns a ``RetryResult`` summarising what happened — callers emit file.diff
    events themselves from ``result.additional_applied`` so the runner keeps full control of the protocol layer.
    """
    out = RetryResult()
    if not reverify_pattern or max_retries <= 0 or not applied:
        return out

    current_applied = list(applied)

    while out.attempts < max_retries:
        verify = verify_fn(
            [{"file": e["file"]} for e in current_applied],
            reverify_pattern,
            reverify_path,
            reverify_glob,
        )
        if verify.get("ok") or not verify.get("surviving_sites"):
            return out

        surviving = verify["surviving_sites"]
        is_final = out.attempts == max_retries - 1
        use_profile = retry_profile if is_final else base_profile

        retry_hits = _build_retry_hits(surviving)
        retry_instruction = _augment_instruction(instruction, surviving)

        proposed = transform_fn(retry_hits, retry_instruction, use_profile)
        applicable = [p for p in proposed if not p.get("error") and p.get("unified_diff")]
        if not applicable:
            # Transform produced nothing useful — stop retrying so we
            # don't loop on a model that's stuck.
            logger.info(
                "[coding.retry] attempt %d produced no applicable diffs — stopping",
                out.attempts + 1,
            )
            break

        apply_out = apply_fn(applicable, burst_id=burst_id, session_id=session_id)
        out.attempts += 1
        extra = list(apply_out.get("applied") or [])
        out.additional_applied.extend(extra)
        current_applied.extend(extra)

    # Final verify so callers can surface the dead-letter list.
    final = verify_fn(
        [{"file": e["file"]} for e in current_applied],
        reverify_pattern,
        reverify_path,
        reverify_glob,
    )
    out.surviving_sites = list(final.get("surviving_sites") or [])
    return out


__all__ = ["RetryResult", "run_verify_retry"]
