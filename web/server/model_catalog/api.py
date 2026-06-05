"""FastAPI router for the Model Catalog UI backend.

Phase 1: ``POST /api/model-catalog/equivalents`` -- LLM-driven equivalence lookup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator

from ..catalog_api import PROVIDERS, UnifiedModel
from .equivalence import find_equivalents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/model-catalog", tags=["model-catalog"])

_CACHE_TTL = 300  # 5 minutes; protects against accidental double-submits.


# -- Request / response models ------------------------------------------------


class SourceRef(BaseModel):
    provider: str
    model_id: str


class EquivalentsRequest(BaseModel):
    source: SourceRef
    targets: list[str] | None = None
    max_results_per_target: int = Field(default=5, ge=1, le=10)

    @field_validator("targets")
    @classmethod
    def _strip_empty(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = [t.strip().lower() for t in v if isinstance(t, str) and t.strip()]
        return cleaned or None


class RankedEntry(BaseModel):
    model: UnifiedModel
    score: float
    reasoning: str


class ProviderResult(BaseModel):
    provider: str
    candidates_considered: int
    ranked: list[RankedEntry]


class EquivalentsResponse(BaseModel):
    source: UnifiedModel
    results: list[ProviderResult]


# -- Redis caching (lazy, fail-soft) -----------------------------------------


_redis_client = None
_redis_attempted = False


def _get_redis():
    """Reuse the catalog_api Redis pattern: lazy connect, fall through if down."""
    global _redis_client, _redis_attempted
    if _redis_attempted:
        return _redis_client
    _redis_attempted = True
    try:
        import redis

        client = redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
        client.ping()
        _redis_client = client
        logger.info("model_catalog: Redis connected")
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_catalog: Redis unavailable (%s) -- caching disabled", exc)
        _redis_client = None
    return _redis_client


def _cache_key(req: EquivalentsRequest) -> str:
    """Stable hash of the request body. Model ids contain slashes so we hash."""
    payload = json.dumps(
        {
            "source": {"provider": req.source.provider, "model_id": req.source.model_id},
            "targets": sorted(req.targets) if req.targets else None,
            "max_results_per_target": req.max_results_per_target,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return f"equiv:{digest}"


# -- Endpoint -----------------------------------------------------------------


@router.post("/equivalents", response_model=EquivalentsResponse)
def equivalents(
    req: EquivalentsRequest,
    force: bool = Query(default=False, description="Bypass the response cache."),
) -> dict:
    """Find equivalents of ``req.source`` on each of ``req.targets`` via an LLM.

    Targets default to "all providers except the source's" when omitted.
    Result is cached in Redis for ~5 minutes keyed on the canonical request.
    """
    client = _get_redis()
    key = _cache_key(req)

    if client is not None and not force:
        try:
            cached = client.get(key)
            if cached:
                return json.loads(cached)
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_catalog: cache read failed: %s", exc)

    result = find_equivalents(
        source_provider=req.source.provider.lower(),
        source_model_id=req.source.model_id,
        targets=req.targets,
        max_results_per_target=req.max_results_per_target,
    )

    # Convert UnifiedModel pydantic instances into JSON-able dicts for the
    # cache write (they round-trip cleanly through .model_dump()).
    serialised = EquivalentsResponse.model_validate(result).model_dump()

    if client is not None:
        try:
            client.setex(key, _CACHE_TTL, json.dumps(serialised))
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_catalog: cache write failed: %s", exc)

    return serialised


@router.get("/providers")
def supported_providers() -> dict:
    """Lightweight helper for the UI: which providers have catalog support."""
    return {"providers": sorted(PROVIDERS)}
