"""Memory policy — single source of truth for what each mode persists.

Modes fall into three tiers:

- NONE     — volatile/operational modes. Chat history persists so the user
  can reload, but nothing is written to conversation_memory, user_facts,
  or result_store.
- SESSION  — investigative modes. Session chat persists; cross-session
  semantic recall and fact extraction are disabled.
- FULL     — conversational modes. Everything persists, subject to the
  incognito flag and downstream secret redaction.

The ``should_*`` helpers below are the only places ws_endpoint, _hooks,
conversation_memory, and fact_extraction should consult when deciding to
write or read memory. Never duplicate the policy inline — if a new mode
appears, add it to ``MODE_TIERS`` and every call site gets it for free.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class MemoryTier(str, Enum):
    NONE = "none"
    SESSION = "session"
    FULL = "full"


# Mode name → tier. Keys must match whatever ``ws_endpoint._classify_mode``
# returns (including the ``custom:<agent_id>`` form). Anything not listed
# falls through to ``_DEFAULT_TIER``.
MODE_TIERS: dict[str, MemoryTier] = {
    # ---- Volatile / operational (Tier NONE) ----------------------------
    "monitor": MemoryTier.NONE,
    "scheduler": MemoryTier.NONE,
    "custom:docker-ops": MemoryTier.NONE,
    "custom:infra-health": MemoryTier.NONE,
    "custom:security-audit": MemoryTier.NONE,
    "custom:perf-analysis": MemoryTier.NONE,
    # ---- Investigative (Tier SESSION) ----------------------------------
    "agent": MemoryTier.SESSION,
    "research": MemoryTier.SESSION,
    "review": MemoryTier.SESSION,
    "sql": MemoryTier.SESSION,
    "logs": MemoryTier.SESSION,
    "discover": MemoryTier.SESSION,
    "web_search": MemoryTier.SESSION,
    # ---- Conversational (Tier FULL) ------------------------------------
    "search": MemoryTier.FULL,  # @qdrant (+ @docs/@find aliases)
    "chat": MemoryTier.FULL,  # default LLM
    "pipeline": MemoryTier.FULL,
}

# Unknown modes — including any new custom agent that hasn't been tiered —
# default to SESSION. That keeps session chat working while refusing to
# write anything cross-session until the tier is deliberately set.
_DEFAULT_TIER = MemoryTier.SESSION

# Warn once per unknown mode so we notice drift without spamming.
_warned_modes: set[str] = set()


def register_mode_tier(mode: str, tier: MemoryTier) -> None:
    """Register/override a mode's memory tier at runtime.

    Used by the custom-agent loader so an agent can opt out of cross-session
    memory (``no_history: true``) without hardcoding its mode here — keeps
    private/deployment-specific agents out of this table.
    """
    MODE_TIERS[mode] = tier


def get_tier(mode: str) -> MemoryTier:
    """Return the memory tier for *mode*, defaulting to SESSION if unknown."""
    mode = mode or ""
    if mode.startswith(("custom:connector:", "custom:connector-account:")):
        return MemoryTier.NONE
    tier = MODE_TIERS.get(mode)
    if tier is not None:
        return tier
    if mode and mode not in _warned_modes:
        _warned_modes.add(mode)
        logger.warning(
            "memory_policy: unknown mode %r, defaulting to %s. Add it to MODE_TIERS.",
            mode,
            _DEFAULT_TIER.value,
        )
    return _DEFAULT_TIER


# ---------------------------------------------------------------------------
# Write-side gates
# ---------------------------------------------------------------------------


def should_store_conversation(mode: str, *, incognito: bool) -> bool:
    """Qdrant conversation_memory — only FULL tier, and never incognito."""
    if incognito:
        return False
    return get_tier(mode) == MemoryTier.FULL


def should_extract_facts(mode: str, *, incognito: bool) -> bool:
    """SQLite user_facts — only FULL tier, and never incognito.

    Secret redaction is layered on top inside fact_extraction itself.
    """
    if incognito:
        return False
    return get_tier(mode) == MemoryTier.FULL


def should_store_result(mode: str, *, incognito: bool) -> bool:
    """Redis result_store — anything except NONE tier, and never incognito.

    Session-scoped by design, so SESSION tier is allowed (agent result is
    reusable within the same session). NONE stays silent.
    """
    if incognito:
        return False
    return get_tier(mode) != MemoryTier.NONE


# ---------------------------------------------------------------------------
# Read-side gates
# ---------------------------------------------------------------------------


def should_recall_conversation(mode: str, *, incognito: bool) -> bool:
    """Inject prior exchanges from Qdrant — only FULL tier, never incognito."""
    if incognito:
        return False
    return get_tier(mode) == MemoryTier.FULL


def should_inject_facts(mode: str, *, incognito: bool) -> bool:
    """Inject user_facts — FULL and SESSION tiers, never incognito.

    SESSION gets facts because preferences ("save to ~/Downloads") are
    useful context for investigative work without being cross-session
    recall. NONE-tier volatile modes skip to avoid leaking preferences
    into audit-sensitive outputs.
    """
    if incognito:
        return False
    return get_tier(mode) != MemoryTier.NONE
