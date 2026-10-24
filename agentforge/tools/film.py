"""Local film library tools — add, search, recommend, get."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agentforge.film.import_simkl import import_watchlist
from agentforge.film.recommend import merge_candidates
from agentforge.film.store import get_store
from agentforge.film.tmdb import fetch_media, fetch_season, similar_and_recommended
from agentforge.film.vectors import collection_name, collection_point_count, search_neighbors
from agentforge.film.vectors import upsert_media as vector_upsert

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def _format_row(row: dict) -> str:
    year = row.get("year") or "N/A"
    rating = row.get("vote_average")
    rating_s = f" {rating}/10" if rating not in (None, "") else ""
    status = row.get("status") or ""
    status_s = f" [{status}]" if status else ""
    why = row.get("why")
    why_s = f" — {why}" if why else ""
    return f"- {row.get('title')} ({year}) [{row.get('type')}] TMDB {row.get('tmdb_id')}{rating_s}{status_s}{why_s}"


@tool
def film_add(tmdb_id: str, media_type: str = "movie") -> str:
    """Add a movie or TV show from TMDB into the local film_db library.

    When to use: The user wants to save a title they looked up into the library.
    When NOT to use: They only want information (use movie_details / tv_details).
    Input: tmdb_id - TMDB numeric id. media_type - movie or tv (default movie).
    Output: Confirmation with title, year, and TMDB id.
    """
    kind = "tv" if media_type.lower() in ("tv", "show", "series") else "movie"
    try:
        payload = fetch_media(kind, int(tmdb_id))
    except Exception as exc:
        return f"Error fetching TMDB {kind} {tmdb_id}: {exc}"
    row = get_store().upsert_media(payload)
    vector_upsert(row)
    return f"Added to film_db: {_format_row(row)}"


def _parse_int(value: str, label: str) -> int | str:
    raw = (value or "").strip()
    if not raw:
        return f"Pass {label}."
    try:
        return int(raw)
    except ValueError:
        return f"{label} must be an integer."


def _show_tmdb_id(tmdb_id: str, title: str) -> int | str:
    if (tmdb_id or "").strip():
        parsed = _parse_int(tmdb_id, "tmdb_id")
        return parsed
    if (title or "").strip():
        rows = get_store().get_by_title(title)
        tv = [r for r in rows if r.get("type") == "tv"]
        if not tv:
            return f"No TV library match for '{title}'. Pass tmdb_id or film_add the show first."
        if len(tv) > 1:
            bits = ", ".join(f"{r['title']} ({r.get('year')}) TMDB {r.get('tmdb_id')}" for r in tv[:8])
            return f"Multiple TV matches for '{title}': {bits}. Pass tmdb_id."
        tid = tv[0].get("tmdb_id")
        if tid in (None, ""):
            return f"'{title}' has no TMDB id in film_db."
        return int(tid)
    return "Pass tmdb_id or title."


@tool
def film_add_season(season: str, tmdb_id: str = "", title: str = "") -> str:
    """Persist one TV season's TMDB episodes into film_db (source=tmdb only).

    When to use: Store episode titles/overviews/air dates locally after tv_season
        or when the user asks to save a season. TMDB gaps stay empty.
    When NOT to use: Live lookup (tv_season). Filling plots from web/Fandom.
    Input: season number. tmdb_id or a unique TV title already in film_db.
    Output: How many episodes stored; counts of empty TMDB names/overviews.
    """
    tid = _show_tmdb_id(tmdb_id, title)
    if isinstance(tid, str):
        return tid
    season_n = _parse_int(season, "season")
    if isinstance(season_n, str):
        return season_n
    try:
        payload = fetch_season(tid, season_n)
    except Exception as exc:
        return f"Error fetching TMDB season {season_n} for {tid}: {exc}"
    episodes = payload.get("episodes") or []
    if not episodes:
        return f"TMDB season {season_n} for show {tid} has no episodes."
    n = get_store().upsert_episodes(tid, episodes)
    untitled = sum(1 for e in episodes if not e.get("name"))
    no_plot = sum(1 for e in episodes if not e.get("overview"))
    label = payload.get("name") or f"Season {payload.get('season')}"
    lines = [f"Stored {n} TMDB episodes for {label} (show {tid}, source=tmdb)."]
    if untitled or no_plot:
        lines.append(f"TMDB left {untitled} titles and {no_plot} overviews empty; not filled from the web.")
    return " ".join(lines)


@tool
def film_get_season(season: str, tmdb_id: str = "", title: str = "") -> str:
    """Read a stored TV season from film_db (not a live TMDB call).

    When to use: Episode list already saved with film_add_season.
    When NOT to use: Live TMDB (tv_season). Show row (film_get).
    Input: season number. tmdb_id or unique TV title in film_db.
    Output: Stored episodes; empty name/overview stay blank.
    """
    tid = _show_tmdb_id(tmdb_id, title)
    if isinstance(tid, str):
        return tid
    season_n = _parse_int(season, "season")
    if isinstance(season_n, str):
        return season_n
    rows = get_store().get_season(tid, season_n)
    if not rows:
        return f"No stored episodes for show {tid} season {season_n}. Use film_add_season after tv_season."
    lines = [f"film_db season {season_n} (show {tid}, {len(rows)} episodes):"]
    for row in rows:
        name = (row.get("name") or "").strip() or "(untitled)"
        air = row.get("air_date") or "N/A"
        runtime = row.get("runtime")
        runtime_s = f" {runtime} min" if runtime not in (None, "") else ""
        lines.append(f"  S{int(row['season']):02d}E{int(row['episode']):02d} {name} ({air}){runtime_s}")
        blurb = (row.get("overview") or "").strip()
        if blurb:
            lines.append(f"    {blurb}")
    return "\n".join(lines)


@tool
def film_stats() -> str:
    """Exact library counts from SQLite film_db (and Qdrant point count if available).

    When to use: 'how many films', 'how many movies/shows', 'library size', counts only.
        Call this ONCE. Do not probe film_search with genres or letters to count.
    When NOT to use: Listing titles (use film_search) or recommendations (film_recommend).
    Output: totals by type and status, plus Qdrant collection size.
    """
    try:
        stats = get_store().stats()
    except Exception as exc:
        return f"Error reading film_db: {type(exc).__name__}: {exc}"
    lines = [f"film_db SQLite: {stats['total']} titles"]
    for kind in sorted(stats.get("by_type") or {}):
        n = stats["by_type"][kind]
        label = "movies" if kind == "movie" else "TV shows" if kind == "tv" else kind
        lines.append(f"  {label}: {n}")
    status = stats.get("by_status") or {}
    if status:
        bits = [f"{key} {status[key]}" for key in sorted(status)]
        lines.append("  by status: " + ", ".join(bits))
    try:
        qpair = collection_point_count()
        qname = qpair[0] if qpair else collection_name()
        pts = qpair[1] if qpair and len(qpair) > 1 else None
    except Exception as exc:
        qname, pts = collection_name(), None
        lines.append(f"Qdrant: {type(exc).__name__}: {exc}")
    else:
        if pts is None:
            lines.append(f"Qdrant {qname}: unreachable")
        else:
            lines.append(f"Qdrant {qname}: {pts} points")
    return "\n".join(lines)


_STATUSES = frozenset({"completed", "watching", "plantowatch", "hold", "dropped"})


@tool
def film_set_status(
    status: str,
    tmdb_id: str = "",
    title: str = "",
    list: str = "",
    entire_library: str = "",
) -> str:
    """Set watch status on one title, every title on a named list, or the whole library.

    When to use: 'mark everything completed', 'mark all titles in Ingrids Filme completed',
        'set Alien to watching'.
    Input: status - completed, watching, plantowatch, hold, or dropped.
        list - list name or id (all members). tmdb_id or title - one title.
        entire_library=true - every row in film_db.
    """
    status = (status or "").strip().lower()
    if status not in _STATUSES:
        return f"Unknown status '{status}'. Use completed, watching, plantowatch, hold, or dropped."
    store = get_store()
    flag = (entire_library or "").strip().lower() in ("1", "true", "yes", "all")
    if flag:
        n = store.set_status_all(status)
        return f"Set status={status} on {n} library titles."
    if (list or "").strip():
        lst = _resolve_list(list)
        if isinstance(lst, str):
            return lst
        n = store.set_status_for_list(int(lst["id"]), status)
        return f"Set status={status} on {n} titles in '{lst['name']}'."
    if not tmdb_id.strip() and not title.strip():
        return "Pass list, tmdb_id/title, or entire_library=true."
    media = _media_for_list(tmdb_id, title)
    if isinstance(media, str):
        return media
    store.set_status(int(media["id"]), status)
    return f"Set {media.get('title')} to {status}."


@tool
def film_get(tmdb_id: str = "", title: str = "") -> str:
    """Look up a title already in the local film_db (not a live TMDB call).

    When to use: Check whether a movie/show is in the library, or print its stored row.
    When NOT to use: You need live TMDB data (use movie_details / tv_details).
    Input: tmdb_id or title (at least one required).
    Output: Stored library rows, or 'not in library'.
    """
    store = get_store()
    if tmdb_id.strip():
        row = store.get_by_tmdb_id(int(tmdb_id))
        if not row:
            return f"Not in film_db (TMDB {tmdb_id}). Use film_add after a TMDB lookup."
        return _format_row(row) + (f"\n{row.get('overview') or ''}".rstrip())
    if title.strip():
        rows = store.get_by_title(title)
        if not rows:
            return f"No library match for '{title}'."
        return "\n".join(_format_row(r) for r in rows)
    return "Pass tmdb_id or title."


@tool
def film_search(query: str, media_type: str = "") -> str:
    """Search the local film_db by title/genre/overview, plus semantic neighbors.

    When to use: Find titles already in the library by name or genre.
    When NOT to use: Counting the library ('how many films') — use film_stats once.
        Do not call this repeatedly with letters, '*' or genre lists to tally titles.
        Live TMDB catalog: movie_search / tv_search / multi_search.
    Input: query - free text (required, not empty). media_type - optional movie or tv.
    Output: Matching library rows (capped, not a full dump).
    """
    if not (query or "").strip() or query.strip() in ("*", "%"):
        return "film_search needs a real query. For library size use film_stats (do not probe with '*' or letters)."
    kind = media_type.lower().strip()
    if kind not in ("movie", "tv", ""):
        kind = ""
    sql_hits = get_store().search(query, media_type=kind)
    vec_hits = search_neighbors(query, limit=10)
    lines = []
    seen: set[int] = set()
    for row in sql_hits:
        tid = int(row.get("tmdb_id") or 0)
        if tid:
            seen.add(tid)
        lines.append(_format_row(row))
    for row in vec_hits:
        tid = int(row.get("tmdb_id") or 0)
        if not tid or tid in seen:
            continue
        if kind and row.get("type") != kind:
            continue
        seen.add(tid)
        lines.append(_format_row(row))
    if not lines:
        return f"No film_db matches for '{query}'. Search TMDB with movie_search/tv_search, then film_add."
    return "Library matches:\n" + "\n".join(lines[:20])


@tool
def film_recommend(seed_title: str = "", tmdb_id: str = "", media_type: str = "movie") -> str:
    """Recommend movies or shows like a seed title using TMDB similar/recs plus film_db vectors.

    When to use: 'Recommend a sci-fi like Alien', 'something like The Expanse'.
        Resolves the seed via TMDB if only a title is given.
    When NOT to use: Looking up one specific title (use movie_search / film_get).
    Input: seed_title and/or tmdb_id. media_type movie or tv (default movie).
    Output: Ranked list with TMDB ids and a short why (TMDB similar vs semantic).
    """
    kind = "tv" if media_type.lower() in ("tv", "show", "series") else "movie"
    seed_id = int(tmdb_id) if str(tmdb_id).strip().isdigit() else None
    seed_label = seed_title.strip() or (f"TMDB {seed_id}" if seed_id else "")
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
        if date[:4]:
            seed_label = f"{seed_label} ({date[:4]})"
    if seed_id is None:
        return "Pass seed_title or tmdb_id."

    tmdb_items = similar_and_recommended(kind, seed_id)
    vec_query = seed_title.strip() or seed_label
    vector_items = search_neighbors(vec_query, limit=15)
    library = get_store().library_tmdb_ids()
    ranked = merge_candidates(tmdb_items, vector_items, library, limit=10, seed_id=seed_id)
    if not ranked:
        return f"No recommendations for {seed_label}. TMDB similar may be empty, or everything is already in film_db."
    lines = [f"Like {seed_label}:"]
    lines.extend(_format_row(item) for item in ranked)
    return "\n".join(lines)


@tool
def film_recommend_ai(seed_title: str = "", tmdb_id: str = "", media_type: str = "movie") -> str:
    """AI recommendation loop: similar mood/genre, different director, skip in-library titles.

    When to use: User wants recs that are NOT TMDB's usual same-director/franchise list.
        'recommend like Alien but not Ridley Scott', 'AI picks like The Expanse'.
        Call once per seed. seed_title is enough.
    When NOT to use: Quick TMDB similar list (use film_recommend). Counting the library (film_stats).
        Do not movie_search first. Do not call again with tmdb_id after a list returns.
    Input: seed_title is enough. tmdb_id optional if you already have it.
        media_type movie or tv (default movie).
    Output: 5-8 titles with year, TMDB id, director, one-line why.
    """
    from agentforge.film.ai_recommend import run_ai_recommend

    try:
        return run_ai_recommend(seed_title=seed_title, tmdb_id=tmdb_id, media_type=media_type)
    except Exception as exc:
        return f"AI recommend failed: {exc}"


@tool
def film_simkl_connect() -> str:
    """Start Simkl OAuth (V2). Returns a URL the user must open in a browser.

    When to use: First-time Simkl connect, or after token expiry when import says not connected.
    When NOT to use: Importing after already connected (use film_simkl_import).
    """
    try:
        from agentforge.film.oauth import start_login

        data = start_login()
    except Exception as exc:
        return f"Simkl connect failed: {exc}"
    return (
        "Open this URL, approve AgentForge, then run film_simkl_import:\n"
        f"{data['authorize_url']}\n"
        f"(redirect_uri must match the Simkl app exactly: {data['redirect_uri']})"
    )


@tool
def film_simkl_import(username: str = "") -> str:
    """Import the Simkl library into film_db (skip titles already imported).

    Uses authenticated /sync/all-items when SIMKL_ACCESS_TOKEN is set (same as
    the playground importer). Falls back to a public profile watchlist if only
    a username is given — that list is often empty when the profile is private.

    When to use: 'Import all current Simkl items', first-time library fill.
    When NOT to use: Adding one title (use film_add).
    Input: username - optional public profile. Empty uses config / env.
    Output: Counts of fetched, imported, skipped, failed.
    """
    try:
        stats = import_watchlist(username)
    except Exception as exc:
        return f"Simkl import failed: {exc}"
    lines = [
        f"Simkl @{stats['username']} ({stats.get('source', '?')}): fetched {stats['fetched']}, "
        f"imported {stats['imported']}, skipped {stats['skipped']}, "
        f"qdrant backfill {stats.get('qdrant_filled', 0)}, failed {stats['failed']}."
    ]
    for err in stats.get("errors") or []:
        lines.append(f"  - {err}")
    return "\n".join(lines)


def _resolve_list(spec: str) -> dict | str:
    store = get_store()
    try:
        lst = store.resolve_list(spec)
    except ValueError as exc:
        return str(exc)
    if not lst:
        return f"No list matching '{spec}'. Use film_lists to see names and ids."
    return lst


def _media_for_list(tmdb_id: str, title: str) -> dict | str:
    store = get_store()
    if tmdb_id.strip():
        row = store.get_by_tmdb_id(int(tmdb_id))
        if not row:
            return f"TMDB {tmdb_id} is not in film_db. film_add it first."
        return row
    if title.strip():
        rows = store.get_by_title(title)
        if not rows:
            return f"'{title}' is not in film_db. film_add it first."
        if len(rows) > 1:
            bits = ", ".join(f"{r['title']} ({r.get('year')}) TMDB {r.get('tmdb_id')}" for r in rows[:8])
            return f"Multiple library matches for '{title}': {bits}. Pass tmdb_id."
        return rows[0]
    return "Pass tmdb_id or title."


@tool
def film_lists() -> str:
    """List all custom film lists with item counts.

    When to use: 'what lists do I have', 'show my lists'.
    When NOT to use: Library size (film_stats) or searching titles (film_search).
    """
    rows = get_store().list_lists()
    if not rows:
        return "No lists yet. Create one with film_list_create."
    return "\n".join(f"- {r['name']} (id {r['id']}, {r['item_count']} titles)" for r in rows)


@tool
def film_list_create(name: str, description: str = "") -> str:
    """Create a named list (watch later, weekend, sci-fi picks, …).

    When to use: 'create a list called Favourites'.
    Input: name - required. description - optional.
    """
    try:
        lst = get_store().create_list(name, description)
    except ValueError as exc:
        return str(exc)
    return f"Created list '{lst['name']}' (id {lst['id']})."


@tool
def film_list_show(list: str) -> str:
    """Show titles on a list (by name or numeric id).

    When to use: 'what's on Favourites', 'show list 3'.
    Input: list - name or id from film_lists.
    """
    lst = _resolve_list(list)
    if isinstance(lst, str):
        return lst
    items = get_store().list_items(int(lst["id"]))
    if not items:
        return f"List '{lst['name']}' (id {lst['id']}) is empty."
    lines = [f"{lst['name']} ({len(items)}):"]
    lines.extend(_format_row(r) for r in items)
    return "\n".join(lines)


@tool
def film_list_add(list: str, tmdb_id: str = "", title: str = "") -> str:
    """Add a library title to a list.

    When to use: 'add Alien to Favourites'. Title must already be in film_db (film_add first).
    Input: list - name or id. tmdb_id or title - one required.
    """
    lst = _resolve_list(list)
    if isinstance(lst, str):
        return lst
    media = _media_for_list(tmdb_id, title)
    if isinstance(media, str):
        return media
    added = get_store().add_to_list(int(lst["id"]), int(media["id"]))
    verb = "Added" if added else "Already on"
    return f"{verb} {media.get('title')} → {lst['name']}."


@tool
def film_list_remove(list: str, tmdb_id: str = "", title: str = "") -> str:
    """Remove a title from a list (does not delete it from film_db).

    Input: list - name or id. tmdb_id or title - one required.
    """
    lst = _resolve_list(list)
    if isinstance(lst, str):
        return lst
    media = _media_for_list(tmdb_id, title)
    if isinstance(media, str):
        return media
    removed = get_store().remove_from_list(int(lst["id"]), int(media["id"]))
    if not removed:
        return f"{media.get('title')} was not on {lst['name']}."
    return f"Removed {media.get('title')} from {lst['name']}."


@tool
def film_list_clear(list: str) -> str:
    """Remove every title from a list. The list itself stays.

    When to use: 'empty Ingrids Filme', 'truncate Weekend sci-fi', 'clear the list'.
    When NOT to use: Deleting the list (film_list_delete). Do not use shell or curl.
    Input: list - name or numeric id.
    """
    lst = _resolve_list(list)
    if isinstance(lst, str):
        return lst
    n = get_store().clear_list(int(lst["id"]))
    return f"Emptied '{lst['name']}' ({n} titles removed). The list still exists."


@tool
def film_list_delete(list: str) -> str:
    """Delete a list and its memberships. Titles stay in film_db.

    When to use: 'delete the Favourites list'.
    """
    lst = _resolve_list(list)
    if isinstance(lst, str):
        return lst
    get_store().delete_list(int(lst["id"]))
    return f"Deleted list '{lst['name']}'."


def register_film_tools(registry: ToolRegistry) -> int:
    registry.register_category_hint(
        "Film",
        "Local film_db library (movies and TV). Add titles from TMDB, search the "
        "library, and recommend similar titles. Use TMDB tools to resolve ids first. "
        "Tools: film_stats, film_set_status, film_add, film_add_season, film_get_season, film_get, film_search, "
        "film_recommend, film_recommend_ai, "
        "film_lists, film_list_create, film_list_show, film_list_add, film_list_remove, film_list_clear, film_list_delete, "
        "film_simkl_connect, film_simkl_import.",
    )
    tools = [
        film_stats,
        film_set_status,
        film_add,
        film_add_season,
        film_get_season,
        film_get,
        film_search,
        film_recommend,
        film_recommend_ai,
        film_lists,
        film_list_create,
        film_list_show,
        film_list_add,
        film_list_remove,
        film_list_clear,
        film_list_delete,
        film_simkl_connect,
        film_simkl_import,
    ]
    for func in tools:
        registry.register(func, category="Film")
    return len(tools)
