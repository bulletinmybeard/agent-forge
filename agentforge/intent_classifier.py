"""Intent classifier — routes a user query to the appropriate execution mode.

Uses @prefix detection as a fast path, then falls back to LLM classification,
then to a simple keyword-based fallback if the LLM call fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_MODES = {
    # Original 6 — covered by routing.md sections + routing_test_set.yaml fixtures.
    "chat",
    "search",
    "web_search",
    "agent",
    "logs",
    "discover",
    # The LLM classifier needs vocabulary for these modes; without them,
    # prompts that should land in coding/review/etc. could only get there via
    # explicit @-prefix or sticky-mode carryover.
    # `routing.md` documents each one so the LLM can actually pick them.
    "coding",
    "review",
    "research",
    "sql",
    "scheduler",
    "monitor",
    "pipeline",
}

_PREFIX_MAP = {
    "@chat": "chat",
    "@qdrant": "search",
    "@docs": "search",  # alias and same mode as @qdrant (docs + older clients)
    "@find": "search",  # alias and same mode as @qdrant
    "@search": "web_search",
    "@web": "web_search",
    "@tooling": "agent",
    "@agent": "agent",
    "@tools": "agent",
    "@run": "agent",
    "@exec": "agent",
    "@logs": "logs",
    "@log": "logs",
    "@discover": "discover",
    "@discovery": "discover",
    "@investigate": "discover",
    # ws_endpoint.py already knows these prefixes via _strip_mode_prefix;
    # mirroring them here lets `classify_intent_fallback` route them
    # deterministically without an LLM round-trip when the WS layer isn't on
    # the path.
    "@coding": "coding",
    "@code": "coding",
    "@review": "review",
    "@research": "research",
    "@sql": "sql",
    "@scheduler": "scheduler",
    "@monitor": "monitor",
    "@pipeline": "pipeline",
}

_ROUTING_PROMPT_PATH = Path(__file__).parent / "prompts" / "routing.md"

_CUSTOM_AGENTS_PLACEHOLDER = "{{CUSTOM_AGENTS}}"


@dataclass
class RouteResult:
    mode: str
    reason: str
    source: str  # "prefix", "llm", or "fallback"


_ANYWHERE_PREFIXES = frozenset({"@qdrant", "@docs", "@find"})  # anywhere in query


def _detect_prefix(query: str) -> tuple[str, str] | None:
    lower = query.strip().lower()

    # Standard start-of-query prefix detection
    for prefix, mode in _PREFIX_MAP.items():
        if prefix in _ANYWHERE_PREFIXES:
            continue  # handled below
        if lower.startswith(prefix):
            stripped = query.strip()[len(prefix) :].strip()
            return mode, stripped

    # Anywhere-in-query detection (@qdrant / @docs / @find)
    for prefix in sorted(_ANYWHERE_PREFIXES, key=len, reverse=True):
        if prefix in lower:
            mode = _PREFIX_MAP[prefix]
            # Remove the prefix from wherever it appears
            idx = lower.index(prefix)
            raw = query.strip()
            stripped = (raw[:idx] + raw[idx + len(prefix) :]).strip()
            return mode, stripped

    return None


def _load_routing_prompt(
    custom_agents: list[dict] | None = None,
) -> str:
    """Read the routing system prompt and inline custom-agent metadata.

    ``custom_agents`` is a list of ``{"alias": "cloud", "description": "..."}``
    rows supplied by the caller from ``rt.custom_agents``. When provided,
    the ``{{CUSTOM_AGENTS}}`` placeholder gets replaced with one bullet
    per agent so the LLM can see what custom agents exist and what they
    do — letting it route an unprefixed prompt like "what's on my cloud storage"
    to ``custom:<agent>`` instead of falling back to ``agent``.

    Empty / missing list = the placeholder is replaced with a short
    "(no custom agents registered)" line. The prompt stays parseable
    either way.
    """
    text = _ROUTING_PROMPT_PATH.read_text()
    if _CUSTOM_AGENTS_PLACEHOLDER not in text:
        return text
    if not custom_agents:
        return text.replace(_CUSTOM_AGENTS_PLACEHOLDER, "(no custom agents registered)")
    lines: list[str] = []
    for entry in custom_agents:
        alias = (entry.get("alias") or "").strip()
        desc = (entry.get("description") or "").strip()
        if not alias:
            continue
        # Emit as "- `custom:<alias>` — <description>" so the LLM
        # knows the exact mode string it should emit AND the alias the
        # user would type if they typed it explicitly.
        if desc:
            lines.append(f"- `custom:{alias}` (alias `@{alias}`) — {desc}")
        else:
            lines.append(f"- `custom:{alias}` (alias `@{alias}`)")
    block = "\n".join(lines) if lines else "(no custom agents registered)"
    return text.replace(_CUSTOM_AGENTS_PLACEHOLDER, block)


async def _call_llm(
    system_prompt: str,
    user_message: str,
    profile_name: str | None = None,
) -> str:
    """Run the routing LLM with the named profile.

    ``profile_name`` is read from config by callers (see ws_endpoint.py's
    ``_classify_mode``). When provided, that exact profile is used. When
    None or unresolvable, falls back to ``"fast"`` then ``"cloud-light"``
    so a misconfigured key never breaks routing entirely.
    """
    from agentforge.client import AIClient  # noqa: PLC0415 — lazy import to avoid heavy deps at module load
    from agentforge.config import get_config  # noqa: PLC0415

    config = get_config()
    profile = None
    # Try the configured profile first, then the legacy fallback chain.
    candidates: list[str] = []
    if profile_name:
        candidates.append(profile_name)
    candidates.extend(["fast", "cloud-light"])
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        try:
            profile = config.get_profile(name)
            break
        except ValueError:
            continue
    if not profile:
        raise ValueError(f"No routing classifier profile configured. Tried: {candidates}")

    client = AIClient(profile=profile)

    def _sync_call() -> str:
        response = client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
        )
        return response.content

    return await asyncio.to_thread(_sync_call)


def classify_intent_fallback(query: str) -> RouteResult:
    prefix_result = _detect_prefix(query)
    if prefix_result:
        mode, _ = prefix_result
        return RouteResult(mode=mode, reason="prefix detected", source="prefix")
    return RouteResult(mode="chat", reason="default — general LLM knowledge", source="fallback")


async def classify_intent(
    query: str,
    conversation_history: list[dict] | None = None,
    fallback_fn: Callable[..., RouteResult] | None = None,
    heuristic_hint: tuple[str, str] | None = None,
    profile_name: str | None = None,
    custom_agents: list[dict] | None = None,
) -> RouteResult:
    """Classify a query, optionally accepting a heuristic hint.

    ``heuristic_hint`` is ``(mode, confidence)`` from the synchronous
    heuristic. When provided, it's surfaced to the LLM as a prior so
    the LLM can confirm a borderline pick or override on clear evidence.
    The LLM still emits its own verdict; the hint never short-circuits.

    ``profile_name`` selects which AI profile drives the LLM call. Read
    by ``ws_endpoint._classify_mode`` from ``routing.classifier.profile``
    and forwarded here so the routing model can be swapped without code
    changes (useful for A/B testing different models / providers for
    routing accuracy). Falls back to ``fast`` then ``cloud-light`` when
    the named profile is missing.

    ``custom_agents`` is a list of ``{"alias": str, "description": str}``
    rows from ``rt.custom_agents``. When supplied, the LLM can return
    ``custom:<alias>`` for prompts that match a custom agent's purpose
    (e.g., "what's on my cloud storage now" → ``custom:<agent>``), closing the
    gap where unprefixed live-data prompts used to fall to ``agent``.
    """
    _fallback = fallback_fn or classify_intent_fallback

    # Fast path: @prefix detection skips LLM entirely
    prefix_result = _detect_prefix(query)
    if prefix_result:
        mode, _ = prefix_result
        return RouteResult(mode=mode, reason="@prefix shortcut", source="prefix")

    # Build the set of modes the LLM is allowed to emit on this call.
    # Built-in modes are always valid; custom agents extend the set per
    # call so the prompt's "Custom agents" section stays in sync with
    # what we'll accept on parse.
    allowed_modes: set[str] = set(VALID_MODES)
    allowed_custom: dict[str, str] = {}  # alias → "custom:<alias>" for resolution
    if custom_agents:
        for entry in custom_agents:
            alias = (entry.get("alias") or "").strip()
            if not alias:
                continue
            allowed_modes.add(f"custom:{alias}")
            allowed_custom[alias] = f"custom:{alias}"

    # Build LLM input with conversation context
    try:
        system_prompt = _load_routing_prompt(custom_agents=custom_agents)

        user_message = ""
        if conversation_history:
            recent = conversation_history[-3:]
            for turn in recent:
                role = turn.get("role", "user")
                content = turn.get("content", "")
                user_message += f"[{role}]: {content}\n"
            user_message += f"\n[user]: {query}"
        else:
            user_message = query

        # Surface the heuristic hint to the LLM. The system prompt
        # documents how to use it (prior, not a command). Suppressed
        # when no hint is provided so the prompt stays stable for
        # cases where the heuristic had nothing to say.
        if heuristic_hint:
            hint_mode, hint_confidence = heuristic_hint
            user_message += f"\n\n[heuristic_hint]: mode={hint_mode} confidence={hint_confidence}"

        raw = await _call_llm(system_prompt, user_message, profile_name=profile_name)

        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        parsed = json.loads(cleaned)
        mode = parsed.get("mode", "").lower()
        reason = parsed.get("reason", "")

        # Some LLMs return "@cloud" or just "cloud" when they meant
        # a custom agent. Normalise to the canonical "custom:<alias>"
        # before the VALID_MODES check.
        if mode.startswith("@"):
            mode = mode[1:]
        if mode in allowed_custom:
            mode = allowed_custom[mode]

        if mode not in allowed_modes:
            logger.warning(
                "LLM returned unknown mode '%s' (allowed: built-in + %d custom), falling back",
                mode,
                len(allowed_custom),
            )
            return _fallback(query)

        return RouteResult(mode=mode, reason=reason, source="llm")

    except (json.JSONDecodeError, TimeoutError, ValueError, Exception) as exc:
        logger.warning("LLM routing failed (%s), using fallback", exc)
        return _fallback(query)
