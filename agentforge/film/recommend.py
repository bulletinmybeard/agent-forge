"""Merge TMDB similar/recs with optional Qdrant neighbors."""

from __future__ import annotations

from typing import Any


def merge_candidates(
    tmdb_items: list[dict[str, Any]],
    vector_items: list[dict[str, Any]],
    library_ids: set[int],
    *,
    limit: int = 10,
    seed_id: int | None = None,
) -> list[dict[str, Any]]:
    """Rank TMDB recs first, then vector hits. Drop seed and library ids."""
    skip = set(library_ids)
    if seed_id is not None:
        skip.add(int(seed_id))
    ranked: list[dict[str, Any]] = []
    seen: set[int] = set()

    def _take(item: dict[str, Any], why: str) -> None:
        tid = int(item.get("tmdb_id") or 0)
        if not tid or tid in skip or tid in seen:
            return
        seen.add(tid)
        out = dict(item)
        out["why"] = why
        ranked.append(out)

    for item in tmdb_items:
        src = item.get("source") or "tmdb"
        _take(item, f"TMDB {src}")
        if len(ranked) >= limit:
            return ranked[:limit]

    for item in vector_items:
        score = item.get("score")
        why = f"semantic {score:.3f}" if isinstance(score, (int, float)) else "semantic"
        _take(item, why)
        if len(ranked) >= limit:
            break
    return ranked[:limit]
