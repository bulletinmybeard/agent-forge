"""TMDB (The Movie Database) tools - movie, TV, and person lookups.

Provides tools for searching and retrieving details about movies, TV shows,
actors, directors, and trending content from TMDB's API v3.

Requirements:
  - A TMDB API key (get one at https://www.themoviedb.org/settings/api)
  - Set either:
      - ``tools.tmdb.api_key`` in config.yaml, OR
      - ``TMDB_API_KEY`` environment variable

The tools gracefully disable themselves when no API key is available -
they return an informative error instead of crashing.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.tmdb import register_tmdb_tools

    registry = ToolRegistry()
    register_tmdb_tools(registry)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TMDB_BASE_URL = "https://api.themoviedb.org/3"
_IMAGE_BASE_URL = "https://image.tmdb.org/t/p"
_REQUEST_TIMEOUT = 15  # seconds

# Simple rate limiter: max 40 requests per 10 seconds (TMDB limit)
_rate_window_start = 0.0
_rate_request_count = 0
_RATE_LIMIT_WINDOW = 10.0
_MAX_REQUESTS_PER_WINDOW = 40

# TMDB v3 requires the key in the query string (no header alternative), so it
# ends up in the request URL. Scrub it before anything is logged or returned.
_API_KEY_RE = re.compile(r"(api_key=)[^&\s]*")


def _redact_key_in_url(url: str) -> str:
    """Replace the TMDB ``api_key`` value in a URL/string with ``***``."""
    return _API_KEY_RE.sub(r"\1***", url)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_api_key() -> str:
    """Return the TMDB API key from the ``TMDB_API_KEY`` environment variable."""
    return os.environ.get("TMDB_API_KEY", "")


def _get_config_value(key: str, default):
    """TMDB tunables (rate limit, language, ...) use built-in defaults."""
    return default


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _enforce_rate_limit() -> None:
    """Block if we've exceeded TMDB's rate limit window."""
    global _rate_window_start, _rate_request_count

    now = time.monotonic()
    if now - _rate_window_start > _RATE_LIMIT_WINDOW:
        _rate_request_count = 0
        _rate_window_start = now

    if _rate_request_count >= _MAX_REQUESTS_PER_WINDOW:
        wait = _RATE_LIMIT_WINDOW - (now - _rate_window_start)
        if wait > 0:
            logger.debug("[tmdb] Rate limit - waiting %.1fs", wait)
            time.sleep(wait)
            _rate_request_count = 0
            _rate_window_start = time.monotonic()

    _rate_request_count += 1


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _tmdb_request(endpoint: str, params: dict[str, str] | None = None) -> dict:
    """Make a GET request to the TMDB API.

    Returns the parsed JSON response or raises an exception.
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "TMDB API key not configured. Set tools.tmdb.api_key in "
            "config.yaml or the TMDB_API_KEY environment variable."
        )

    _enforce_rate_limit()

    # Build URL with query params
    query_parts = [f"api_key={api_key}"]
    if params:
        for k, v in params.items():
            query_parts.append(f"{quote(k)}={quote(str(v))}")
    query_string = "&".join(query_parts)

    url = f"{_TMDB_BASE_URL}{endpoint}?{query_string}"

    req = Request(url, headers={"Accept": "application/json"}, method="GET")

    try:
        with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        # exc.url / exc.filename carry the key-bearing request URL - scrub the
        # body (TMDB sometimes echoes the request) before surfacing it.
        body = _redact_key_in_url(exc.read().decode()) if exc.fp else ""
        raise RuntimeError(f"TMDB API error (HTTP {exc.code}): {body}") from exc
    except URLError as exc:
        # URLError can carry the URL in .filename; scrub the stringified form.
        raise RuntimeError(f"TMDB API connection error: {_redact_key_in_url(str(exc))}") from exc


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _poster_url(path: str | None, size: str = "w342") -> str:
    if not path:
        return "N/A"
    return f"{_IMAGE_BASE_URL}/{size}{path}"


def _format_movie(m: dict, credits: dict | None = None) -> str:
    """Format a movie dict into a readable string."""
    genres = ", ".join(g["name"] for g in m.get("genres", []))
    lines = [
        f"Title: {m.get('title', 'Unknown')}",
        f"Year: {(m.get('release_date') or '')[:4] or 'N/A'}",
        f"TMDB ID: {m.get('id')}",
        f"Rating: {m.get('vote_average', 'N/A')}/10 ({m.get('vote_count', 0)} votes)",
        f"Runtime: {m.get('runtime', 'N/A')} min",
        f"Genres: {genres or 'N/A'}",
        f"Overview: {m.get('overview', 'N/A')}",
        f"Release Date: {m.get('release_date', 'N/A')}",
        f"Poster: {_poster_url(m.get('poster_path'))}",
    ]
    if credits:
        director = next(
            (c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"),
            "N/A",
        )
        max_cast = int(_get_config_value("max_cast", 10))
        cast = [c["name"] for c in credits.get("cast", [])[:max_cast]]
        lines.append(f"Director: {director}")
        lines.append(f"Cast: {', '.join(cast) if cast else 'N/A'}")
    return "\n".join(lines)


def _format_tv(t: dict, credits: dict | None = None) -> str:
    """Format a TV show dict into a readable string."""
    genres = ", ".join(g["name"] for g in t.get("genres", []))
    lines = [
        f"Title: {t.get('name', 'Unknown')}",
        f"Year: {(t.get('first_air_date') or '')[:4] or 'N/A'}",
        f"TMDB ID: {t.get('id')}",
        f"Rating: {t.get('vote_average', 'N/A')}/10 ({t.get('vote_count', 0)} votes)",
        f"Seasons: {t.get('number_of_seasons', 'N/A')}",
        f"Episodes: {t.get('number_of_episodes', 'N/A')}",
        f"Genres: {genres or 'N/A'}",
        f"Overview: {t.get('overview', 'N/A')}",
        f"First Air Date: {t.get('first_air_date', 'N/A')}",
        f"Status: {t.get('status', 'N/A')}",
        f"Poster: {_poster_url(t.get('poster_path'))}",
    ]
    if credits:
        creators = [c["name"] for c in t.get("created_by", [])]
        max_cast = int(_get_config_value("max_cast", 10))
        cast = [c["name"] for c in credits.get("cast", [])[:max_cast]]
        if creators:
            lines.append(f"Created By: {', '.join(creators)}")
        lines.append(f"Cast: {', '.join(cast) if cast else 'N/A'}")
    return "\n".join(lines)


def _format_person(p: dict) -> str:
    """Format a person dict into a readable string."""
    lines = [
        f"Name: {p.get('name', 'Unknown')}",
        f"TMDB ID: {p.get('id')}",
        f"Known For: {p.get('known_for_department', 'N/A')}",
        f"Birthday: {p.get('birthday', 'N/A')}",
        f"Place of Birth: {p.get('place_of_birth', 'N/A')}",
        f"Biography: {(p.get('biography') or 'N/A')[:500]}",
        f"Photo: {_poster_url(p.get('profile_path'), 'w185')}",
    ]
    if p.get("deathday"):
        lines.insert(5, f"Died: {p['deathday']}")
    return "\n".join(lines)


def _format_search_item(item: dict) -> str:
    """Format a search result item (movie, TV, or person)."""
    media_type = item.get("media_type", "unknown")
    title = item.get("title") or item.get("name") or "Unknown"
    year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
    rating = item.get("vote_average", "")
    rating_str = f" - {rating}/10" if rating else ""
    return f"[{media_type}] {title} ({year or 'N/A'}){rating_str} (ID: {item.get('id')})"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def movie_search(query: str, year: str = "") -> str:
    """Search for movies on TMDB by title and return a list of matches.

    When to use: Find a movie's TMDB ID when you know the title but not the ID.
        Use this first, then call movie_details with the ID for full information.
    When NOT to use: You already have the TMDB ID (use movie_details directly),
        searching across movies + TV + people (use multi_search).
    Input: query - movie title to search for.
        year - optional release year to narrow results.
    Output: Up to 10 matching movies with title, year, rating, and TMDB ID.
    """
    try:
        params: dict[str, str] = {
            "query": query,
            "include_adult": str(_get_config_value("include_adult", False)).lower(),
        }
        if year:
            params["year"] = year

        data = _tmdb_request("/search/movie", params)
        results = data.get("results", [])[:10]

        if not results:
            return f"No movies found for '{query}'" + (f" ({year})" if year else "")

        lines = [f"Found {data.get('total_results', len(results))} movies for '{query}':"]
        for m in results:
            yr = (m.get("release_date") or "")[:4]
            rating = m.get("vote_average", "")
            lines.append(f"  - {m.get('title', '?')} ({yr or 'N/A'}) - {rating}/10 - TMDB ID: {m['id']}")
        lines.append("\nUse movie_details(tmdb_id) for full information about a specific movie.")
        return "\n".join(lines)

    except Exception as exc:
        return f"Error searching movies: {exc}"


@tool
def movie_details(tmdb_id: str) -> str:
    """Get full details for a movie by its TMDB ID.

    When to use: Retrieve complete movie information - cast, director, runtime,
        genres, rating, overview - after finding the ID via movie_search.
    When NOT to use: You only know the title (use movie_search first),
        TV show details (use tv_details).
    Input: tmdb_id - TMDB movie ID as a string (e.g., '389' for The Wrong Man).
    Output: Title, year, rating, runtime, genres, overview, release date,
        director, and top cast members.
    """
    try:
        movie = _tmdb_request(f"/movie/{tmdb_id}")
        credits = _tmdb_request(f"/movie/{tmdb_id}/credits")
        return _format_movie(movie, credits)
    except Exception as exc:
        return f"Error getting movie details: {exc}"


@tool
def tv_search(query: str, year: str = "") -> str:
    """Search for TV shows on TMDB by title and return a list of matches.

    When to use: Find a TV show's TMDB ID when you know the title but not the ID.
        Use this first, then call tv_details with the ID for full information.
    When NOT to use: You already have the TMDB ID (use tv_details directly),
        searching across movies + TV + people (use multi_search).
    Input: query - TV show title to search for.
        year - optional first air year to narrow results.
    Output: Up to 10 matching shows with title, year, rating, and TMDB ID.
    """
    try:
        params: dict[str, str] = {
            "query": query,
            "include_adult": str(_get_config_value("include_adult", False)).lower(),
        }
        if year:
            params["first_air_date_year"] = year

        data = _tmdb_request("/search/tv", params)
        results = data.get("results", [])[:10]

        if not results:
            return f"No TV shows found for '{query}'" + (f" ({year})" if year else "")

        lines = [f"Found {data.get('total_results', len(results))} TV shows for '{query}':"]
        for t in results:
            yr = (t.get("first_air_date") or "")[:4]
            rating = t.get("vote_average", "")
            lines.append(f"  - {t.get('name', '?')} ({yr or 'N/A'}) - {rating}/10 - TMDB ID: {t['id']}")
        lines.append("\nUse tv_details(tmdb_id) for full information about a specific show.")
        return "\n".join(lines)

    except Exception as exc:
        return f"Error searching TV shows: {exc}"


@tool
def tv_details(tmdb_id: str) -> str:
    """Get full details for a TV show by its TMDB ID.

    When to use: Retrieve complete TV show information - cast, creators,
        seasons, episodes, genres, rating - after finding the ID via tv_search.
    When NOT to use: You only know the title (use tv_search first),
        movie details (use movie_details).
    Input: tmdb_id - TMDB TV show ID as a string (e.g., '1396' for Breaking Bad).
    Output: Title, year, rating, seasons/episodes count, genres, overview,
        first air date, status, creators, and top cast.
    """
    try:
        show = _tmdb_request(f"/tv/{tmdb_id}")
        credits = _tmdb_request(f"/tv/{tmdb_id}/credits")
        return _format_tv(show, credits)
    except Exception as exc:
        return f"Error getting TV details: {exc}"


@tool
def person_search(query: str) -> str:
    """Search for an actor, director, or crew member on TMDB by name.

    When to use: Find a person's TMDB ID when you know the name but not the ID.
        Use this first, then call person_details for biography and filmography.
    When NOT to use: You already have the TMDB ID (use person_details directly),
        searching across movies + TV + people (use multi_search).
    Input: query - person's name to search for (e.g., 'Alfred Hitchcock').
    Output: Up to 10 matching people with name, department, known-for credits,
        and TMDB ID.
    """
    try:
        data = _tmdb_request("/search/person", {"query": query})
        results = data.get("results", [])[:10]

        if not results:
            return f"No people found for '{query}'"

        lines = [f"Found {data.get('total_results', len(results))} people for '{query}':"]
        for p in results:
            dept = p.get("known_for_department", "")
            known_for = [(kf.get("title") or kf.get("name", "?")) for kf in p.get("known_for", [])[:3]]
            known_str = f" - Known for: {', '.join(known_for)}" if known_for else ""
            lines.append(f"  - {p.get('name', '?')} ({dept}){known_str} - TMDB ID: {p['id']}")
        lines.append("\nUse person_details(tmdb_id) for full biography and filmography.")
        return "\n".join(lines)

    except Exception as exc:
        return f"Error searching people: {exc}"


@tool
def person_details(tmdb_id: str) -> str:
    """Get full biography, filmography, and directing credits for a person by TMDB ID.

    When to use: Retrieve complete information about an actor or director -
        biography, birthday, birthplace, and their most notable acting and
        directing credits - after finding the ID via person_search.
    When NOT to use: You only know the name (use person_search first),
        movie details (use movie_details), TV details (use tv_details).
    Input: tmdb_id - TMDB person ID as a string (e.g., '2636' for Alfred Hitchcock).
    Output: Name, birthday, birthplace, biography excerpt, top acting roles
        sorted by popularity, and directing credits.
    """
    try:
        person = _tmdb_request(f"/person/{tmdb_id}")
        credits = _tmdb_request(f"/person/{tmdb_id}/combined_credits")

        result = _format_person(person)

        # Add filmography highlights
        cast_credits = credits.get("cast", [])
        crew_credits = credits.get("crew", [])

        if cast_credits:
            # Sort by popularity/vote_count
            cast_credits.sort(key=lambda x: x.get("vote_count", 0), reverse=True)
            top = cast_credits[:15]
            result += "\n\nNotable Acting Roles:"
            for c in top:
                title = c.get("title") or c.get("name") or "?"
                yr = (c.get("release_date") or c.get("first_air_date") or "")[:4]
                char = c.get("character", "")
                media = c.get("media_type", "")
                char_str = f" as {char}" if char else ""
                result += f"\n  - {title} ({yr or 'N/A'}) [{media}]{char_str}"

        if crew_credits:
            # Group by department, show directing credits prominently
            directing = [c for c in crew_credits if c.get("job") == "Director"]
            if directing:
                directing.sort(key=lambda x: x.get("vote_count", 0), reverse=True)
                result += "\n\nDirected:"
                for c in directing[:20]:
                    title = c.get("title") or c.get("name") or "?"
                    yr = (c.get("release_date") or c.get("first_air_date") or "")[:4]
                    result += f"\n  - {title} ({yr or 'N/A'})"

        return result

    except Exception as exc:
        return f"Error getting person details: {exc}"


@tool
def trending_media(media_type: str = "all", time_window: str = "week") -> str:
    """Get trending movies, TV shows, or people from TMDB for today or this week.

    When to use: Find out what is currently popular on TMDB without a specific
        title in mind - e.g., 'what movies are trending this week?'
    When NOT to use: Looking up a specific title (use movie_search or tv_search),
        getting full details for an item (use movie_details, tv_details, person_details).
    Input: media_type - 'movie', 'tv', 'person', or 'all' (default: 'all').
        time_window - 'day' or 'week' (default: 'week').
    Output: Up to 15 trending items with title, year, type, rating, and TMDB ID.
    """
    try:
        if media_type not in ("movie", "tv", "person", "all"):
            media_type = "all"
        if time_window not in ("day", "week"):
            time_window = "week"

        data = _tmdb_request(f"/trending/{media_type}/{time_window}")
        results = data.get("results", [])[:15]

        if not results:
            return f"No trending {media_type} found."

        type_label = media_type if media_type != "all" else "media"
        lines = [f"Trending {type_label} this {time_window}:"]
        for i, item in enumerate(results, 1):
            lines.append(f"  {i}. {_format_search_item(item)}")
        return "\n".join(lines)

    except Exception as exc:
        return f"Error getting trending: {exc}"


@tool
def multi_search(query: str) -> str:
    """Search TMDB across movies, TV shows, and people in a single query.

    When to use: When you do not know whether the user is asking about a movie,
        a TV show, or a person - let TMDB rank the most relevant results across
        all three types at once.
    When NOT to use: You know the type - use movie_search, tv_search, or
        person_search for more targeted results.
    Input: query - search term (e.g., 'Hitchcock', 'Oppenheimer').
    Output: Up to 15 results labelled by type (movie/tv/person) with title,
        year, rating, and TMDB ID. Follow up with the appropriate *_details tool.
    """
    try:
        data = _tmdb_request(
            "/search/multi",
            {
                "query": query,
                "include_adult": str(_get_config_value("include_adult", False)).lower(),
            },
        )
        results = data.get("results", [])[:15]

        if not results:
            return f"No results found for '{query}'"

        lines = [f"Found {data.get('total_results', len(results))} results for '{query}':"]
        for item in results:
            lines.append(f"  - {_format_search_item(item)}")
        lines.append("\nUse movie_details, tv_details, or person_details with the TMDB ID for full information.")
        return "\n".join(lines)

    except Exception as exc:
        return f"Error in multi-search: {exc}"


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

_availability_checked = False
_available = False


def is_tmdb_available() -> bool:
    """Return True if TMDB API is configured and reachable.

    Caches the result after the first probe.
    """
    global _availability_checked, _available

    if _availability_checked:
        return _available

    api_key = _get_api_key()
    if not api_key:
        logger.info("[tmdb] No API key found - TMDB tools disabled")
        _availability_checked = True
        _available = False
        return False

    # Quick probe: hit /configuration (lightweight endpoint)
    try:
        _tmdb_request("/configuration")
        logger.info("[tmdb] TMDB API available")
        _availability_checked = True
        _available = True
        return True
    except Exception as exc:
        logger.warning("[tmdb] Probe failed (%s) - TMDB tools disabled", exc)
        _availability_checked = True
        _available = False
        return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_tmdb_tools(registry: ToolRegistry) -> int:
    """Register TMDB tools with the given *registry*.

    Tools are always registered so the model knows they exist, but they
    return a helpful error if no API key is configured.
    """
    registry.register_category_hint(
        "TMDB",
        "Search and retrieve movie, TV show, and person information from "
        "The Movie Database (TMDB). Use these tools for any entertainment "
        "media lookups - they provide structured, reliable data. "
        "Available tools: movie_search, movie_details, tv_search, tv_details, "
        "person_search, person_details, trending_media, multi_search.",
    )

    tools = [
        movie_search,
        movie_details,
        tv_search,
        tv_details,
        person_search,
        person_details,
        trending_media,
        multi_search,
    ]
    for func in tools:
        registry.register(func, category="TMDB")
        logger.debug("Registered TMDB tool: %s", func.__name__)
    return len(tools)
