"""Inner AgentLoop for film_recommend_ai — diversify off TMDB same-director bias."""

from __future__ import annotations

import logging
import threading
import time

from agentforge.agent import AgentLoop
from agentforge.client import AIClient
from agentforge.film.recommend import merge_candidates
from agentforge.film.store import get_store
from agentforge.film.tmdb import fetch_media, similar_and_recommended
from agentforge.film.vectors import search_neighbors
from agentforge.review.loop import extract_agent_loop_output
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.tmdb import register_tmdb_tools

logger = logging.getLogger(__name__)

_INNER_TOOLS = [
    "movie_search",
    "movie_details",
    "tv_search",
    "tv_details",
    "multi_search",
    "film_get",
    "film_search",
]

_SYSTEM = """You pick 5-8 recommendations similar in mood/genre to a seed title.

The seed, its TMDB id, director, and the full in-library TMDB id list are already in the user message. Do not look up the seed. Do not film_get or film_search to skip library titles. Look up the seed only if the user message has no director.

Rules:
- Do not recommend other films by the seed director unless no alternative exists. Say so if you make an exception.
- In-library TMDB ids are forbidden.
- The TMDB similar/recs dump is a biased starting pool (often the same director/franchise). You may keep a title from it only after you know the director. Prefer other directors in the same mood.
- Do not invent TMDB ids. Call movie_details or tv_details only for a title you might add that has no director yet.
- Do not add to the library, do not use lists, do not use web or shell.
- Final answer: a numbered list only. Each line: title (year) — TMDB id — director — one-line why.
"""

_CACHE: dict[tuple[str, int, tuple[int, ...]], tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600.0


def _cache_key(kind: str, seed_id: int, library: set[int]) -> tuple[str, int, tuple[int, ...]]:
    return (kind, int(seed_id), tuple(sorted(library)))


def _cache_get(key: tuple[str, int, tuple[int, ...]]) -> str | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        ts, text = hit
        if now - ts > _CACHE_TTL:
            _CACHE.pop(key, None)
            return None
        return text


def _cache_put(key: tuple[str, int, tuple[int, ...]], text: str) -> None:
    now = time.monotonic()
    cutoff = now - _CACHE_TTL
    with _CACHE_LOCK:
        dead = [k for k, (ts, _) in _CACHE.items() if ts < cutoff]
        for k in dead:
            _CACHE.pop(k, None)
        _CACHE[key] = (now, text)


def clear_ai_recommend_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def run_ai_recommend(*, seed_title: str = "", tmdb_id: str = "", media_type: str = "movie") -> str:
    kind = "tv" if media_type.lower() in ("tv", "show", "series") else "movie"
    seed_id = int(tmdb_id) if str(tmdb_id).strip().isdigit() else None
    seed_label = seed_title.strip() or (f"TMDB {seed_id}" if seed_id else "")
    director = ""
    if seed_id is None and seed_title.strip():
        from agentforge.tools.tmdb import _tmdb_request

        endpoint = "/search/tv" if kind == "tv" else "/search/movie"
        data = _tmdb_request(endpoint, {"query": seed_title.strip()})
        results = data.get("results") or []
        if not results:
            return f"Could not resolve '{seed_title}' on TMDB."
        first = results[0]
        seed_id = int(first["id"])
        seed_label = first.get("title") or first.get("name") or seed_title
        date = first.get("release_date") or first.get("first_air_date") or ""
        if date[:4].isdigit():
            seed_label = f"{seed_label} ({date[:4]})"
    if seed_id is None:
        return "Pass seed_title or tmdb_id."

    library = get_store().library_tmdb_ids()
    key = _cache_key(kind, seed_id, library)
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        seed = fetch_media(kind, seed_id)
        director = seed.get("director") or ""
        seed_label = f"{seed.get('title')} ({seed.get('year') or '?'})"
    except Exception as exc:
        logger.debug("seed details skipped: %s", exc)

    tmdb_items = similar_and_recommended(kind, seed_id)
    vector_items = search_neighbors(seed_title.strip() or seed_label, limit=10)
    pool = merge_candidates(tmdb_items, vector_items, library, limit=12, seed_id=seed_id)
    pool_lines = (
        "\n".join(f"- {p.get('title')} ({p.get('year') or '?'}) TMDB {p.get('tmdb_id')} [{p.get('why')}]" for p in pool)
        or "(empty)"
    )
    lib_ids = ", ".join(str(i) for i in sorted(library)) or "(none)"

    user = (
        f"Seed: {seed_label} TMDB {seed_id}. Director: {director or '(look up)'}.\n"
        f"In-library TMDB ids ({len(library)}): {lib_ids}\n\n"
        f"TMDB similar/recs pool (do not copy blindly):\n{pool_lines}\n\n"
        "Recommend 5-8 titles. Different director from the seed. Not in the library."
    )

    from agentforge.tools.film import film_get, film_search

    reg = ToolRegistry()
    register_tmdb_tools(reg)
    reg.register(film_get)
    reg.register(film_search)

    client = AIClient(profile="agent")
    agent = AgentLoop(
        client,
        reg,
        system_prompt=_SYSTEM,
        tools=_INNER_TOOLS,
        max_iterations=6,
        verbose=False,
        iter_timeout=120,
        stream_final=False,
    )
    ctx = agent.run(user)
    text, _calls, _tok = extract_agent_loop_output(ctx)
    if not (text or "").strip():
        return f"AI recommend produced no list for {seed_label}."
    out = f"AI picks like {seed_label}" + (f" (avoid director: {director})" if director else "") + ":\n" + text.strip()
    _cache_put(key, out)
    return out
