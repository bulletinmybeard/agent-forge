"""TMDB season/episode/watch-provider formatters (no live HTTP)."""

from agentforge.tools.registry import ToolRegistry
from agentforge.tools.tmdb import (
    _format_episode,
    _format_season,
    _format_tv,
    _format_watch_providers,
    register_tmdb_tools,
)


def test_register_includes_season_episode_providers():
    registry = ToolRegistry()
    n = register_tmdb_tools(registry)
    names = set(registry.list_tools())
    assert n == 11
    assert {"tv_season", "tv_episode", "watch_providers"} <= names


def test_format_season_untitled():
    text = _format_season(
        {
            "season_number": 1,
            "name": "Season 1",
            "air_date": "2015-12-27",
            "episodes": [
                {
                    "episode_number": 1,
                    "season_number": 1,
                    "name": "",
                    "overview": "",
                    "air_date": "2015-12-27",
                },
                {
                    "episode_number": 2,
                    "season_number": 1,
                    "name": "Named",
                    "overview": "Plot.",
                    "air_date": "2016-01-03",
                    "runtime": 50,
                    "vote_average": 8,
                },
            ],
        },
        "62755",
    )
    assert "(untitled)" in text
    assert "Named" in text
    assert "Plot." in text
    assert "show TMDB 62755" in text


def test_format_episode_empty_overview():
    text = _format_episode(
        {
            "episode_number": 1,
            "season_number": 2,
            "name": "",
            "overview": "",
            "air_date": "2016-01-01",
            "id": 99,
            "crew": [{"job": "Director", "name": "Baltasar Kormakur"}],
        },
        "62755",
    )
    assert "(untitled)" in text
    assert "(empty on TMDB)" in text
    assert "Director: Baltasar Kormakur" in text


def test_format_watch_providers_missing_and_hit():
    missing = _format_watch_providers(
        {"results": {"US": {"flatrate": [{"provider_name": "Netflix"}]}}},
        "DE",
        "tv",
        "1396",
    )
    assert "JustWatch" in missing
    assert "No providers" in missing
    assert "US" in missing
    hit = _format_watch_providers(
        {
            "results": {
                "DE": {
                    "link": "https://www.themoviedb.org/tv/1/watch?locale=DE",
                    "flatrate": [{"provider_name": "Netflix"}],
                    "rent": [{"provider_name": "Amazon Video"}],
                }
            }
        },
        "de",
        "tv",
        "1",
    )
    assert "Stream: Netflix" in hit
    assert "Rent: Amazon Video" in hit


def test_format_tv_includes_season_list():
    text = _format_tv(
        {
            "name": "Trapped",
            "id": 62755,
            "first_air_date": "2015-12-27",
            "number_of_seasons": 2,
            "number_of_episodes": 20,
            "genres": [],
            "seasons": [
                {"season_number": 1, "name": "Season 1", "episode_count": 10},
                {"season_number": 2, "name": "Season 2", "episode_count": 10},
            ],
        }
    )
    assert "S01 Season 1 (10 ep)" in text
    assert "tv_season" in text
