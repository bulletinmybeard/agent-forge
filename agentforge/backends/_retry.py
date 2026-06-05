"""Retry classification + small retry helper for model calls.

This module centralises the logic for deciding whether a failed LLM call
should be retried, and provides a simple ``retry_call`` wrapper for the
non-agent call sites (title generation, fact extraction, compaction)
that previously had no retry at all.

The existing classification in ``framework/agent.py`` was **inverted** from
normal HTTP semantics: it excluded any exception containing the substring
``"500"`` from the retry set, under the comment "500 = model crash (not
transient)".  In practice HTTP 5xx is the most transient class of error
— it is exactly what retries exist for.  4xx errors (400 bad request, 413
payload too large, 422 unprocessable entity) are the ones that should
*not* be retried because the request itself is malformed and retrying
sends the same bad bytes.

Responsibilities of this module:

* :func:`extract_status_code` — pull an HTTP status code out of an
  exception regardless of whether it came from ``ollama.ResponseError``,
  ``httpx.HTTPStatusError``, or a plain ``Exception`` with a stringified
  status in the message.
* :func:`classify_model_error` — turn an exception into a
  :class:`RetryDecision` (``retryable``, ``status_code``, ``category``,
  ``user_message``) so both the agent loop and the sync helpers share
  the same policy.
* :func:`retry_call` — a tiny synchronous retry wrapper with exponential
  backoff and jitter for call sites that don't need the agent loop's
  thread-pool / cancel-event machinery.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

try:
    from chalkbox.logging.bridge import get_logger
except ImportError:  # pragma: no cover — fallback for tests that stub chalkbox
    import logging

    def get_logger(name: str):  # type: ignore[no-redef]
        return logging.getLogger(name)


logger = get_logger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Status code extraction
# ---------------------------------------------------------------------------

# Matches the tail of an ollama ResponseError stringification:
#     "Internal Server Error (ref: abc) (status code: 500)"
# or the start of an HTTPStatusError:
#     "Server error '500 Internal Server Error' for url '...'"
_STATUS_CODE_PATTERNS = (
    re.compile(r"\(status code:\s*(\d{3})\)", re.IGNORECASE),
    re.compile(r"\b(\d{3})\s+Internal Server Error\b", re.IGNORECASE),
    re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE),
    re.compile(r"\bstatus\s+code\s+(\d{3})\b", re.IGNORECASE),
)


def extract_status_code(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an exception.

    Tries, in order:

    1. ``exc.status_code`` — set by ``ollama.ResponseError``.
    2. ``exc.response.status_code`` — set by ``httpx.HTTPStatusError``.
    3. Regex patterns against ``str(exc)`` — last-resort fallback for
       wrapped exceptions that only preserve a stringified body.

    Returns ``None`` if no status code can be identified (e.g., a connection
    error or a plain exception with no code).  A returned value of ``-1``
    (which some clients use as "unknown") is normalised to ``None``.
    """
    # 1. Direct attribute (ollama.ResponseError)
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and code > 0:
        return code

    # 2. httpx.HTTPStatusError shape
    resp = getattr(exc, "response", None)
    if resp is not None:
        sub = getattr(resp, "status_code", None)
        if isinstance(sub, int) and sub > 0:
            return sub

    # 3. Regex fallback — only trust a match if the pattern explicitly
    # anchors the number to a status-code context (so "read 500 chars"
    # does NOT match). The compiled patterns above all require either
    # "status code:", "HTTP <nnn>", or "<nnn> Internal Server Error".
    s = str(exc)
    for pat in _STATUS_CODE_PATTERNS:
        m = pat.search(s)
        if m:
            try:
                code = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 100 <= code <= 599:
                return code
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Transient error substrings that indicate a temporary network / provider
# hiccup even when no HTTP status code is present. All matched case-insensitively.
_TRANSIENT_MARKERS = (
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "temporarily unavailable",
    "temporary failure",
    "timed out",
    "timeout",
    "read timeout",
    "eof occurred",
    "remote end closed",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "server disconnected",
)


@dataclass(frozen=True)
class RetryDecision:
    """The outcome of classifying an exception."""

    retryable: bool
    status_code: int | None
    category: str  # "5xx" | "4xx" | "transient_network" | "unknown"
    reason: str  # human-readable one-line explanation

    @property
    def is_5xx(self) -> bool:
        return self.status_code is not None and 500 <= self.status_code < 600

    @property
    def is_4xx(self) -> bool:
        return self.status_code is not None and 400 <= self.status_code < 500


def classify_model_error(exc: BaseException) -> RetryDecision:
    """Return a :class:`RetryDecision` for *exc*.

    Policy (applied in order):

    * HTTP 4xx → **not retryable** — the request itself is wrong.
    * HTTP 5xx → **retryable** — server-side transient failure.
    * "connection reset", "timed out", etc. in the message → **retryable**.
    * Anything else → **not retryable** — treat as hard failure.
    """
    code = extract_status_code(exc)
    msg = str(exc) or type(exc).__name__

    if code is not None and 400 <= code < 500:
        return RetryDecision(
            retryable=False,
            status_code=code,
            category="4xx",
            reason=f"{code} client error — request shape is wrong, retrying will not help",
        )

    if code is not None and 500 <= code < 600:
        return RetryDecision(
            retryable=True,
            status_code=code,
            category="5xx",
            reason=f"{code} server error — transient provider issue, will retry",
        )

    lowered = msg.lower()
    for marker in _TRANSIENT_MARKERS:
        if marker in lowered:
            return RetryDecision(
                retryable=True,
                status_code=code,
                category="transient_network",
                reason=f"network/transport hiccup ({marker}) — will retry",
            )

    return RetryDecision(
        retryable=False,
        status_code=code,
        category="unknown",
        reason=f"unclassified error ({type(exc).__name__}) — treating as hard failure",
    )


# ---------------------------------------------------------------------------
# User-facing error messages
# ---------------------------------------------------------------------------


def user_message_for(decision: RetryDecision, *, exc: BaseException | None = None) -> str:
    """Return the text that should be shown to the end-user after the
    retry budget is exhausted.

    Critically, this no longer says "too large or complex" for 5xx — that
    was the previous misdiagnosis that sent users chasing the wrong fix.
    """
    if decision.is_5xx:
        ref = _extract_ref_id(exc) if exc is not None else ""
        ref_part = f" Reference: {ref}." if ref else ""
        return (
            f"The AI model provider returned a {decision.status_code} server error "
            f"after multiple retries. This is usually a transient provider issue — "
            f"please try again in a moment.{ref_part}"
        )
    if decision.is_4xx:
        return (
            f"The AI model rejected the request ({decision.status_code}). "
            f"This usually means the request is too large, malformed, or "
            f"exceeds the model's context window. Please try a simpler request."
        )
    if decision.category == "transient_network":
        return (
            "The AI model call failed because of a network/connection issue. "
            "This is usually transient — please try again."
        )
    return (
        "The AI model encountered an error. Please try again, and if the problem "
        "persists simplify the request or switch models."
    )


_REF_ID_PATTERN = re.compile(r"ref:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.IGNORECASE)


def _extract_ref_id(exc: BaseException) -> str:
    """Pull an Ollama-cloud ``ref: UUID`` out of an exception message."""
    m = _REF_ID_PATTERN.search(str(exc))
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Backoff timing
# ---------------------------------------------------------------------------


def backoff_seconds(attempt: int, *, base: float = 1.0, cap: float = 8.0, jitter: float = 0.25) -> float:
    """Return the sleep duration before *attempt* (1-indexed).

    Implements exponential backoff ``base * 2**(attempt-1)`` clamped to
    ``cap``, plus +/- ``jitter`` fraction of uniform random noise.  With
    the defaults: ``attempt=1`` → ~1s, ``attempt=2`` → ~2s, ``attempt=3``
    → ~4s, never more than 8s.
    """
    if attempt < 1:
        attempt = 1
    expo = base * (2 ** (attempt - 1))
    expo = min(expo, cap)
    noise = expo * jitter * (2.0 * random.random() - 1.0)
    return max(0.1, expo + noise)


# ---------------------------------------------------------------------------
# retry_call — sync helper for the non-agent call sites
# ---------------------------------------------------------------------------


def retry_call(
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    on_retry: Callable[[int, RetryDecision, float], None] | None = None,
    context: str = "model-call",
    **kwargs: Any,
) -> T:
    """Call ``fn(*args, **kwargs)`` with automatic retry on transient errors.

    ``on_retry`` — optional callback ``(attempt, decision, sleep_seconds)``
    invoked right before each retry sleep. Lets callers emit structured
    events (e.g., ``agent.retry``) without coupling this helper to any
    event bus.

    Raises the original exception if:
      * the decision says it's not retryable, or
      * ``max_attempts`` is exhausted.
    """
    if max_attempts < 1:
        max_attempts = 1

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — we reraise below
            last_exc = exc
            decision = classify_model_error(exc)
            if not decision.retryable or attempt >= max_attempts:
                logger.warning(
                    "[%s] giving up after %d attempt(s): %s",
                    context,
                    attempt,
                    decision.reason,
                )
                raise

            sleep_s = backoff_seconds(attempt, base=base_delay, cap=max_delay)
            logger.info(
                "[%s] attempt %d/%d failed (%s) — retrying in %.1fs",
                context,
                attempt,
                max_attempts,
                decision.reason,
                sleep_s,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt, decision, sleep_s)
                except Exception:  # noqa: BLE001
                    logger.debug("[%s] on_retry callback raised", context, exc_info=True)
            time.sleep(sleep_s)

    # Unreachable: the loop either returns or raises, but mypy wants a sentinel
    assert last_exc is not None
    raise last_exc  # pragma: no cover
