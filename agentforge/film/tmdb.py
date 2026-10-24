"""TMDB payloads for the local library (wraps the shared HTTP helper)."""

from __future__ import annotations

from typing import Any

from agentforge.tools.tmdb import _tmdb_request


def find_tmdb_id_by_imdb(imdb_id: str, media_type: str) -> int | None:
    data = _tmdb_request(f"/find/{imdb_id}", {"external_source": "imdb_id"})
    if media_type == "movie":
        hits = data.get("movie_results") or []
    else:
        hits = data.get("tv_results") or []
    if not hits:
        return None
    return int(hits[0]["id"]) if hits[0].get("id") else None


def fetch_media(media_type: str, tmdb_id: int) -> dict[str, Any]:
    kind = "tv" if media_type == "tv" else "movie"
    item = _tmdb_request(f"/{kind}/{tmdb_id}")
    credits = _tmdb_request(f"/{kind}/{tmdb_id}/credits")
    genres = ", ".join(g.get("name", "") for g in item.get("genres") or [] if g.get("name"))
    crew = credits.get("crew") or []
    director = next((c.get("name") for c in crew if c.get("job") == "Director"), None)
    if kind == "tv" and not director:
        director = ", ".join(c.get("name", "") for c in item.get("created_by") or [] if c.get("name"))
    cast = ", ".join(c.get("name", "") for c in (credits.get("cast") or [])[:10] if c.get("name"))
    title = item.get("title") or item.get("name") or ""
    date = item.get("release_date") or item.get("first_air_date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None
    return {
        "type": kind,
        "title": title,
        "year": year,
        "overview": item.get("overview") or "",
        "runtime": item.get("runtime") or ((item.get("episode_run_time") or [None])[0]),
        "poster_path": item.get("poster_path"),
        "vote_average": item.get("vote_average"),
        "vote_count": item.get("vote_count"),
        "tmdb_id": int(item["id"]),
        "imdb_id": item.get("imdb_id"),
        "genres": genres,
        "director": director or "",
        "cast": cast,
    }


def _blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def episodes_from_season_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Map TMDB /tv/{id}/season/{n} episodes onto film_db rows (source=tmdb)."""
    season_fallback = data.get("season_number")
    out: list[dict[str, Any]] = []
    for ep in data.get("episodes") or []:
        num = ep.get("episode_number")
        if num is None:
            continue
        season = ep.get("season_number")
        if season is None:
            season = season_fallback
        if season is None:
            continue
        out.append(
            {
                "season": int(season),
                "episode": int(num),
                "name": _blank(ep.get("name")),
                "overview": _blank(ep.get("overview")),
                "air_date": _blank(ep.get("air_date")),
                "runtime": ep.get("runtime"),
                "still_path": ep.get("still_path") or None,
                "vote_average": ep.get("vote_average"),
                "tmdb_episode_id": ep.get("id"),
            }
        )
    return out


def fetch_season(show_tmdb_id: int, season: int) -> dict[str, Any]:
    data = _tmdb_request(f"/tv/{int(show_tmdb_id)}/season/{int(season)}")
    episodes = episodes_from_season_payload(data)
    season_number = data.get("season_number")
    return {
        "show_tmdb_id": int(show_tmdb_id),
        "season": int(season_number) if season_number is not None else int(season),
        "name": _blank(data.get("name")),
        "overview": _blank(data.get("overview")),
        "air_date": _blank(data.get("air_date")),
        "episodes": episodes,
    }


def similar_and_recommended(media_type: str, tmdb_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    kind = "tv" if media_type == "tv" else "movie"
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for source, path in (
        ("recommendations", f"/{kind}/{tmdb_id}/recommendations"),
        ("similar", f"/{kind}/{tmdb_id}/similar"),
    ):
        try:
            data = _tmdb_request(path)
        except Exception:
            continue
        for item in data.get("results") or []:
            tid = int(item.get("id") or 0)
            if not tid or tid in seen:
                continue
            seen.add(tid)
            title = item.get("title") or item.get("name") or ""
            date = item.get("release_date") or item.get("first_air_date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            out.append(
                {
                    "tmdb_id": tid,
                    "title": title,
                    "year": year,
                    "type": kind,
                    "overview": item.get("overview") or "",
                    "vote_average": item.get("vote_average"),
                    "source": source,
                }
            )
            if len(out) >= limit:
                return out
    return out
