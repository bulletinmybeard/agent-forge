import logging
import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentforge.config import load_merged_yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
# Merged YAML via agentforge.config (framework-config + config.yaml + profiles/).
_yaml = load_merged_yaml(_CONFIG_PATH)


# ── AI model profiles ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedProfile:
    """Fully resolved profile ready for Ollama client construction."""

    name: str
    model: str
    host: str
    api_key: str
    provider: str = "ollama"

    @property
    def headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}


@dataclass(frozen=True)
class ResolvedRole:
    """A pipeline role with its resolved profile and LLM options.

    Options are read from ``ollama.model_roles.<role>`` in config.yaml and
    override the hardcoded defaults in each service.  Services access
    options via :attr:`options` (the raw dict) or the convenience
    properties below.
    """

    profile: ResolvedProfile
    options: dict = dc_field(default_factory=dict)

    # ── Convenience accessors with sensible defaults ──────────────────

    @property
    def num_predict(self) -> int:
        return int(self.options.get("num_predict", 1024))

    @property
    def temperature(self) -> float:
        return float(self.options.get("temperature", 0.2))

    @property
    def top_p(self) -> float:
        return float(self.options.get("top_p", 0.9))

    @property
    def top_k(self) -> int:
        return int(self.options.get("top_k", 40))

    @property
    def repeat_penalty(self) -> float:
        return float(self.options.get("repeat_penalty", 1.1))

    @property
    def refiner_max_results(self) -> int:
        """Max chunks sent to the LLM refiner (answer_generation role)."""
        return int(self.options.get("refiner_max_results", 20))

    @property
    def refiner_max_context_chars(self) -> int:
        """Character budget for the context block sent to the LLM."""
        return int(self.options.get("refiner_max_context_chars", 12000))

    @property
    def ollama_options(self) -> dict:
        """Return the subset of options that are Ollama API parameters."""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repeat_penalty": self.repeat_penalty,
            "num_predict": self.num_predict,
        }


class OllamaSettings(BaseSettings):
    """Ollama connection settings with named AI model profiles.

    Profiles define model + host + api_key combinations.  Roles map
    pipeline steps (``query_refinement``, ``answer_generation``,
    ``embedding``) to profile names plus per-role LLM options.

    Backward compatible: when a role is a plain string (e.g.,
    ``query_refinement: "cloud-light"``), it is treated as
    ``{profile: "cloud-light"}`` with default options.
    """

    model_config = SettingsConfigDict(env_prefix="OLLAMA_")

    host: str = Field(default=_yaml.get("ollama", {}).get("host", "http://localhost:11434"))

    @staticmethod
    def _merge_profile_chain(
        profile_name: str,
        profiles: dict,
        visited: tuple = (),
    ) -> dict:
        """Recursively merge a profile with its parent(s) via the ``profile:`` key.

        Profile inheritance lets a profile declare ``profile: <parent-name>``
        to inherit the parent's model + options; any other keys on the child
        override the parent.  This mirrors the same resolution done by
        ``agentforge/config.py`` so that both loaders agree on the final shape.
        """
        if profile_name in visited:
            cycle = " → ".join((*visited, profile_name))
            raise ValueError(f"Circular profile inheritance detected: {cycle}")

        if profile_name not in profiles:
            raise ValueError(
                f"Profile '{profile_name}' not found in config.yaml. "
                f"Available profiles: {', '.join(sorted(profiles)) or '(none)'}"
            )

        raw = profiles[profile_name]
        parent_name = raw.get("profile") if isinstance(raw, dict) else None
        if parent_name is None:
            return dict(raw) if isinstance(raw, dict) else {}

        parent_merged = OllamaSettings._merge_profile_chain(parent_name, profiles, (*visited, profile_name))
        # `abstract` is a per-definition marker and MUST NOT be inherited —
        # otherwise every child of an abstract model profile would also be
        # abstract and get filtered out of UI profile lists.
        parent_merged.pop("abstract", None)
        child_overrides = {k: v for k, v in raw.items() if k != "profile"}
        return {**parent_merged, **child_overrides}

    def _resolve_profile(self, profile_name: str) -> ResolvedProfile:
        """Resolve a profile name to a fully populated ResolvedProfile.

        Delegates to the framework's :class:`ConfigManager.get_profile` so
        ``ai.provider_override`` + ``ai.provider_override_map`` are applied
        here exactly the same way they are for every other LLM call path.
        Without this, roles resolved through agentforge's own config
        (``@chat``, scheduler, RAG) silently bypassed the override and kept
        hitting Ollama even when the global switch was flipped to DeepInfra,
        Bedrock, or OpenRouter.

        Falls back to the legacy local merger if the framework's config
        singleton can't be loaded (e.g., at very early import time before
        ``ConfigManager`` has initialised).
        """
        try:
            from agentforge.config import get_config as _get_framework_config

            fw_prof = _get_framework_config().get_profile(profile_name)
            return ResolvedProfile(
                name=fw_prof.name,
                model=fw_prof.model,
                # Non-Ollama providers store their endpoint in base_url;
                # fall back to host for Ollama profiles.
                host=fw_prof.base_url or fw_prof.host,
                api_key=fw_prof.api_key or "",
                provider=(fw_prof.provider or "ollama").lower(),
            )
        except Exception as exc:
            logger.warning(
                "OllamaSettings._resolve_profile: framework ConfigManager "
                "unavailable (%s); falling back to local merger — "
                "provider_override will NOT be applied for '%s'",
                exc,
                profile_name,
            )

        ai_cfg = _yaml.get("ai", {})
        profiles = ai_cfg.get("profiles", {})

        merged = self._merge_profile_chain(profile_name, profiles)

        model = merged.get("model")
        if not model:
            raise ValueError(
                f"Profile '{profile_name}' in config.yaml has no 'model' key "
                f"(neither directly nor via its parent profile chain)"
            )

        return ResolvedProfile(
            name=profile_name,
            model=model,
            host=merged.get("host", self.host),
            api_key=merged.get("api_key", ""),
            provider=(merged.get("provider") or "ollama").lower(),
        )

    def get_role(self, role: str) -> ResolvedRole:
        """Resolve a pipeline role to a profile + options bundle.

        Roles are defined in config.yaml under ollama.model_roles.
        Profiles are resolved from config.yaml.
        Raises ValueError if a role or profile is missing.
        """
        ollama_cfg = _yaml.get("ollama", {})
        roles = ollama_cfg.get("model_roles", {})
        role_value = roles.get(role)

        if role_value is None:
            raise ValueError(
                f"Role '{role}' not found in config.yaml (ollama.model_roles). "
                f"Available roles: {', '.join(sorted(roles)) or '(none)'}"
            )

        # ── Legacy string format: "cloud-light" ──────────────────────
        if isinstance(role_value, str):
            return ResolvedRole(profile=self._resolve_profile(role_value))

        # ── Dict format: {profile: "cloud-light", num_predict: 256}
        if isinstance(role_value, dict):
            profile_name = role_value.get("profile")
            if not profile_name:
                raise ValueError(f"Role '{role}' in config.yaml has no 'profile' key")

            profile = self._resolve_profile(profile_name)
            options = {k: v for k, v in role_value.items() if k != "profile"}
            return ResolvedRole(profile=profile, options=options)

        raise ValueError(f"Role '{role}' in config.yaml has unexpected type {type(role_value).__name__}")

    def get_profile(self, role: str) -> ResolvedProfile:
        """Backward-compatible shortcut: resolve role → profile only.

        Prefer :meth:`get_role` in new code to also get LLM options.
        """
        return self.get_role(role).profile

    def list_selectable_profiles(self, include_abstract: bool = False) -> dict[str, dict]:
        """Return ``{profile_name: {model, temperature, max_tokens, provider, abstract}}``
        for every profile, with inheritance + provider override applied.

        Abstract profiles (base model definitions tagged ``abstract: true``)
        are excluded by default — they exist only to be inherited. Set
        ``include_abstract=True`` to include them (used by the multi-provider
        prompt lab which wants to pick individual ``bedrock-claude-*`` /
        ``deepinfra-*`` / ``openrouter-*`` abstracts directly).

        Resolution is delegated to the framework's :class:`ConfigManager`
        (single source of truth) so that ``ai.provider_override`` +
        ``ai.provider_override_map`` are honoured here exactly the same way
        they are at chat-time. Result: when override is set to ``deepinfra``,
        every role profile reports the DeepInfra model it actually maps to —
        the UI dropdown shows the real options, not stale Ollama tags.
        """
        from agentforge.config import get_config as _get_framework_config

        ai_cfg = _yaml.get("ai", {})
        profiles = ai_cfg.get("profiles", {})

        try:
            fw_cfg = _get_framework_config()
        except Exception as e:
            # Framework config unavailable — fall back to the legacy local
            # merger so the endpoint still returns something usable.
            logger.warning(
                "Framework ConfigManager unavailable (%s); falling back to "
                "local _merge_profile_chain — provider_override will NOT be "
                "applied to the dropdown",
                e,
            )
            fw_cfg = None

        def _walk_declared_provider(start: str) -> str:
            """Return the provider DECLARED in YAML (no override applied).

            Walks ``profile:`` parents up the chain until it finds an explicit
            ``provider:`` key. Defaults to ``"ollama"`` when nothing is set.
            Used by the multi-provider prompt lab so abstracts like
            ``devstral-small`` group under Ollama even when a global
            ``provider_override`` would rewrite them at chat-time.
            """
            visited: set[str] = set()
            current: str | None = start
            while current and current not in visited:
                visited.add(current)
                data = profiles.get(current)
                if not isinstance(data, dict):
                    break
                prov = data.get("provider")
                if prov:
                    return str(prov).lower()
                parent = data.get("profile")
                if not parent:
                    break
                current = parent
            return "ollama"

        result: dict[str, dict] = {}
        for name, raw in profiles.items():
            if not isinstance(raw, dict):
                continue
            is_abstract = bool(raw.get("abstract", False))
            if is_abstract and not include_abstract:
                continue

            declared_provider = _walk_declared_provider(name)

            if fw_cfg is not None:
                try:
                    prof = fw_cfg.get_profile(name)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Skipping unresolvable profile '%s': %s", name, e)
                    continue
                model = prof.model
                if not model:
                    logger.warning("Skipping profile '%s' with no resolved model", name)
                    continue
                result[name] = {
                    "model": model,
                    "temperature": float(prof.temperature),
                    "max_tokens": int(prof.max_tokens),
                    "provider": (prof.provider or "ollama").lower(),
                    "declared_provider": declared_provider,
                    "abstract": is_abstract,
                }
            else:
                # Fallback path — legacy local merger, no override applied
                try:
                    merged = self._merge_profile_chain(name, profiles)
                except ValueError as e:
                    logger.warning("Skipping unresolvable profile '%s': %s", name, e)
                    continue
                model = merged.get("model")
                if not model:
                    logger.warning("Skipping profile '%s' with no resolved model", name)
                    continue
                result[name] = {
                    "model": model,
                    "temperature": float(merged.get("temperature", 0.7)),
                    "max_tokens": int(merged.get("max_tokens", 4000)),
                    "provider": (merged.get("provider") or "ollama").lower(),
                    "declared_provider": declared_provider,
                    "abstract": is_abstract,
                }
        return result


class QdrantSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QDRANT_")

    host: str = Field(default=_yaml.get("qdrant", {}).get("host", "localhost"))
    port: int = Field(default=_yaml.get("qdrant", {}).get("port", 6333))
    collection_name: str = Field(default=_yaml.get("qdrant", {}).get("collection_name", "agentforge_kb"))


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMBEDDING_")

    dimension: int = Field(default=_yaml.get("embedding", {}).get("dimension", 4096))
    distance_metric: str = Field(default=_yaml.get("embedding", {}).get("distance_metric", "Cosine"))
    # Ollama keep_alive for the embed model: -1 keeps it resident (no eviction),
    # so sparse search/index calls don't pay a cold-load each time. str ("24h") ok.
    keep_alive: int | str = Field(default=_yaml.get("embedding", {}).get("keep_alive", -1))


class IndexerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INDEXER_")

    chunks_dir: str = Field(default=_yaml.get("indexer", {}).get("chunks_dir", "/app/chunks"))
    batch_size: int = Field(default=_yaml.get("indexer", {}).get("batch_size", 50))


class DedupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEDUP_")

    enabled: bool = Field(default=_yaml.get("dedup", {}).get("enabled", True))
    similarity_threshold: float = Field(default=_yaml.get("dedup", {}).get("similarity_threshold", 0.95))
    drift_threshold: float = Field(default=_yaml.get("dedup", {}).get("drift_threshold", 0.70))


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SEARCH_")

    score_floor: float = Field(default=_yaml.get("search", {}).get("score_floor", 0.50))
    overfetch_factor: int = Field(default=_yaml.get("search", {}).get("overfetch_factor", 3))
    relevance_threshold: float = Field(
        default=_yaml.get("search", {}).get("relevance_threshold", 0.60),
        description="If the best result score is below this, skip RAG context and answer from general knowledge.",
    )
    # @source shorthand -> Qdrant source_name(s). A list searches multiple
    # collections (e.g., an OpenAPI spec + its indexed source). Deployer-specific,
    # so it lives in config (keeps internal project names out of the codebase).
    source_aliases: dict[str, str | list[str]] = Field(
        default_factory=lambda: dict(_yaml.get("search", {}).get("source_aliases", {})),
    )
    # #type shorthand -> source_type filter (e.g., "help" -> "docs").
    source_type_aliases: dict[str, str] = Field(
        default_factory=lambda: dict(_yaml.get("search", {}).get("source_type_aliases", {})),
    )


class RefinementSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REFINEMENT_")

    input_enabled: bool = Field(default=_yaml.get("refinement", {}).get("input_enabled", True))
    output_examples: bool = Field(default=_yaml.get("refinement", {}).get("output_examples", False))


class PromptRefinementSettings(BaseSettings):
    """Opening-prompt refinement for the Prompt Lab + agent endpoints.

    Distinct from RefinementSettings, which refines the search query for
    embedding. Off by default; ``profile`` names a framework-config profile.
    """

    model_config = SettingsConfigDict(env_prefix="PROMPT_REFINEMENT_")

    enabled: bool = Field(default=_yaml.get("prompt_refinement", {}).get("enabled", False))
    profile: str = Field(default=_yaml.get("prompt_refinement", {}).get("profile", "input-refiner"))


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_")

    enabled: bool = Field(default=_yaml.get("memory", {}).get("enabled", True))
    max_tokens: int = Field(default=_yaml.get("memory", {}).get("max_tokens", 2000))
    persistence_path: str = Field(
        default=_yaml.get("memory", {}).get("persistence_path", "/app/data/agentforge_memory.json")
    )
    fact_stale_days: int = Field(default=_yaml.get("memory", {}).get("fact_stale_days", 30))
    semantic_max_age_days: int = Field(
        default=_yaml.get("memory", {}).get("semantic", {}).get("max_age_days", 14),
    )
    # Char cap on the text embedded per query+response pair (semantic memory).
    semantic_max_embed_chars: int = Field(
        default=_yaml.get("memory", {}).get("semantic", {}).get("max_embed_chars", 2000),
    )
    # Result-store label truncation (chars).
    result_store_label_truncate: int = Field(
        default=_yaml.get("memory", {}).get("result_store", {}).get("label_truncate", 50),
    )
    # Session-event Pub/Sub client tuning.
    session_events_event_timeout: float = Field(
        default=_yaml.get("memory", {}).get("session_events", {}).get("event_timeout", 1.0),
    )
    session_events_reconnect_delay: float = Field(
        default=_yaml.get("memory", {}).get("session_events", {}).get("reconnect_delay", 2.0),
    )
    session_events_max_reconnect_attempts: int = Field(
        default=_yaml.get("memory", {}).get("session_events", {}).get("max_reconnect_attempts", 5),
    )
    # Short-term replay buffer for late subscribers.
    session_event_buffer_max_events: int = Field(
        default=_yaml.get("memory", {}).get("session_event_buffer", {}).get("max_events", 500),
    )
    session_event_buffer_ttl_seconds: int = Field(
        default=_yaml.get("memory", {}).get("session_event_buffer", {}).get("ttl_seconds", 3600),
    )


class CodeContextSettings(BaseSettings):
    """Settings for hybrid code context enrichment (Option C).

    When enabled, code-type search results are enriched with actual source
    code snippets and grep-based usage sites before being sent to the LLM.
    """

    model_config = SettingsConfigDict(env_prefix="CODE_CONTEXT_")

    enabled: bool = Field(default=_yaml.get("code_context", {}).get("enabled", False))
    snippet_lines: int = Field(default=_yaml.get("code_context", {}).get("snippet_lines", 15))
    max_grep_results: int = Field(default=_yaml.get("code_context", {}).get("max_grep_results", 10))
    file_extensions: list[str] = Field(
        default_factory=lambda: _yaml.get("code_context", {}).get("file_extensions", [".py", ".yaml", ".yml"])
    )
    source_roots: dict[str, str] = Field(default_factory=lambda: _yaml.get("code_context", {}).get("source_roots", {}))


class ChunkingSettings(BaseSettings):
    """Chunking and document-lookup settings."""

    model_config = SettingsConfigDict(env_prefix="CHUNKING_")

    document_lookup_stoplist: list[str] = Field(
        default_factory=lambda: _yaml.get("chunking", {}).get("document_lookup_stoplist", []),
        description="Project names too generic for auto-detection in queries (stoplist).",
    )


@dataclass
class SqlDatabaseEntry:
    """A single named database connection for the execute_sql tool."""

    url: str
    engine: str  # "mysql" or "postgres"
    name: str = ""  # human-readable display name (e.g., "MyDB Database")
    max_rows: int = 100
    readonly: bool = True


class SqlDatabasesSettings(BaseSettings):
    """Runtime SQL query execution connections.

    Loaded from config.yaml ``sql_databases`` section.  Each key is a
    logical database name (matching a Qdrant ``source_name``), and the
    value describes how to connect.
    """

    model_config = SettingsConfigDict(env_prefix="SQL_DB_")

    databases: dict[str, SqlDatabaseEntry] = dc_field(default_factory=dict)

    def __init__(self, **kwargs: object) -> None:
        raw = _yaml.get("sql_databases", {})
        # When running inside Docker, DB_HOST overrides 'localhost' in URLs
        # so containers can reach host-network databases via host.docker.internal.
        db_host = os.environ.get("DB_HOST", "")
        entries: dict[str, SqlDatabaseEntry] = {}
        for name, cfg in raw.items():
            if isinstance(cfg, dict):
                url = cfg.get("url", "")
                if db_host and "localhost" in url:
                    url = url.replace("localhost", db_host)
                entries[name] = SqlDatabaseEntry(
                    url=url,
                    engine=cfg.get("engine", "postgres"),
                    name=cfg.get("name", name),
                    max_rows=int(cfg.get("max_rows", 100)),
                    readonly=bool(cfg.get("readonly", True)),
                )
        super().__init__(databases=entries, **kwargs)

    def get(self, name: str) -> SqlDatabaseEntry | None:
        return self.databases.get(name)

    @property
    def available_names(self) -> list[str]:
        return list(self.databases.keys())


class BottySettings(BaseSettings):
    """Settings for Botty — the session awareness layer."""

    model_config = SettingsConfigDict(env_prefix="BOTTY_")

    enabled: bool = Field(default=_yaml.get("botty", {}).get("enabled", True))
    analysis_interval: int = Field(default=_yaml.get("botty", {}).get("analysis_interval", 10))
    intervention_threshold: float = Field(default=_yaml.get("botty", {}).get("intervention_threshold", 0.6))
    max_frequency_seconds: int = Field(default=_yaml.get("botty", {}).get("max_frequency_seconds", 300))
    dismissal_cooldown_seconds: int = Field(default=_yaml.get("botty", {}).get("dismissal_cooldown_seconds", 900))
    classifier_model: str = Field(default=_yaml.get("botty", {}).get("classifier_model", "cloud-light"))
    intervener_model: str = Field(default=_yaml.get("botty", {}).get("intervener_model", "cloud-light"))
    recall_top_k: int = Field(default=_yaml.get("botty", {}).get("recall", {}).get("top_k", 5))
    recall_min_score: float = Field(default=_yaml.get("botty", {}).get("recall", {}).get("min_score", 0.55))
    recall_include_facts: bool = Field(default=_yaml.get("botty", {}).get("recall", {}).get("include_facts", True))
    insights_collection: str = Field(
        default=_yaml.get("botty", {}).get("insights", {}).get("collection", "botty_insights")
    )
    store_helpful: bool = Field(default=_yaml.get("botty", {}).get("insights", {}).get("store_helpful", True))


class AgentSettings(BaseSettings):
    """Agent loop behaviour — prompt condensing, iteration limits, etc."""

    model_config = SettingsConfigDict(env_prefix="AGENT_")

    condense_tool_prompt: bool = Field(
        default=_yaml.get("agent", {}).get("condense_tool_prompt", True),
    )
    # Cap on a single tool's stringified output (chars). Oversized results
    # are truncated with a "use grep/tail" marker before being fed back to
    # the model. Too low and the model gives up + emits stub responses on
    # large listings (172-item cloud folders, big SQL result sets, …).
    max_tool_output: int = Field(
        default=int(_yaml.get("agent", {}).get("max_tool_output", 64000)),
    )


class ReviewSettings(BaseSettings):
    """@review mode — parallel multi-agent code review."""

    model_config = SettingsConfigDict(env_prefix="REVIEW_")

    # How long to wait for each review sub-agent before giving up on it.
    subagent_timeout_seconds: int = Field(
        default=int(_yaml.get("review", {}).get("subagent_timeout_seconds", 300)),
    )


class PersonaSettings(BaseSettings):
    """The RAG assistant's identity, injected into answer-generation prompts.

    Deployer-specific, so it lives in config (keeps an org name out of the
    published prompts). ``domain_context`` is an optional extra hint fed to the
    query refiner (e.g., describing your domain / product vocabulary).
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_")

    name: str = Field(default=_yaml.get("persona", {}).get("name", "AgentForge"))
    team: str = Field(default=_yaml.get("persona", {}).get("team", "your team"))
    domain_context: str = Field(default=_yaml.get("persona", {}).get("domain_context", ""))


class HashtagRoutesSettings(BaseSettings):
    """Optional private hashtag shortcuts that activate a custom agent.

    Empty by default — the published build has no hashtag routes. Configure in
    config.yaml (gitignored) to add bespoke shortcuts, e.g., ``#myservice`` -> the
    ``cloud`` agent. Keeps deployment-specific routing out of the codebase.
    """

    model_config = SettingsConfigDict(env_prefix="HASHTAG_ROUTES_")

    # The custom agent (alias without @) these hashtags activate.
    agent: str = Field(default=_yaml.get("hashtag_routes", {}).get("agent", ""))
    # hashtag -> natural-language name (used when rewriting for non-agent modes).
    tags: dict[str, str] = Field(
        default_factory=lambda: dict(_yaml.get("hashtag_routes", {}).get("tags", {})),
    )
    # Read-only tools also blended into @agent so it can do cross lookups.
    blend_tools: list[str] = Field(
        default_factory=lambda: list(_yaml.get("hashtag_routes", {}).get("blend_tools", [])),
    )


class SecuritySettings(BaseSettings):
    """Optional API-key auth for the HTTP + WebSocket surface.

    Empty list = disabled (open) — the default. Add keys to require them on
    every request. The ``AGENTFORGE_API_KEYS`` env var (comma-separated)
    overrides this list; that override is applied in ``app.security``.
    """

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    api_keys: list[str] = Field(
        default_factory=lambda: [
            str(k).strip() for k in _yaml.get("security", {}).get("api_keys", []) if str(k).strip()
        ],
    )


class SlackSettings(BaseSettings):
    """Slack bot integration — Socket Mode listener + outbound notifications."""

    model_config = SettingsConfigDict(env_prefix="SLACK_")

    enabled: bool = Field(default=_yaml.get("slack", {}).get("enabled", False))
    bot_token: str = Field(default=_yaml.get("slack", {}).get("bot_token", ""))
    app_token: str = Field(default=_yaml.get("slack", {}).get("app_token", ""))
    default_channel: str = Field(default=_yaml.get("slack", {}).get("default_channel", ""))
    reply_in_thread: bool = Field(default=_yaml.get("slack", {}).get("reply_in_thread", True))


class KnowledgeSettings(BaseSettings):
    """Personal knowledge database.
    Dedicated Qdrant collection for user-created entries.
    """

    model_config = SettingsConfigDict(env_prefix="KNOWLEDGE_")

    collection_name: str = Field(default=_yaml.get("knowledge", {}).get("collection_name", "knowledge_entries"))
    dedup_threshold: float = Field(default=_yaml.get("knowledge", {}).get("dedup_threshold", 0.92))
    composite_template: str = Field(
        default=_yaml.get("knowledge", {}).get("composite_template", "{title}\n{notes}\n{content}")
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTFORGE_")

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8100)
    log_level: str = Field(default="INFO")

    # Hard guard: how long a new WebSocket waits for background runtime init
    # before telling the client to reconnect. Normally completes in seconds.
    startup_timeout_seconds: float = Field(
        default=float(_yaml.get("service", {}).get("startup_timeout_seconds", 120.0)),
    )

    # Path to the user context Markdown file, relative to agentforge/.
    # When empty or the file does not exist the feature is silently disabled.
    user_context_file: str = Field(default=_yaml.get("user_context", ""))

    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    indexer: IndexerSettings = Field(default_factory=IndexerSettings)
    dedup: DedupSettings = Field(default_factory=DedupSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    refinement: RefinementSettings = Field(default_factory=RefinementSettings)
    prompt_refinement: PromptRefinementSettings = Field(default_factory=PromptRefinementSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    code_context: CodeContextSettings = Field(default_factory=CodeContextSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    sql_databases: SqlDatabasesSettings = Field(default_factory=SqlDatabasesSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    botty: BottySettings = Field(default_factory=BottySettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    hashtag_routes: HashtagRoutesSettings = Field(default_factory=HashtagRoutesSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)


settings = Settings()
