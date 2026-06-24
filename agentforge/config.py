"""Configuration manager — YAML files with environment variable overrides and named profiles."""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins on conflicts)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_yaml_file(path: Path, label: str = "") -> dict[str, Any]:
    """Load a YAML file; return ``{}`` when missing (with a debug log)."""
    if path.exists():
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    logger.debug("%s not found at %s, using defaults", label or path.name, path)
    return {}


def merge_split_profiles(raw: dict[str, Any], config_dir: Path) -> None:
    """Glob ``profiles/**/*.yaml`` under *config_dir* and merge into *raw* ``ai``."""
    profiles_dir = config_dir / "profiles"
    if not profiles_dir.is_dir():
        return

    ai = raw.setdefault("ai", {})
    profiles = ai.setdefault("profiles", {})
    override_map = ai.setdefault("provider_override_map", {})

    loaded_profiles = 0
    loaded_overrides = 0
    for yaml_file in sorted(profiles_dir.rglob("*.yaml")):
        if yaml_file.name.endswith(".example.yaml"):
            continue
        stem = yaml_file.stem
        expected_prefix = stem + "-"
        with open(yaml_file) as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"{yaml_file} must contain a top-level dict (with `profiles:` and/or `provider_override_map:` keys)"
            )

        unknown = set(data) - {"profiles", "provider_override_map"}
        if unknown:
            raise ValueError(
                f"{yaml_file.name}: unknown top-level key(s) {sorted(unknown)}. "
                f"Allowed: 'profiles', 'provider_override_map'."
            )

        file_profiles = data.get("profiles") or {}
        if file_profiles:
            if not isinstance(file_profiles, dict):
                raise ValueError(f"{yaml_file.name}: 'profiles' must be a dict, got {type(file_profiles).__name__}")
            mismatched = [k for k in file_profiles if not k.startswith(expected_prefix)]
            if mismatched:
                raise ValueError(
                    f"{yaml_file.name}: profile keys must start with "
                    f"{expected_prefix!r} — found violations: {mismatched}"
                )
            collisions = [k for k in file_profiles if k in profiles]
            if collisions:
                raise ValueError(f"{yaml_file.name}: profile name(s) already defined in config.yaml: {collisions}")
            profiles.update(file_profiles)
            loaded_profiles += len(file_profiles)

        file_overrides = data.get("provider_override_map") or {}
        if file_overrides:
            if not isinstance(file_overrides, dict):
                raise ValueError(
                    f"{yaml_file.name}: 'provider_override_map' must be a dict, got {type(file_overrides).__name__}"
                )
            existing = override_map.get(stem) or {}
            if not isinstance(existing, dict):
                existing = {}
            clashes = [k for k in file_overrides if k in existing]
            if clashes:
                raise ValueError(
                    f"{yaml_file.name}: override-map key(s) already mapped for {stem!r} elsewhere: {clashes}"
                )
            override_map[stem] = {**existing, **file_overrides}
            loaded_overrides += len(file_overrides)

        logger.debug(
            "Loaded %d profile(s), %d override(s) from %s",
            len(file_profiles),
            len(file_overrides),
            yaml_file.relative_to(config_dir),
        )

    if loaded_profiles or loaded_overrides:
        logger.info(
            "Merged %d split profile(s) and %d override-map entr(ies) from %s",
            loaded_profiles,
            loaded_overrides,
            profiles_dir,
        )


def _config_with_example_fallback(path: Path) -> Path:
    """Use ``*.example.yaml`` when the real config file is missing (CI / fresh clone)."""
    if path.exists():
        return path
    example = path.with_name(f"{path.stem}.example{path.suffix}")
    if example.exists():
        logger.debug("%s not found at %s, using %s", path.name, path, example)
        return example
    return path


def load_merged_yaml(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load framework-config.yaml + config.yaml and merge split profiles.

    Single source of truth for the YAML dict both :class:`ConfigManager` and
    ``app.config`` build their typed settings from. Does not apply env-var
    overrides — those are :class:`ConfigManager` / pydantic concerns.

    When ``framework-config.yaml`` or ``config.yaml`` are absent (gitignored in
    the public repo), falls back to the committed ``*.example.yaml`` templates.
    """
    path = Path(config_path) if config_path else Path("config.yaml")
    base_path = _config_with_example_fallback(path.parent / "framework-config.yaml")
    user_path = _config_with_example_fallback(path)

    raw: dict[str, Any] = {}
    if base_path.exists() and base_path.resolve() != user_path.resolve():
        raw = load_yaml_file(base_path, base_path.name)
    if user_path.exists():
        raw = _deep_merge(raw, load_yaml_file(user_path, user_path.name))
    elif not raw:
        logger.debug("No config file found at %s / %s — using defaults", base_path, user_path)

    merge_split_profiles(raw, path.parent)
    return raw


# Per-request provider override. Set by the WS endpoint at the top of
# ``_process_query`` from the session row, and by the SAQ worker entry from
# the cross-process ``overrides`` dict. ContextVars propagate through
# ``asyncio.create_task`` and ``loop.run_in_executor`` automatically, so the
# variable is visible to every ``AIClient(...)`` site downstream — including
# framework internals (``router.py``, ``intent_classifier.py``, ``coding/*``,
# the agent's ``_retry_client``) — without threading kwargs through.
#
# An empty/None value means "use the singleton's provider override (or none
# at all)". A non-empty string forces this request to use that provider's
# override map, regardless of the singleton's value.
_request_provider_override: ContextVar[str | None] = ContextVar(
    "agentforge_request_provider_override",
    default=None,
)


def set_request_provider_override(provider: str | None) -> None:
    """Set the per-request provider override for the current ContextVar scope.

    Pass ``None`` to clear. Reentrant — overwrites any prior value in the
    same context. The WS endpoint sets this at the top of ``_process_query``;
    the SAQ worker sets it at the top of ``_execute_agent_job``.
    """
    if provider is not None:
        provider = provider.strip().lower() or None
    _request_provider_override.set(provider)


def get_request_provider_override() -> str | None:
    """Return the current request-scoped override, or ``None`` if unset."""
    return _request_provider_override.get()


# Per-request role/tier -> concrete-profile overrides, layered ON TOP of the
# active provider's ``provider_override_map`` for THIS request only. Set by the
# WS endpoint per external app via ``app_provider_role_mapping`` in
# framework-config.yaml (keyed by the session ``source`` tag, e.g., "felix") so
# an app can pick different models per role without editing the global provider
# maps or affecting other apps. Same shape as a provider submap:
# ``{role_or_tier: concrete_profile_name}``.
_request_role_override_map: ContextVar[dict[str, str] | None] = ContextVar(
    "agentforge_request_role_override_map",
    default=None,
)


def set_request_role_override_map(role_map: dict[str, str] | None) -> None:
    """Set the per-request role override map. Pass ``None`` to clear."""
    _request_role_override_map.set(role_map or None)


def get_request_role_override_map() -> dict[str, str] | None:
    """Return the current request-scoped role override map, or ``None``."""
    return _request_role_override_map.get()


# Per-request chat session id. Set by the WS endpoint at the top of
# ``_process_query`` (same place as the provider override). Read by the
# cross-role tool dispatch (``saq_dispatch_tool``) so a tool dispatched to a
# worker can carry the session id — the worker needs it to prompt the user
# (e.g., for a sudo password) back through that session's WebSocket. Propagates
# within the process via ContextVar; crosses to the worker as a job kwarg.
_request_session_id: ContextVar[str | None] = ContextVar(
    "agentforge_request_session_id",
    default=None,
)


def set_request_session_id(session_id: str | None) -> None:
    """Set the per-request chat session id for the current ContextVar scope."""
    _request_session_id.set(session_id or None)


def get_request_session_id() -> str | None:
    """Return the current request-scoped session id, or ``None`` if unset."""
    return _request_session_id.get()


# ---------------------------------------------------------------------------
# Profile dataclass — typed representation of a single AI profile
# ---------------------------------------------------------------------------


def _coerce_fallbacks(profile_name: str, value: Any) -> list[str]:
    """Validate the optional ``fallbacks:`` list on a profile."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"Profile '{profile_name}' has 'fallbacks' that is not a list "
            f"(got {type(value).__name__}). Expected a list of profile names."
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Profile '{profile_name}' has a non-string entry in 'fallbacks': {item!r}")
        out.append(item.strip())
    return out


@dataclass
class AIProfile:
    """A named AI profile with model, host, and generation options."""

    name: str
    model: str
    provider: str = "ollama"  # "ollama" | "bedrock"
    host: str = "http://localhost:11434"
    timeout: int = 600
    temperature: float = 0.7
    # Some newer models (e.g., Anthropic Claude Opus 4.7) reject the
    # ``temperature`` field entirely. Set this on the profile to skip
    # emitting it; the model uses its own default. Backend-specific.
    omit_temperature: bool = False
    max_tokens: int = 4000
    parse_thinking: bool = False
    abstract: bool = False  # base/model profile — not meant for direct use
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    # Optional sampling params (Ollama-native; Bedrock maps where possible)
    top_p: float | None = None
    top_k: int | None = None
    repeat_penalty: float | None = None
    stop: list[str] | None = None
    keep_alive: bool | None = None

    # Bedrock prompt caching — inject cachePoint blocks into system prompt
    # and tool definitions. Only effective for provider=bedrock; ignored by Ollama.
    prompt_caching: bool = False

    # Bedrock extended thinking -- token budget for chain-of-thought reasoning.
    # Only effective for provider=bedrock with Claude models; ignored by Ollama.
    # When set and deep_think=True, the backend uses native thinking blocks
    # instead of prompt-engineering.
    thinking_budget: int | None = None

    # Bedrock-specific credentials & region.
    # Populated from ai.bedrock.* (shared) unless a profile overrides.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_region: str | None = None

    # OpenAI-compatible providers (DeepInfra, OpenRouter) — the REST base URL.
    # Populated from ai.<provider>.base_url (shared) unless a profile
    # overrides. Used by the openai_compat backend as the root for
    # ``<base_url>/chat/completions`` etc.
    base_url: str | None = None

    # Ordered list of profile names to try if this profile's call fails.
    # Resolved at call time (not config-build time) so a misspelled or
    # not-yet-defined fallback never crashes startup. AIClient walks the
    # chain on retryable AND non-retryable exceptions (different model may
    # not have the same constraint); cancellation always bubbles.
    fallbacks: list[str] = field(default_factory=list)

    # Generic per-request escape hatch — extra keys merged verbatim into the
    # provider request for knobs the typed fields above don't cover. The
    # openai_compat backend merges them into the top level of the JSON body
    # (e.g., {"reasoning": {"enabled": false}} for OpenRouter); the Ollama
    # backend merges them into the chat() kwargs (e.g., {"think": false}), with
    # a nested "options" key merged into sampling options.
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any], base: dict[str, Any] | None = None) -> AIProfile:
        """Build a profile from a config dict, filling gaps from *base* (top-level ai: section)."""
        base = base or {}
        merged = {**base, **data}

        # Build headers from api_key if present
        headers: dict[str, str] = {}
        api_key = merged.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        model = merged.get("model")
        if not model:
            raise ValueError(f"Profile '{name}' has no 'model' key in config.yaml")

        return cls(
            name=name,
            model=model,
            provider=str(merged.get("provider", "ollama")).lower(),
            host=merged.get("host", "http://localhost:11434"),
            timeout=int(merged.get("timeout", 600)),
            temperature=float(merged.get("temperature", 0.7)),
            omit_temperature=bool(merged.get("omit_temperature", False)),
            max_tokens=int(merged.get("max_tokens", 4000)),
            parse_thinking=bool(merged.get("parse_thinking", False)),
            abstract=bool(merged.get("abstract", False)),
            api_key=api_key,
            headers=headers,
            top_p=merged.get("top_p"),
            top_k=merged.get("top_k"),
            repeat_penalty=merged.get("repeat_penalty"),
            stop=merged.get("stop"),
            keep_alive=merged.get("keep_alive"),
            prompt_caching=bool(merged.get("prompt_caching", False)),
            thinking_budget=merged.get("thinking_budget"),
            aws_access_key_id=merged.get("aws_access_key_id"),
            aws_secret_access_key=merged.get("aws_secret_access_key"),
            aws_session_token=merged.get("aws_session_token"),
            aws_region=merged.get("aws_region"),
            base_url=merged.get("base_url"),
            fallbacks=_coerce_fallbacks(name, merged.get("fallbacks")),
            extra_body=dict(merged.get("extra_body") or {}),
        )


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Load configuration from YAML + env overrides, expose profiles."""

    # Env var → config path mappings  (path tuple + optional converter)
    ENV_MAPPINGS: dict[str, tuple[str, ...]] = {
        "OLLAMA_MODEL": ("ai", "model"),
        "OLLAMA_HOST": ("ai", "host"),
        "OLLAMA_TIMEOUT": ("ai", "timeout", "int"),
    }

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._raw: dict[str, Any] = {}
        self._profiles: dict[str, AIProfile] = {}
        # Global provider override — flips every role profile between Ollama
        # and Bedrock without touching individual profile definitions.
        # Populated by _resolve_provider_override() from AGENTFORGE_PROVIDER env
        # var (wins) or ai.provider_override YAML key.
        self._provider_override: str | None = None
        self._provider_override_map: dict[str, str] = {}

        path = Path(config_path) if config_path else Path("config.yaml")
        self._raw = load_merged_yaml(path)

        # Per-external-app role overrides (framework-config.yaml top-level key).
        # Shape: {app_source: {provider: {role_or_tier: concrete_profile}}}.
        # Layered over provider_override_map for that app's sessions only,
        # keyed by the session `source` tag — see ws_endpoint._process_query.
        self._app_role_map: dict[str, dict[str, dict[str, str]]] = self._raw.get("app_provider_role_mapping", {}) or {}

        self._apply_env_overrides()
        self._resolve_provider_override()
        self._build_profiles()

    # -- file loading -------------------------------------------------------

    def _load_file(self, path: Path) -> None:
        self._raw = load_merged_yaml(path)

    @property
    def raw(self) -> dict[str, Any]:
        """Merged YAML dict (framework-config + config.yaml + split profiles)."""
        return self._raw

    # -- env overrides ------------------------------------------------------

    def _apply_env_overrides(self) -> None:
        converters = {"int": int, "float": float, "bool": lambda v: v.lower() in ("true", "1", "yes")}

        for env_var, mapping in self.ENV_MAPPINGS.items():
            value = os.getenv(env_var)
            if value is None:
                continue

            *path_parts, key = mapping[:-1] if mapping[-1] in converters else mapping
            converter_name = mapping[-1] if mapping[-1] in converters else None

            converted = converters[converter_name](value) if converter_name else value

            # Walk to the nested dict
            node = self._raw
            for part in path_parts:
                node = node.setdefault(part, {})
            node[key] = converted
            logger.debug(
                "Env override: %s → %s = %s", env_var, ".".join(mapping[:-1] if converter_name else mapping), converted
            )

    # -- provider override --------------------------------------------------

    def _resolve_provider_override(self) -> None:
        """Resolve the active provider and its tier->concrete map.

        Precedence: ``AGENTFORGE_PROVIDER`` env var, then ``ai.provider_override``
        in YAML, then ``ollama`` as the default. Ollama is selected like any
        other provider — not a hardcoded fallback. The companion mapping
        ``ai.provider_override_map`` (nested per-provider) tells
        :meth:`_resolve_profile` which concrete model each capability tier
        resolves to::

            provider_override_map:
              ollama:                       # the base layer (covers every tier)
                heavy:   ollama-mistral-large
                light:   ollama-ministral-3
              deepinfra:                    # overrides the base per tier/role
                heavy:   deepinfra-mistral-small-3-2-24b
                agent:   deepinfra-claude-haiku-4-5   # role-keyed direct-select

        The Ollama submap underlies every provider (see :meth:`_compute_active_map`),
        so a tier a provider doesn't map falls back to the Ollama concrete.
        """
        ai = self._raw.get("ai", {})
        env_override = os.getenv("AGENTFORGE_PROVIDER")
        yaml_override = ai.get("provider_override")

        override = env_override or yaml_override or "ollama"
        self._provider_override = str(override).strip().lower() or "ollama"
        self._provider_override_map = self._compute_active_map(self._provider_override)
        source = "env AGENTFORGE_PROVIDER" if env_override else "ai.provider_override" if yaml_override else "default"
        logger.info(
            "Provider active: %s (from %s, %d tier/role aliases)",
            self._provider_override,
            source,
            len(self._provider_override_map),
        )

    def _compute_active_map(self, provider: str) -> dict[str, str]:
        """Compute the effective tier->concrete map for *provider*.

        The Ollama submap is the base (it covers every capability tier); the
        active provider's submap is layered on top, overriding per key and
        adding any role-keyed direct-selects. So a tier the provider doesn't
        map falls back to the Ollama concrete — preserving the old behaviour
        where an unmapped abstract simply was an Ollama model.

        Used at startup (singleton) and at request time (per-session override
        without mutating singleton state).
        """
        raw_map = self._raw.get("ai", {}).get("provider_override_map") or {}
        if not isinstance(raw_map, dict):
            return {}

        def _as_str_map(v: object) -> dict[str, str]:
            return {str(k): str(val) for k, val in v.items()} if isinstance(v, dict) else {}

        merged = _as_str_map(raw_map.get("ollama"))
        if provider != "ollama":
            merged.update(_as_str_map(raw_map.get(provider)))
        return merged

    # -- profiles -----------------------------------------------------------

    def _resolve_profile(
        self,
        name: str,
        profiles_raw: dict[str, Any],
        visited: tuple[str, ...] = (),
        *,
        active_override: str | None = None,
        active_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a profile dict with inheritance applied.

        If the profile dict contains a ``profile:`` key, the named parent is
        resolved first and used as the base; child keys (except ``profile``
        itself) then override it.  Supports arbitrary inheritance chains;
        raises ``ValueError`` on circular references.

        ``active_override`` / ``active_map`` let callers (the request-time
        path in :meth:`get_profile`) supply override values that differ from
        the singleton's, without mutating singleton state. Defaults read from
        the instance — preserves the original behaviour for the build-time
        and singleton-resolution code paths.
        """
        if active_override is None:
            active_override = self._provider_override
        if active_map is None:
            active_map = self._provider_override_map

        if name in visited:
            cycle = " → ".join((*visited, name))
            raise ValueError(f"Circular profile inheritance detected: {cycle}")

        data = profiles_raw.get(name)
        if data is None:
            available = ", ".join(profiles_raw)
            raise ValueError(f"Profile '{name}' referenced but not defined. Available: {available}")

        # Direct-select redirect: the user is asking for an abstract model
        # profile (e.g., `gemma4`) that itself sits in the override map. Swap
        # it out for the override target before doing any inheritance work.
        # Without this, the early-return below would hand back the raw
        # Ollama profile regardless of the override. Only fires on the
        # initial call (visited is empty) so chained parent lookups can't
        # trip it recursively. The active map is already per-provider — we
        # just need a non-empty override.
        if active_override and not visited and name in active_map:
            mapped = active_map[name]
            if mapped in profiles_raw:
                logger.debug("Provider override: direct select %s → %s", name, mapped)
                return self._resolve_profile(
                    mapped,
                    profiles_raw,
                    (name,),
                    active_override=active_override,
                    active_map=active_map,
                )
            logger.warning(
                "Provider override map entry '%s → %s' ignored: target profile not defined",
                name,
                mapped,
            )

        parent_name = data.get("profile")
        if parent_name is None:
            return dict(data)

        # Global provider override: if active and this parent is in the
        # override map, redirect to its replacement (e.g., devstral-small →
        # bedrock-devstral). Only fires for role profiles — abstract model
        # profiles (which themselves sit in the map) are excluded so we don't
        # recursively rewrite Bedrock-typed parents.
        if active_override and parent_name in active_map and name not in active_map:
            mapped = active_map[parent_name]
            if mapped in profiles_raw:
                logger.debug("Provider override: %s → parent %s (was %s)", name, mapped, parent_name)
                parent_name = mapped
            else:
                logger.warning(
                    "Provider override map entry '%s → %s' ignored: target profile not defined",
                    parent_name,
                    mapped,
                )

        # Resolve parent first (recursive), then let child keys win.
        # `abstract` is a property of the definition itself and MUST NOT be
        # inherited — otherwise every child of an abstract model profile
        # would also become abstract.
        parent_resolved = self._resolve_profile(
            parent_name,
            profiles_raw,
            (*visited, name),
            active_override=active_override,
            active_map=active_map,
        )
        parent_resolved.pop("abstract", None)
        child_overrides = {k: v for k, v in data.items() if k != "profile"}
        return {**parent_resolved, **child_overrides}

    def _shared_creds(self) -> dict[str, dict[str, Any]]:
        """Bundle the per-provider shared credential dicts read from ``ai.*``.

        Used by both ``_build_profiles`` (startup) and ``_build_profile_for_override``
        (request-time). Env vars beat YAML for OpenAI-compatible providers so
        CI / secret stores can override without editing the file.
        """
        ai = self._raw.get("ai", {})
        deepinfra_shared: dict[str, Any] = dict(ai.get("deepinfra") or {})
        openrouter_shared: dict[str, Any] = dict(ai.get("openrouter") or {})
        deepinfra_env = os.getenv("DEEPINFRA_API_KEY") or os.getenv("DEEPINFRA_TOKEN")
        if deepinfra_env:
            deepinfra_shared["api_key"] = deepinfra_env
        openrouter_env = os.getenv("OPENROUTER_API_KEY")
        if openrouter_env:
            openrouter_shared["api_key"] = openrouter_env
        return {
            "bedrock": ai.get("bedrock") or {},
            "deepinfra": deepinfra_shared,
            "openrouter": openrouter_shared,
        }

    @staticmethod
    def _apply_shared_creds(resolved: dict[str, Any], shared: dict[str, dict[str, Any]]) -> None:
        """Fill in per-provider shared creds on a resolved profile dict in place."""
        provider = str(resolved.get("provider", "ollama")).lower()
        bedrock_shared = shared.get("bedrock", {})
        deepinfra_shared = shared.get("deepinfra", {})
        openrouter_shared = shared.get("openrouter", {})
        if provider == "bedrock" and bedrock_shared:
            for key in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token", "aws_region"):
                if key in bedrock_shared and key not in resolved:
                    resolved[key] = bedrock_shared[key]
        if provider == "deepinfra" and deepinfra_shared:
            for key in ("api_key", "base_url", "timeout"):
                if key in deepinfra_shared and key not in resolved:
                    resolved[key] = deepinfra_shared[key]
        if provider == "openrouter" and openrouter_shared:
            for key in ("api_key", "base_url", "timeout"):
                if key in openrouter_shared and key not in resolved:
                    resolved[key] = openrouter_shared[key]

    def _build_profiles(self) -> None:
        ai = self._raw.get("ai", {})
        if "model" not in ai:
            raise ValueError("Missing 'ai.model' in config.yaml (default model for profiles)")
        base = {
            "model": ai["model"],
            "host": ai.get("host", "http://localhost:11434"),
            "timeout": ai.get("timeout", 600),
        }

        shared = self._shared_creds()
        profiles_raw = ai.get("profiles", {})
        if not profiles_raw:
            raise ValueError("No profiles defined in config.yaml (ai.profiles is empty)")

        for name, data in profiles_raw.items():
            resolved = self._resolve_profile(name, profiles_raw)
            self._apply_shared_creds(resolved, shared)
            self._profiles[name] = AIProfile.from_dict(name, resolved, base)
            # Fail fast: if a non-abstract Bedrock profile lacks a region at
            # this point, there's no way the backend can construct a client.
            prof = self._profiles[name]
            if prof.provider == "bedrock" and not prof.abstract and not prof.aws_region:
                raise ValueError(
                    f"Profile '{name}' has provider=bedrock but no aws_region. "
                    f"Set ai.bedrock.aws_region or override on the profile."
                )
            if prof.provider in ("deepinfra", "openrouter") and not prof.abstract:
                env_var = {
                    "deepinfra": "DEEPINFRA_API_KEY",
                    "openrouter": "OPENROUTER_API_KEY",
                }[prof.provider]
                if not prof.api_key:
                    raise ValueError(
                        f"Profile '{name}' has provider={prof.provider} but no api_key. "
                        f"Set ai.{prof.provider}.api_key or export {env_var}."
                    )
                if not prof.base_url:
                    raise ValueError(
                        f"Profile '{name}' has provider={prof.provider} but no base_url. "
                        f"Set ai.{prof.provider}.base_url in config.yaml."
                    )

    def _build_profile_for_override(
        self, name: str, override: str, role_map: dict[str, str] | None = None
    ) -> AIProfile:
        """Resolve and build an :class:`AIProfile` under a per-request override.

        Mirrors ``_build_profiles`` for a single profile, but uses the
        request-supplied override + map instead of the singleton's. Doesn't
        cache — each call returns a fresh object so the singleton's
        ``self._profiles`` cache stays untouched.

        If *override* has no submap entry for *name* (or for the parent it
        inherits from), the function still resolves successfully and returns
        the raw Ollama profile — same fallback the singleton path uses.
        """
        ai = self._raw.get("ai", {})
        profiles_raw = ai.get("profiles", {})
        if name not in profiles_raw:
            available = ", ".join(profiles_raw)
            raise ValueError(f"Profile '{name}' not found. Available: {available}")
        active_map = self._compute_active_map(override)
        if role_map:
            # App-only role overrides win over the provider's global map.
            active_map = {**active_map, **role_map}
        resolved = self._resolve_profile(
            name,
            profiles_raw,
            active_override=override,
            active_map=active_map,
        )
        self._apply_shared_creds(resolved, self._shared_creds())
        base = {
            "model": ai.get("model"),
            "host": ai.get("host", "http://localhost:11434"),
            "timeout": ai.get("timeout", 600),
        }
        return AIProfile.from_dict(name, resolved, base)

    # -- public API ---------------------------------------------------------

    def get(self, dotpath: str, default: Any = None) -> Any:
        """Get a value by dot-notation path (e.g., ``ai.model``)."""
        parts = dotpath.split(".")
        node: Any = self._raw
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def get_by_provider(self, section: str, key: str, default: Any = None) -> Any:
        """Read ``section.key`` with an optional per-provider override.

        Looks up ``section.{key}_by_provider.<active_provider>`` first and
        returns that value if present. Falls back to ``section.key`` and
        finally ``default``. The active provider is the request-scoped
        override if set, else the singleton's resolved value
        (``AGENTFORGE_PROVIDER`` env var wins over the YAML key).

        Used by concurrency knobs that need to scale with provider capacity
        (e.g., ``parallel.max_workers`` should stay small on Ollama Cloud but
        can be much larger on DeepInfra's 200-concurrent-requests tier)
        without forcing edits to the YAML every time the provider flips.
        """
        active = _request_provider_override.get() or self._provider_override or ""
        if active:
            by_provider = self.get(f"{section}.{key}_by_provider")
            if isinstance(by_provider, dict):
                val = by_provider.get(active)
                if val is not None:
                    return val
        return self.get(f"{section}.{key}", default)

    def get_profile(self, name: str | None = None) -> AIProfile:
        """Return a named profile, or the default profile if *name* is ``None``.

        Honours the per-request override set via :func:`set_request_provider_override`.
        When the request override differs from the singleton's, the profile
        is rebuilt on the fly without mutating the cached ``self._profiles``.
        """
        if name is None:
            name = self.get("ai.default_profile", "default")
        request_override = _request_provider_override.get()
        request_role_map = _request_role_override_map.get()
        if (request_override and request_override != self._provider_override) or request_role_map:
            # Rebuild on the fly under the request's provider + app role overrides.
            return self._build_profile_for_override(name, request_override or self._provider_override, request_role_map)
        if name not in self._profiles:
            available = ", ".join(self._profiles)
            raise ValueError(f"Profile '{name}' not found. Available: {available}")
        return self._profiles[name]

    def list_profiles(self, include_abstract: bool = False) -> list[str]:
        """List profile names.  Abstract (base/model) profiles are hidden by default."""
        if include_abstract:
            return list(self._profiles)
        return [name for name, prof in self._profiles.items() if not prof.abstract]

    @property
    def default_profile_name(self) -> str:
        return self.get("ai.default_profile", "default")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: ConfigManager | None = None


def get_config(config_path: str | Path | None = None) -> ConfigManager:
    """Return the global ``ConfigManager`` (created on first call)."""
    global _instance
    if _instance is None:
        _instance = ConfigManager(config_path)
    return _instance


def reset_config() -> None:
    """Reset the global config (useful for testing)."""
    global _instance
    _instance = None
