"""Per-provider settings under ``tools.web_search.<provider>``.

Each provider block carries its own ``enabled`` / ``api_key`` /
``max_results`` / ``fetch_max_chars``. The old root-level keys
(``tools.web_search.api_key`` for Ollama, ``max_results``,
``fetch_max_chars``) are still read as a fallback and log a
deprecation warning once per key.
"""

from __future__ import annotations

from typing import Any

from chalkbox.logging.bridge import get_logger

from agentforge.config import get_config

logger = get_logger(__name__)

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 20
DEFAULT_FETCH_MAX_CHARS = 8000

# Root keys that only ever meant one provider (the pre-split Ollama key).
_LEGACY_ROOT_KEYS: dict[str, set[str]] = {
    "api_key": {"ollama"},
    "max_results": set(),  # empty = applies to every provider
    "fetch_max_chars": set(),
}

_warned: set[str] = set()


def _web_search_cfg() -> dict[str, Any]:
    try:
        return dict(get_config()._raw.get("tools", {}).get("web_search", {}) or {})
    except Exception:
        return {}


def provider_options(name: str) -> dict[str, Any]:
    return dict(_web_search_cfg().get(name) or {})


def provider_setting(name: str, key: str, default: Any = None) -> Any:
    """Provider block wins, then the legacy root key, then ``default``."""
    cfg = _web_search_cfg()
    value = (cfg.get(name) or {}).get(key)
    if value not in (None, ""):
        return value
    legacy_scope = _LEGACY_ROOT_KEYS.get(key)
    if legacy_scope is not None and (not legacy_scope or name in legacy_scope):
        legacy = cfg.get(key)
        if legacy not in (None, ""):
            if key not in _warned:
                _warned.add(key)
                logger.warning(
                    "[search_providers] tools.web_search.%s is deprecated — move it to tools.web_search.<provider>.%s",
                    key,
                    key,
                )
            return legacy
    return default


def is_enabled(name: str) -> bool:
    """``enabled`` defaults to true so configs without the flag keep working."""
    value = provider_options(name).get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0")
    return bool(value)


def max_results_for(name: str) -> int:
    try:
        n = int(provider_setting(name, "max_results", DEFAULT_MAX_RESULTS))
    except (TypeError, ValueError):
        n = DEFAULT_MAX_RESULTS
    return max(1, min(n, MAX_RESULTS_CAP))


def fetch_max_chars_for(name: str | None) -> int:
    if not name:
        return DEFAULT_FETCH_MAX_CHARS
    try:
        return int(provider_setting(name, "fetch_max_chars", DEFAULT_FETCH_MAX_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_FETCH_MAX_CHARS


__all__ = [
    "DEFAULT_FETCH_MAX_CHARS",
    "DEFAULT_MAX_RESULTS",
    "MAX_RESULTS_CAP",
    "fetch_max_chars_for",
    "is_enabled",
    "max_results_for",
    "provider_options",
    "provider_setting",
]
