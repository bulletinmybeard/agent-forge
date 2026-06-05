"""Catalog API — unified model-metadata endpoint across LLM providers.

Reads raw catalog JSONs at agentforge/data/catalogs/<provider>-models.json,
normalizes every entry to a single UnifiedModel schema, serves them via
``/api/catalog/<provider>``. Redis-cached with TTL (default 1h). Graceful
degrade to direct file read when Redis is unavailable.

No filtering, no scoring -- just the data layer. Equivalence / ranking belongs
to downstream consumers (future UI, CLI helper, etc.).

Endpoints:
    GET /api/catalog/providers              -- summary + counts
    GET /api/catalog/{provider}             -- normalized list
    GET /api/catalog/{provider}/{model_id}  -- single entry (model_id may have slashes)

Cache layout:
    catalog:<provider>   string, JSON-encoded list[UnifiedModel]   TTL=3600s

Bust manually:
    redis-cli DEL catalog:deepinfra catalog:openrouter catalog:ollama
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# -- Configuration -------------------------------------------------------------
#
# PROVIDERS is the single source of truth. Add a new provider by appending one
# entry (adapter function + JSON shape hints). Everything else -- the endpoint
# routing, the test smoke suite, the file loader -- derives from this dict.

_THIS_FILE = Path(__file__).resolve()


# Candidate directories tried in order on each load. First hit wins.
# Override the whole search list via env: AGENTFORGE_CATALOG_DIR=/abs/path.
def _candidate_dirs() -> list[Path]:
    explicit = os.environ.get("AGENTFORGE_CATALOG_DIR")
    if explicit:
        return [Path(explicit)]
    return [
        # Source tree (local dev + Docker build context).
        _THIS_FILE.parent.parent.parent / "data" / "catalogs",
        # In-container deployed path on remote.
        Path("/app/data/catalogs"),
        # Repo root -- legacy fallback for files dropped during exploration.
        _THIS_FILE.parent.parent.parent.parent,
    ]


def _find_catalog_file(provider: str) -> Path | None:
    config = PROVIDERS.get(provider)
    if config is None:
        return None
    for d in _candidate_dirs():
        path = d / config.filename
        if path.is_file():
            return path
    return None


_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
_CACHE_TTL = int(os.environ.get("AGENTFORGE_CATALOG_TTL", "3600"))


# -- Pydantic models -----------------------------------------------------------


class PricingTier(BaseModel):
    """Normalized $/1M tokens. Either field may be None when the provider
    doesn't expose token-based pricing (e.g.,, Ollama local relay)."""

    input_per_1m: float | None = None
    output_per_1m: float | None = None


class UnifiedModel(BaseModel):
    """The single shape every endpoint returns, regardless of provider."""

    id: str
    provider: str
    model_id: str
    name: str
    family: str | None = None
    description: str | None = None
    type: str | None = None
    context_length: int | None = None
    max_tokens: int | None = None
    pricing: PricingTier = Field(default_factory=PricingTier)
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    parameter_size: str | None = None
    deprecated: bool = False
    # True/False for Ollama (after refresh-ollama-cloud-flag.py runs); always
    # True for cloud-only providers (DeepInfra, OpenRouter); None when the flag
    # hasn't been determined yet (Ollama entry without the field).
    is_cloud: bool | None = None
    # ISO 8601 UTC timestamp of the model's last update on the provider.
    # Sources: DeepInfra `create_ts`, OpenRouter `created` (unix epoch),
    # Ollama scraped from /library/<slug> (parsed from "May 21, 2026 7:16 PM UTC").
    # None if the provider doesn't expose one or parsing failed.
    last_updated: str | None = None
    raw: dict[str, Any]


# -- Capability normalization --------------------------------------------------

_CAPABILITY_TOKEN_MAP: dict[str, str] = {
    # DeepInfra tags
    "tools": "tools",
    "tool-calling": "tools",
    "function-calling": "tools",
    "vision": "vision",
    "multimodal": "multimodal",
    "reasoning": "reasoning",
    "can-disable-reasoning": "reasoning",
    "thinking": "reasoning",  # Ollama cloud-page capability chip
    "code": "code",
    "coding": "code",
    "embeddings": "embedding",
    "embedding": "embedding",
    "audio": "audio",
    # DeepInfra/OpenRouter type / architecture values
    "text-generation": "text-generation",
    "text-to-image": "image-generation",
    "image-to-image": "image-generation",
    "speech-to-text": "audio",
    "text-to-speech": "audio",
    "multimodal-text-image": "multimodal",
    # OpenRouter supported_parameters
    "tool_choice": "tools",
    "tools_choice": "tools",
    "reasoning_effort": "reasoning",
}


def _normalize_capabilities(raw_tokens: list[Any]) -> list[str]:
    """Map provider-specific tokens to canonical ones; dedupe + sort."""
    out: set[str] = set()
    for tok in raw_tokens:
        if not tok or not isinstance(tok, str):
            continue
        canonical = _CAPABILITY_TOKEN_MAP.get(tok.lower())
        if canonical:
            out.add(canonical)
    return sorted(out)


# Name-pattern capability inference. Provider-agnostic so cross-provider lookups
# stay consistent: ``Qwen3-Coder-480B`` gets ``code`` whether DeepInfra tagged it
# or not, ``qwen3-vl`` gets ``vision``, ``kimi-k2-thinking`` gets ``reasoning``.
# These signals come from the model name itself; the provider's own tags/
# supported_parameters still flow through ``_normalize_capabilities`` and union
# with these.
_NAME_CAPABILITY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?:^|[-/.])vl(?:[-:.]|$)", re.IGNORECASE), "vision"),
    (re.compile(r"(?:^|[-/.])coder(?:[-:.]|$)", re.IGNORECASE), "code"),
    (re.compile(r"-thinking(?:[-:.]|$)", re.IGNORECASE), "reasoning"),
)


def _infer_capabilities_from_name(name: str) -> list[str]:
    """Best-effort capability tokens derived from naming conventions."""
    if not name:
        return []
    base = name.lower().split(":", 1)[0]
    out: set[str] = set()
    for pattern, cap in _NAME_CAPABILITY_PATTERNS:
        if pattern.search(base):
            out.add(cap)
    return sorted(out)


def _combined_capabilities(provider_tokens: list[Any], *names: str) -> list[str]:
    """Union of normalized provider tags + name-pattern inference. Sorted, deduped."""
    out = set(_normalize_capabilities(provider_tokens))
    for name in names:
        out.update(_infer_capabilities_from_name(name))
    return sorted(out)


# -- Timestamp normalization --------------------------------------------------


def _to_iso_utc(value: Any) -> str | None:
    """Normalize any provider's timestamp form to an ISO 8601 UTC string.

    Accepted inputs:
    - ``None`` / empty                                  -> ``None``
    - int / float                (unix epoch seconds)   -> ISO string
    - ISO-ish string             ("...Z" or "...+00:00") -> ISO string
    - "May 21, 2026 7:16 PM UTC" (Ollama library page)  -> ISO string

    Returns ``None`` on unrecognised input rather than raising; callers can
    safely use the result as a serialisable field.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        for candidate in (s, s.replace("Z", "+00:00")):
            try:
                dt = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        # Ollama library-page format: "May 21, 2026 7:16 PM UTC".
        try:
            dt = datetime.strptime(s, "%b %d, %Y %I:%M %p UTC")
        except ValueError:
            return None
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return None


# -- Parsing helpers -----------------------------------------------------------


def _family_from_prefix(model_id: str) -> str | None:
    """`Qwen/Qwen3.5-35B-A3B` -> `qwen`, `anthropic/claude-haiku-4.5` -> `anthropic`."""
    if "/" in model_id:
        prefix = model_id.split("/", 1)[0].strip().lower()
        return prefix or None
    return None


def _ollama_family_from_name(name: str) -> str | None:
    """Derive family from an Ollama model name when ``details.family`` is empty.

    ``gemma3:4b`` -> ``gemma``, ``qwen3.5:397b`` -> ``qwen``,
    ``kimi-k2-thinking`` -> ``kimi``, ``deepseek-v3.2`` -> ``deepseek``.

    Strategy: drop the tag (``:NNN``), take the first hyphen-segment, strip
    trailing digits / dots (the version glued onto the name).
    """
    if not name:
        return None
    base = name.split(":", 1)[0].lower()
    first_seg = base.split("-", 1)[0]
    family = re.sub(r"[\d.]+$", "", first_seg)
    return family or None


# Matches parameter-size patterns like 35B, 397B-A17B, 1.1T, 14B, 70B-Instruct
_PARAM_SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?[BTK](?:-A\d+(?:\.\d+)?[BT])?)",
    re.IGNORECASE,
)


def _parameter_size_from_text(text: str) -> str | None:
    """Extract ``35B``, ``397B-A17B``, ``1.1T`` etc. -- uppercase for consistency."""
    if not text:
        return None
    m = _PARAM_SIZE_RE.search(text)
    return m.group(1).upper() if m else None


# DeepInfra pricing strings observed in the wild (2026-05):
#
#   "$0.10 in $0.40 out $0.02 cached <= 128K, $0.2 in $0.80 out $0.2 cached"
#   "$1.20 in $6.00 out $0.24 cached / 1M tokens"
#   "$0.20/1M input, $0.95/1M output"           (older docs format -- kept as fallback)
#   "$0.10/1M"                                   (single-figure)
#
# We try the "$X in $Y out" pair pattern first (the one DeepInfra actually
# ships today). Tier-based strings start with the cheapest tier, so the first
# match is the right floor price to surface.
_PRICE_IN_OUT_PAIR_RE = re.compile(
    r"\$(\d+(?:\.\d+)?)\s+in\s+\$(\d+(?:\.\d+)?)\s+out",
    re.IGNORECASE,
)
_PRICE_IN_RE = re.compile(r"\$([\d.]+)\s*/\s*1M\s*input", re.IGNORECASE)
_PRICE_OUT_RE = re.compile(r"\$([\d.]+)\s*/\s*1M\s*output", re.IGNORECASE)
_PRICE_GENERIC_RE = re.compile(r"\$([\d.]+)\s*/\s*1M", re.IGNORECASE)


def _parse_deepinfra_pricing(pricing: dict | None) -> PricingTier:
    if not pricing:
        return PricingTier()
    full = pricing.get("full") or ""
    short = pricing.get("short") or ""
    text = f"{full} {short}"

    # 1. Natural-language pair: "$0.10 in $0.40 out". DeepInfra's current shape.
    pair = _PRICE_IN_OUT_PAIR_RE.search(text)
    if pair:
        return PricingTier(
            input_per_1m=float(pair.group(1)),
            output_per_1m=float(pair.group(2)),
        )

    # 2. Older "/1M input" + "/1M output" form (kept for back-compat).
    in_match = _PRICE_IN_RE.search(text)
    out_match = _PRICE_OUT_RE.search(text)
    if in_match or out_match:
        return PricingTier(
            input_per_1m=float(in_match.group(1)) if in_match else None,
            output_per_1m=float(out_match.group(1)) if out_match else None,
        )

    # 3. Single-figure fallback ("$0.10/1M") -- assume input == output.
    generic = _PRICE_GENERIC_RE.search(text)
    if generic:
        val = float(generic.group(1))
        return PricingTier(input_per_1m=val, output_per_1m=val)

    return PricingTier()


def _parse_openrouter_pricing(pricing: dict | None) -> PricingTier:
    """OpenRouter quotes per-token prices as strings like `'0.0000002'`.
    Multiply by 1e6 to get $/1M tokens."""
    if not pricing:
        return PricingTier()

    def _scale(v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f <= 0:
            return None
        return round(f * 1_000_000, 6)

    return PricingTier(
        input_per_1m=_scale(pricing.get("prompt")),
        output_per_1m=_scale(pricing.get("completion")),
    )


# -- Adapters ------------------------------------------------------------------


def deepinfra_to_unified(entry: dict) -> UnifiedModel:
    model_name = entry.get("model_name", "") or ""
    tags = list(entry.get("tags") or [])
    name = model_name.rsplit("/", 1)[-1] if "/" in model_name else model_name
    raw_type = entry.get("type") or entry.get("reported_type")
    return UnifiedModel(
        id=f"deepinfra/{model_name}",
        provider="deepinfra",
        model_id=model_name,
        name=name,
        family=_family_from_prefix(model_name),
        description=entry.get("description"),
        type=raw_type,
        context_length=None,  # DeepInfra surfaces this in description prose; skip
        max_tokens=entry.get("max_tokens"),
        pricing=_parse_deepinfra_pricing(entry.get("pricing")),
        capabilities=_combined_capabilities([*tags, raw_type], model_name),
        tags=tags,
        parameter_size=_parameter_size_from_text(model_name),
        deprecated=bool(entry.get("deprecated")) or bool(entry.get("replaced_by")),
        is_cloud=True,  # DeepInfra is a cloud-only provider
        last_updated=_to_iso_utc(entry.get("create_ts")),
        raw=entry,
    )


def openrouter_to_unified(entry: dict) -> UnifiedModel:
    model_id = entry.get("id", "") or ""
    # `architecture` is a dict in the current OpenRouter API
    # ({"modality": "text->text", "input_modalities": [...], ...}).  Older
    # entries shipped it as a bare string -- handle both.
    arch_raw = entry.get("architecture")
    modalities: list[str] = []
    if isinstance(arch_raw, dict):
        arch = str(arch_raw.get("modality") or "text-generation")
        for k in ("input_modalities", "output_modalities"):
            v = arch_raw.get(k)
            if isinstance(v, list):
                modalities.extend(str(x) for x in v)
    elif isinstance(arch_raw, str) and arch_raw:
        arch = arch_raw
    else:
        arch = "text-generation"

    # `top_provider` is a dict ({"context_length", "max_completion_tokens",
    # "is_moderated"}). Pull max_completion_tokens through as max_tokens.
    tp_raw = entry.get("top_provider")
    max_tokens = None
    if isinstance(tp_raw, dict):
        mct = tp_raw.get("max_completion_tokens")
        if isinstance(mct, int):
            max_tokens = mct

    supported = [str(s) for s in (entry.get("supported_parameters") or [])]
    name = entry.get("name") or model_id.rsplit("/", 1)[-1]
    is_deprecated = False
    expiration = entry.get("expiration_date")
    if expiration:
        try:
            exp_dt = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
            is_deprecated = exp_dt < datetime.now(timezone.utc)
        except (TypeError, ValueError):
            pass
    return UnifiedModel(
        id=f"openrouter/{model_id}",
        provider="openrouter",
        model_id=model_id,
        name=name,
        family=_family_from_prefix(model_id),
        description=entry.get("description"),
        type=arch,
        context_length=entry.get("context_length"),
        max_tokens=max_tokens,
        pricing=_parse_openrouter_pricing(entry.get("pricing")),
        capabilities=_combined_capabilities([arch, *supported, *modalities], model_id),
        tags=supported,
        parameter_size=_parameter_size_from_text(model_id),
        deprecated=is_deprecated,
        is_cloud=True,  # OpenRouter is a cloud-only provider
        last_updated=_to_iso_utc(entry.get("created")),
        raw=entry,
    )


def ollama_to_unified(entry: dict) -> UnifiedModel:
    name = entry.get("name", "") or ""
    details = entry.get("details") or {}
    # ``details.family`` is empty in `ollama list` for most cloud models, so
    # fall back to deriving from the name itself.
    family = (details.get("family") or "").strip().lower() or _ollama_family_from_name(name)
    # refresh-ollama-cloud-flag.py scrapes ``/library/<slug>`` for every catalog
    # entry (cloud + local) and writes the ``library_*`` block.
    return UnifiedModel(
        id=f"ollama/{name}",
        provider="ollama",
        model_id=name,
        name=name,
        family=family,
        description=entry.get("library_description"),
        type="text-generation",  # Ollama JSON enumerates text-gen models only
        context_length=None,
        max_tokens=None,
        pricing=PricingTier(),
        # Capabilities = canonical-mapped library chips (tools / thinking /
        # vision / audio / embedding) unioned with name-pattern inference
        # (vl -> vision, coder -> code, -thinking -> reasoning).
        capabilities=_combined_capabilities(entry.get("library_capabilities") or [], name),
        # Tags = size variants (e2b, 26b, ...) + other coloured chips on the
        # library page (e.g., the ``cloud`` badge). Passed through verbatim.
        tags=list(entry.get("library_tags") or []),
        # `details.parameter_size` is usually empty in `ollama list` -- fall
        # back to parsing it out of the name (e.g., "gemma3:4b" -> "4B").
        parameter_size=((details.get("parameter_size") or "").strip().upper() or _parameter_size_from_text(name)),
        deprecated=False,
        # is_cloud is True iff the slug appears on https://ollama.com/search?c=cloud
        # (populated by refresh-ollama-cloud-flag.py).
        is_cloud=entry.get("is_cloud"),
        last_updated=_to_iso_utc(entry.get("library_updated_at")),
        raw=entry,
    )


@dataclass(frozen=True)
class ProviderConfig:
    """Catalog metadata for one LLM provider.

    Add a new provider by appending one entry to PROVIDERS below. Nothing else
    in this file (or tests) should hardcode provider names.
    """

    name: str
    adapter: Callable[[dict], UnifiedModel]
    filename: str  # e.g., "deepinfra-models.json"
    wrapper_key: str | None  # JSON top-level key; None = bare list


PROVIDERS: dict[str, ProviderConfig] = {
    "deepinfra": ProviderConfig(
        name="deepinfra",
        adapter=deepinfra_to_unified,
        filename="deepinfra-models.json",
        wrapper_key=None,  # ships as a bare JSON array
    ),
    "openrouter": ProviderConfig(
        name="openrouter",
        adapter=openrouter_to_unified,
        filename="openrouter-models.json",
        wrapper_key="data",
    ),
    "ollama": ProviderConfig(
        name="ollama",
        adapter=ollama_to_unified,
        filename="ollama-models.json",
        wrapper_key="models",
    ),
}


# -- Cache layer (Redis, lazy, fail-soft) -------------------------------------

_redis_client = None
_redis_attempted = False


def _get_redis():
    """Lazy connect once. Returns None if Redis is unreachable -- callers must
    handle that and fall through to direct file read."""
    global _redis_client, _redis_attempted
    if _redis_attempted:
        return _redis_client
    _redis_attempted = True
    try:
        import redis

        client = redis.from_url(_REDIS_URL, decode_responses=True)
        client.ping()
        _redis_client = client
        logger.info("CatalogAPI: Redis connected at %s", _REDIS_URL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CatalogAPI: Redis unavailable (%s) -- direct file reads", exc)
        _redis_client = None
    return _redis_client


def _cache_key(provider: str) -> str:
    return f"catalog:{provider}"


# -- Service layer -------------------------------------------------------------


def _load_raw(provider: str) -> list[dict]:
    """Read JSON from disk; return the unwrapped entry list."""
    config = PROVIDERS.get(provider)
    path = _find_catalog_file(provider)
    if path is None or config is None:
        searched = ", ".join(str(d) for d in _candidate_dirs())
        filename = config.filename if config else f"{provider}-models.json"
        raise HTTPException(
            status_code=503,
            detail=(
                f"catalog file for '{provider}' not found. "
                f"Searched: {searched}. Set AGENTFORGE_CATALOG_DIR or place "
                f"{filename} in one of those directories."
            ),
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"catalog file {path} is invalid JSON: {exc}",
        ) from exc

    # Bare list wins; otherwise try the provider's declared wrapper_key, then
    # fall through to any top-level list value (defensive against schema drift).
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        if config.wrapper_key and isinstance(obj.get(config.wrapper_key), list):
            return obj[config.wrapper_key]
        for v in obj.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v
    raise HTTPException(
        status_code=503,
        detail=(f"catalog file {path} is neither a bare array nor an object with a top-level list of entries"),
    )


def _normalize_all(provider: str, raw_entries: list[dict]) -> list[UnifiedModel]:
    """Adapter pass; log + skip entries that crash, don't fail the whole call."""
    adapter = PROVIDERS[provider].adapter
    out: list[UnifiedModel] = []
    for i, entry in enumerate(raw_entries):
        try:
            out.append(adapter(entry))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CatalogAPI: %s adapter failed on entry %d: %s",
                provider,
                i,
                exc,
            )
    return out


def get_catalog(provider: str, *, force_refresh: bool = False) -> list[UnifiedModel]:
    """Return the normalized model list for *provider*, Redis-cached."""
    if provider not in PROVIDERS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown provider '{provider}'. Available: {', '.join(PROVIDERS)}",
        )

    client = _get_redis()
    key = _cache_key(provider)

    if not force_refresh and client is not None:
        try:
            cached = client.get(key)
            if cached:
                return [UnifiedModel(**m) for m in json.loads(cached)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("CatalogAPI: cache read failed for %s: %s", provider, exc)

    raw = _load_raw(provider)
    models = _normalize_all(provider, raw)

    if client is not None:
        try:
            payload = json.dumps([m.model_dump() for m in models])
            client.setex(key, _CACHE_TTL, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CatalogAPI: cache write failed for %s: %s", provider, exc)

    return models


def _is_cached(provider: str) -> bool:
    client = _get_redis()
    if not client:
        return False
    try:
        return bool(client.exists(_cache_key(provider)))
    except Exception:  # noqa: BLE001
        return False


# -- FastAPI router ------------------------------------------------------------

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/providers")
def list_providers() -> dict:
    """List supported providers with model counts and cache state."""
    items = []
    for provider in PROVIDERS:
        cached = _is_cached(provider)
        try:
            count = len(get_catalog(provider))
        except HTTPException:
            count = 0
        items.append({"name": provider, "count": count, "cached": cached})
    return {"providers": items}


@router.get("/{provider}")
def list_models(
    provider: str,
    family: str | None = None,
    capability: str | None = None,
    model_type: str | None = None,
    parameter_size: str | None = None,
    min_context_length: int | None = None,
    deprecated: Literal["false", "true", "any"] = "false",
    is_cloud: Literal["true", "false", "any"] | None = None,
    limit: int | None = None,
) -> dict:
    """Normalized model list. Same JSON shape for every provider.

    Filters (all optional, AND-combined). Filters that don't apply to a
    provider's catalog (e.g.,, ``deprecated`` against Ollama, which never sets
    that flag) just return empty matches -- no error.

    - ``family``              exact, case-insensitive (e.g., ``qwen``, ``anthropic``)
    - ``capability``          membership in ``UnifiedModel.capabilities``
                              (e.g., ``tools``, ``vision``, ``reasoning``, ``code``)
    - ``model_type``          exact, case-insensitive (e.g., ``text-generation``)
    - ``parameter_size``      exact, case-insensitive (e.g., ``35B-A3B``, ``70B``)
    - ``min_context_length``  numeric, ``UnifiedModel.context_length >= value``
    - ``deprecated``          ``false`` (default), ``true``, or ``any``
    - ``is_cloud``            ``true``, ``false``, or ``any``. Default unset =
                              no filter. Useful primarily for Ollama (cloud vs
                              local-only); cloud-only providers (DeepInfra,
                              OpenRouter) always match ``true``.
    - ``limit``               cap the result size
    """
    models = get_catalog(provider)
    models = _apply_filters(
        models,
        family=family,
        capability=capability,
        model_type=model_type,
        parameter_size=parameter_size,
        min_context_length=min_context_length,
        deprecated=deprecated,
        is_cloud=is_cloud,
    )
    if limit and limit > 0:
        models = models[:limit]
    return {
        "provider": provider,
        "count": len(models),
        "models": models,
    }


def _apply_filters(
    models: list[UnifiedModel],
    *,
    family: str | None,
    capability: str | None,
    model_type: str | None,
    parameter_size: str | None,
    min_context_length: int | None,
    deprecated: str,
    is_cloud: str | None = None,
) -> list[UnifiedModel]:
    """AND-combine all filters. Each filter is a no-op when its value is None."""
    if family:
        target = family.strip().lower()
        models = [m for m in models if (m.family or "").lower() == target]
    if capability:
        target = capability.strip().lower()
        models = [m for m in models if target in (c.lower() for c in m.capabilities)]
    if model_type:
        target = model_type.strip().lower()
        models = [m for m in models if (m.type or "").lower() == target]
    if parameter_size:
        target = parameter_size.strip().lower()
        models = [m for m in models if (m.parameter_size or "").lower() == target]
    if min_context_length is not None and min_context_length > 0:
        models = [m for m in models if m.context_length is not None and m.context_length >= min_context_length]
    if deprecated == "false":
        models = [m for m in models if not m.deprecated]
    elif deprecated == "true":
        models = [m for m in models if m.deprecated]
    # deprecated == "any" -> no filter

    if is_cloud == "true":
        models = [m for m in models if m.is_cloud is True]
    elif is_cloud == "false":
        models = [m for m in models if m.is_cloud is False]
    # is_cloud == "any" or None -> no filter (None entries pass through)
    return models


@router.get("/{provider}/{model_id:path}")
def get_model(provider: str, model_id: str) -> UnifiedModel:
    """Look up one entry by raw provider model_id (may contain slashes)."""
    for m in get_catalog(provider):
        if m.model_id == model_id:
            return m
    raise HTTPException(
        status_code=404,
        detail=f"model '{model_id}' not found in {provider} catalog",
    )
