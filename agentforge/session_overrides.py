from __future__ import annotations

from dataclasses import replace
from typing import Any

from .config import AIProfile


def apply_session_overrides(
    profile: AIProfile,
    overrides: dict[str, Any] | None,
    *,
    profile_key: str | None = None,
) -> AIProfile:
    """Return *profile* with session overrides merged in (or *profile* unchanged).

    Lookup order for the per-profile map:
      1. ``profile_key`` if given (explicit role used for this client)
      2. ``profile.name`` (resolved profile name)

    Flat ``overrides.model`` / ``temperature`` / ``max_tokens`` apply last
    and win when present (legacy single-model chat override).
    """
    if not overrides:
        return profile

    patch: dict[str, Any] = {}

    profiles_map = overrides.get("profiles")
    if isinstance(profiles_map, dict):
        entry = None
        if profile_key and profile_key in profiles_map:
            entry = profiles_map[profile_key]
        elif profile.name in profiles_map:
            entry = profiles_map[profile.name]
        if isinstance(entry, dict):
            _merge_entry(patch, entry)

    # Flat keys (chat-style single override) — applied after per-profile
    if overrides.get("model"):
        patch["model"] = str(overrides["model"])
    if overrides.get("temperature") is not None:
        try:
            patch["temperature"] = float(overrides["temperature"])
        except (TypeError, ValueError):
            pass
    if overrides.get("max_tokens") is not None:
        try:
            patch["max_tokens"] = int(overrides["max_tokens"])
        except (TypeError, ValueError):
            pass

    if not patch:
        return profile
    return replace(profile, **patch)


def _merge_entry(patch: dict[str, Any], entry: dict[str, Any]) -> None:
    if entry.get("model"):
        patch["model"] = str(entry["model"])
    if entry.get("temperature") is not None:
        try:
            patch["temperature"] = float(entry["temperature"])
        except (TypeError, ValueError):
            pass
    if entry.get("max_tokens") is not None:
        try:
            patch["max_tokens"] = int(entry["max_tokens"])
        except (TypeError, ValueError):
            pass
