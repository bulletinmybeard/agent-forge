"""Equivalence-finder service: trim each candidate to a compact dossier, send
the source + the full target catalog to ``agent-heavy``, parse the ranked JSON
response, attach full ``UnifiedModel`` payloads, return.

Spec: ``.claude/specs/2026-05-23-model-equivalence-finder.md``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import HTTPException

from ..catalog_api import PROVIDERS, UnifiedModel, get_catalog

logger = logging.getLogger(__name__)

_AGENT_PROFILE = "agent-heavy"
_MIN_RESULTS = 1
_MAX_RESULTS = 10
_DEFAULT_MAX_RESULTS = 5


# -- Dossier + filter ---------------------------------------------------------


def _candidate_dossier(m: UnifiedModel) -> dict:
    """Compact JSON-line dossier of fields the LLM needs to compare. No prose,
    no ``raw``, no provider-specific tags. ~80-120 tokens per model."""
    return {
        "id": m.model_id,
        "family": m.family,
        "size": m.parameter_size,
        "type": m.type,
        "caps": m.capabilities,
        "ctx": m.context_length,
        "max_out": m.max_tokens,
        "price_in_per_1m": m.pricing.input_per_1m,
        "price_out_per_1m": m.pricing.output_per_1m,
    }


def _is_text_generation(m: UnifiedModel) -> bool:
    """``text-generation`` (DeepInfra/Ollama) or ``text->text`` (OpenRouter)."""
    if not m.type:
        return False
    t = m.type.lower()
    return t in {"text-generation", "text->text"}


def _filter_candidates(models: list[UnifiedModel]) -> tuple[list[UnifiedModel], int]:
    """Drop deprecated + non-text-generation entries. Return (kept, dropped)."""
    kept = [m for m in models if _is_text_generation(m) and not m.deprecated]
    return kept, len(models) - len(kept)


# -- Prompt assembly ----------------------------------------------------------


def _build_prompt(
    source_dossier: dict,
    source_provider: str,
    target_provider: str,
    candidates: list[dict],
    max_results: int,
) -> str:
    """Build the user prompt for the LLM. Source + candidate set + the rules."""
    candidates_jsonl = "\n".join(json.dumps(c, separators=(",", ":")) for c in candidates)
    return (
        "You are comparing AI language models to find the closest equivalents "
        "to a given source model from a list of candidates on a different provider.\n"
        "\n"
        f'SOURCE MODEL (from provider "{source_provider}"):\n'
        f"{json.dumps(source_dossier, indent=2)}\n"
        "\n"
        f'CANDIDATES (from provider "{target_provider}"; full non-deprecated '
        f"text-generation catalog, {len(candidates)} entries; one JSON per line):\n"
        f"{candidates_jsonl}\n"
        "\n"
        f"TASK: Pick the top {max_results} candidates that are the closest "
        'functional equivalents to the source. "Closest" means: similar '
        "capabilities (tools, reasoning, vision), similar quality tier (rough "
        "parameter-size band / known model family standing), similar context "
        "window when relevant, comparable pricing tier (mention if a candidate "
        "is notably cheaper or more expensive).\n"
        "\n"
        "Output a single JSON object with this exact shape (no prose outside the JSON):\n"
        "{\n"
        '  "ranked": [\n'
        '    {"id": "<candidate model_id>", "score": <0..1 float>, '
        '"reasoning": "<one or two sentences>"},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "\n"
        "Rules:\n"
        "- Do not include candidates not in the provided list.\n"
        "- score 1.0 = near-perfect substitute, 0.0 = completely unrelated.\n"
        "- Order by score, highest first.\n"
        f"- Limit to exactly {max_results} entries (fewer is fine if the list is small).\n"
        "- Keep reasoning grounded in the candidate's fields. No invented capabilities or prices.\n"
    )


_SYSTEM_PROMPT = (
    "You are a precise AI-model comparator. Output strict JSON in the requested shape. "
    "Do not include prose outside the JSON object."
)


# -- Response parsing ---------------------------------------------------------


_JSON_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(content: str) -> dict | None:
    """Pull the first parseable JSON object out of the LLM response. Tries
    fenced ```` ```json ```` blocks first, then any ``{...}`` substring."""
    if not content:
        return None
    m = _JSON_FENCED.search(content)
    candidates: list[str] = []
    if m:
        candidates.append(m.group(1))
    # Greedy outer-braces match: find the largest top-level {...} that parses.
    # Walk balanced-brace candidates.
    depth = 0
    start = -1
    for i, ch in enumerate(content):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(content[start : i + 1])
                start = -1
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _parse_ranked(
    content: str,
    valid_ids: set[str],
    max_results: int,
) -> list[dict]:
    """Pull a list of ``{id, score, reasoning}`` from the LLM response. Drops
    entries whose id isn't in ``valid_ids``, whose score isn't a number, or
    whose shape is wrong. Caps the result at ``max_results``."""
    obj = _extract_json_object(content)
    if not obj:
        raise HTTPException(
            status_code=503,
            detail="equivalence inference failed: no JSON object in LLM response",
        )
    ranked = obj.get("ranked")
    if not isinstance(ranked, list):
        raise HTTPException(
            status_code=503,
            detail="equivalence inference failed: 'ranked' missing or not a list",
        )
    out: list[dict] = []
    for entry in ranked:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or model_id not in valid_ids:
            continue
        raw_score = entry.get("score")
        try:
            score = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            continue
        if score is None:
            continue
        score = max(0.0, min(1.0, score))  # clamp
        reasoning = entry.get("reasoning")
        if not isinstance(reasoning, str):
            reasoning = ""
        out.append({"id": model_id, "score": score, "reasoning": reasoning.strip()})
    # Keep the LLM's order (it was asked to sort by score desc). Cap.
    return out[:max_results]


# -- Source resolution --------------------------------------------------------


def _resolve_source(provider: str, model_id: str) -> UnifiedModel:
    if provider not in PROVIDERS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown source provider '{provider}'. Available: {', '.join(PROVIDERS)}",
        )
    for m in get_catalog(provider):
        if m.model_id == model_id:
            return m
    raise HTTPException(
        status_code=404,
        detail=f"source model '{model_id}' not in '{provider}' catalog",
    )


# -- Public entry point -------------------------------------------------------


def find_equivalents(
    source_provider: str,
    source_model_id: str,
    targets: list[str] | None,
    max_results_per_target: int = _DEFAULT_MAX_RESULTS,
    *,
    chat_fn: Any = None,
) -> dict:
    """Return a result dict matching the spec's response shape.

    ``chat_fn`` is injectable for tests: a callable ``(messages, profile) -> str``
    returning the assistant content. When None, builds a fresh
    ``AIClient(profile="agent-heavy")`` and calls ``.chat(...)``.
    """
    max_results_per_target = max(_MIN_RESULTS, min(_MAX_RESULTS, max_results_per_target))

    source = _resolve_source(source_provider, source_model_id)
    source_dossier = _candidate_dossier(source)

    if targets is None or targets == []:
        targets = [p for p in PROVIDERS if p != source_provider]

    # Reject unknown targets up front so the request fails cleanly.
    unknown = [t for t in targets if t not in PROVIDERS]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"unknown target provider(s): {', '.join(unknown)}. Available: {', '.join(PROVIDERS)}",
        )

    if chat_fn is None:
        from agentforge.client import AIClient

        ai = AIClient(profile=_AGENT_PROFILE)

        def _do_chat(messages: list[dict]) -> str:
            resp = ai.chat(messages)
            return getattr(resp, "content", "") or ""

        chat_fn = _do_chat

    results: list[dict] = []
    for tgt in targets:
        raw_candidates = get_catalog(tgt)
        kept, _dropped = _filter_candidates(raw_candidates)
        dossiers = [_candidate_dossier(m) for m in kept]
        valid_ids = {m.model_id for m in kept}

        if not kept:
            results.append(
                {
                    "provider": tgt,
                    "candidates_considered": 0,
                    "ranked": [],
                }
            )
            continue

        prompt = _build_prompt(
            source_dossier=source_dossier,
            source_provider=source_provider,
            target_provider=tgt,
            candidates=dossiers,
            max_results=max_results_per_target,
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            content = chat_fn(messages)
        except Exception as exc:  # noqa: BLE001
            logger.exception("equivalence: LLM call failed for target %s", tgt)
            raise HTTPException(
                status_code=503,
                detail=f"equivalence inference failed: {type(exc).__name__}: {exc}",
            ) from exc

        ranked = _parse_ranked(content, valid_ids, max_results_per_target)

        # Attach full UnifiedModel payloads.
        by_id = {m.model_id: m for m in kept}
        enriched = [{"model": by_id[r["id"]], "score": r["score"], "reasoning": r["reasoning"]} for r in ranked]

        results.append(
            {
                "provider": tgt,
                "candidates_considered": len(kept),
                "ranked": enriched,
            }
        )

    return {"source": source, "results": results}
