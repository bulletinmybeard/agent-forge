"""Local film_db store and recommend merge (no live TMDB/Qdrant)."""

from __future__ import annotations

from pathlib import Path

from agentforge.film.import_simkl import import_watchlist
from agentforge.film.recommend import merge_candidates
from agentforge.film.simkl import extract_media, normalize_username
from agentforge.film.store import FilmStore
from agentforge.film.tmdb import episodes_from_season_payload
from agentforge.film.vectors import collection_name


def test_upsert_and_get(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_FILM_DB", str(tmp_path / "film.sqlite"))
    store = FilmStore(tmp_path / "film.sqlite")
    row = store.upsert_media(
        {
            "type": "movie",
            "title": "Alien",
            "year": 1979,
            "overview": "In space no one can hear you scream.",
            "tmdb_id": 348,
            "genres": "Science Fiction, Horror",
            "director": "Ridley Scott",
            "cast": "Sigourney Weaver",
        }
    )
    assert row["tmdb_id"] == 348
    assert row["status"] == "plantowatch"
    got = store.get_by_tmdb_id(348)
    assert got and got["title"] == "Alien"
    assert store.library_tmdb_ids() == {348}


def test_stats_counts(tmp_path: Path):
    store = FilmStore(tmp_path / "film.sqlite")
    store.upsert_media({"type": "movie", "title": "Alien", "tmdb_id": 348}, status="completed")
    store.upsert_media({"type": "tv", "title": "The Expanse", "tmdb_id": 63639}, status="watching")
    stats = store.stats()
    assert stats["total"] == 2
    assert stats["by_type"]["movie"] == 1
    assert stats["by_type"]["tv"] == 1
    assert stats["by_status"]["completed"] == 1
    assert stats["by_status"]["watching"] == 1


def test_set_status_all(tmp_path: Path):
    store = FilmStore(tmp_path / "film.sqlite")
    store.upsert_media({"type": "movie", "title": "Alien", "tmdb_id": 348}, status="plantowatch")
    store.upsert_media({"type": "tv", "title": "The Expanse", "tmdb_id": 63639}, status="watching")
    n = store.set_status_all("completed")
    assert n == 2
    assert store.stats()["by_status"] == {"completed": 2}


def test_set_status_for_list(tmp_path: Path):
    store = FilmStore(tmp_path / "film.sqlite")
    a = store.upsert_media({"type": "movie", "title": "Alien", "tmdb_id": 348}, status="plantowatch")
    b = store.upsert_media({"type": "movie", "title": "Aliens", "tmdb_id": 679}, status="plantowatch")
    store.upsert_media({"type": "movie", "title": "Arrival", "tmdb_id": 329865}, status="watching")
    lst = store.create_list("Ingrids Filme")
    store.add_to_list(int(lst["id"]), int(a["id"]))
    store.add_to_list(int(lst["id"]), int(b["id"]))
    n = store.set_status_for_list(int(lst["id"]), "completed")
    assert n == 2
    for tmdb_id, expected in ((348, "completed"), (679, "completed"), (329865, "watching")):
        row = store.get_by_tmdb_id(tmdb_id)
        assert row and row["status"] == expected


def test_list_crud(tmp_path: Path):
    store = FilmStore(tmp_path / "film.sqlite")
    store.upsert_media({"type": "movie", "title": "Alien", "year": 1979, "tmdb_id": 348})
    lst = store.create_list("Favourites", "weekend")
    media = store.get_by_tmdb_id(348)
    assert media
    assert store.add_to_list(int(lst["id"]), int(media["id"]))
    assert not store.add_to_list(int(lst["id"]), int(media["id"]))
    found = store.resolve_list("favourites")
    assert found and found["id"] == lst["id"]
    items = store.list_items(int(lst["id"]))
    assert len(items) == 1
    assert items[0]["title"] == "Alien"
    assert store.list_lists()[0]["item_count"] == 1
    assert store.remove_from_list(int(lst["id"]), int(media["id"]))
    assert store.list_items(int(lst["id"])) == []
    assert store.clear_list(int(lst["id"])) == 0  # already empty after remove
    store.add_to_list(int(lst["id"]), int(media["id"]))
    assert store.clear_list(int(lst["id"])) == 1
    assert store.list_items(int(lst["id"])) == []
    assert store.delete_list(int(lst["id"]))
    assert store.list_lists() == []


def test_upsert_episodes_nullable_tmdb_source(tmp_path: Path):
    store = FilmStore(tmp_path / "film.sqlite")
    n = store.upsert_episodes(
        62755,
        [
            {
                "season": 1,
                "episode": 1,
                "name": None,
                "overview": "",
                "air_date": "2015-12-27",
                "runtime": 50,
                "still_path": None,
                "vote_average": 7.2,
                "tmdb_episode_id": 111,
            },
            {
                "season": 1,
                "episode": 2,
                "name": "Some title",
                "overview": "A plot.",
                "air_date": "2016-01-03",
                "runtime": 51,
                "still_path": "/x.jpg",
                "vote_average": 7.4,
                "tmdb_episode_id": 112,
            },
        ],
    )
    assert n == 2
    rows = store.get_season(62755, 1)
    assert len(rows) == 2
    assert rows[0]["name"] is None
    assert rows[0]["overview"] is None
    assert rows[0]["source"] == "tmdb"
    assert rows[0]["air_date"] == "2015-12-27"
    assert rows[1]["name"] == "Some title"
    store.upsert_episodes(
        62755,
        [
            {
                "season": 1,
                "episode": 1,
                "name": "Now named",
                "overview": None,
                "air_date": "2015-12-27",
                "runtime": 50,
                "still_path": None,
                "vote_average": 7.2,
                "tmdb_episode_id": 111,
            }
        ],
    )
    again = store.get_season(62755, 1)
    assert again[0]["name"] == "Now named"
    assert again[0]["source"] == "tmdb"
    assert again[1]["name"] == "Some title"


def test_film_add_and_get_season_tools(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_FILM_DB", str(tmp_path / "film.sqlite"))
    from agentforge.tools import film as film_mod

    monkeypatch.setattr(
        film_mod,
        "fetch_season",
        lambda tid, season: {
            "show_tmdb_id": tid,
            "season": season,
            "name": "Season 1",
            "episodes": [
                {"season": 1, "episode": 1, "name": None, "overview": None, "air_date": "2015-12-27"},
            ],
        },
    )
    out = film_mod.film_add_season("1", tmdb_id="62755")
    assert "Stored 1" in out
    assert "source=tmdb" in out
    assert "1 titles" in out
    got = film_mod.film_get_season("1", tmdb_id="62755")
    assert "(untitled)" in got
    assert "S01E01" in got


def test_episodes_from_season_payload_blanks():
    rows = episodes_from_season_payload(
        {
            "season_number": 1,
            "episodes": [
                {
                    "episode_number": 1,
                    "season_number": 1,
                    "id": 10,
                    "name": "  ",
                    "overview": "",
                    "air_date": "2015-12-27",
                    "runtime": 50,
                    "still_path": None,
                    "vote_average": 7.1,
                },
                {
                    "episode_number": 2,
                    "id": 11,
                    "name": "Winter",
                    "overview": "Snow.",
                    "air_date": "2016-01-03",
                    "runtime": None,
                    "still_path": "/s.jpg",
                    "vote_average": 8,
                },
            ],
        }
    )
    assert rows[0]["name"] is None
    assert rows[0]["overview"] is None
    assert rows[0]["tmdb_episode_id"] == 10
    assert rows[1]["season"] == 1
    assert rows[1]["name"] == "Winter"


def test_search_title(tmp_path: Path):
    store = FilmStore(tmp_path / "film.sqlite")
    store.upsert_media({"type": "movie", "title": "Arrival", "year": 2016, "tmdb_id": 329865, "genres": "Sci-Fi"})
    hits = store.search("arriv")
    assert hits and hits[0]["tmdb_id"] == 329865


def test_ai_recommend_inner_tools_exclude_shell_and_self():
    from agentforge.film.ai_recommend import _INNER_TOOLS

    assert "shell" not in _INNER_TOOLS
    assert "film_recommend_ai" not in _INNER_TOOLS
    assert "movie_details" in _INNER_TOOLS
    assert "film_get" in _INNER_TOOLS


def test_ai_recommend_inner_prompt_seed_settled():
    from agentforge.film.ai_recommend import _SYSTEM

    assert "Do not look up the seed" in _SYSTEM
    assert "no director yet" in _SYSTEM


def _stub_ai_recommend(monkeypatch, *, library: set[int]):
    from agentforge.film import ai_recommend as mod

    mod.clear_ai_recommend_cache()
    runs: list[str] = []

    class _Store:
        def library_tmdb_ids(self):
            return set(library)

    class _Reg:
        def register(self, *a, **k):
            return None

    class _Agent:
        def __init__(self, *a, **k):
            pass

        def run(self, user):
            runs.append(user)
            return object()

    monkeypatch.setattr(mod, "get_store", lambda: _Store())
    monkeypatch.setattr(
        mod,
        "fetch_media",
        lambda kind, tid: {"title": "Alien", "year": 1979, "director": "Ridley Scott", "tmdb_id": tid},
    )
    monkeypatch.setattr(mod, "similar_and_recommended", lambda *a, **k: [])
    monkeypatch.setattr(mod, "search_neighbors", lambda *a, **k: [])
    monkeypatch.setattr(mod, "ToolRegistry", lambda: _Reg())
    monkeypatch.setattr(mod, "register_tmdb_tools", lambda r: 0)
    monkeypatch.setattr(mod, "AIClient", lambda **k: object())
    monkeypatch.setattr(mod, "AgentLoop", _Agent)
    monkeypatch.setattr(
        mod,
        "extract_agent_loop_output",
        lambda ctx: ("1. Pandorum (2009) — TMDB 19898 — Christian Alvart — claustrophobia", [], 0),
    )
    return runs, mod


def test_ai_recommend_cache_same_seed(monkeypatch):
    runs, mod = _stub_ai_recommend(monkeypatch, library={348, 679})
    a = mod.run_ai_recommend(seed_title="Alien", tmdb_id="348")
    b = mod.run_ai_recommend(seed_title="Alien", tmdb_id="348")
    assert a == b
    assert "Pandorum" in a
    assert len(runs) == 1


def test_ai_recommend_cache_misses_when_library_changes(monkeypatch):
    runs, mod = _stub_ai_recommend(monkeypatch, library={348})
    store_lib = {348}

    class _Store:
        def library_tmdb_ids(self):
            return set(store_lib)

    monkeypatch.setattr(mod, "get_store", lambda: _Store())
    mod.run_ai_recommend(seed_title="Alien", tmdb_id="348")
    store_lib.add(679)
    mod.run_ai_recommend(seed_title="Alien", tmdb_id="348")
    assert len(runs) == 2


def test_ai_recommend_passes_full_library_ids(monkeypatch):
    library = set(range(1, 91))
    runs, mod = _stub_ai_recommend(monkeypatch, library=library)
    mod.run_ai_recommend(seed_title="Alien", tmdb_id="348")
    assert "In-library TMDB ids (90):" in runs[0]
    assert "89, 90" in runs[0]


def test_merge_drops_library_and_seed():
    tmdb = [
        {"tmdb_id": 11, "title": "Seed", "source": "similar"},
        {"tmdb_id": 22, "title": "Keep", "source": "recommendations"},
        {"tmdb_id": 33, "title": "Owned", "source": "similar"},
    ]
    vectors = [
        {"tmdb_id": 44, "title": "Semantic", "score": 0.91},
        {"tmdb_id": 22, "title": "Keep dup", "score": 0.5},
    ]
    ranked = merge_candidates(tmdb, vectors, library_ids={33}, limit=10, seed_id=11)
    ids = [r["tmdb_id"] for r in ranked]
    assert ids == [22, 44]
    assert ranked[0]["why"].startswith("TMDB")
    assert "semantic" in ranked[1]["why"]


def test_migrates_simkl_id_on_old_schema(tmp_path: Path):
    path = tmp_path / "old.sqlite"
    conn = __import__("sqlite3").connect(path)
    conn.executescript(
        """
        CREATE TABLE media (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER,
            overview TEXT,
            runtime INTEGER,
            poster_path TEXT,
            vote_average REAL,
            vote_count INTEGER,
            tmdb_id INTEGER UNIQUE,
            imdb_id TEXT,
            genres TEXT,
            director TEXT,
            cast TEXT
        );
        CREATE TABLE user_media (
            media_id INTEGER PRIMARY KEY REFERENCES media(id),
            status TEXT NOT NULL,
            user_rating INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    store = FilmStore(path)
    store.upsert_media({"type": "movie", "title": "Alien", "tmdb_id": 348, "simkl_id": 99})
    assert store.has_simkl_id(99)


def test_normalize_username_rejects_hex_key():
    try:
        normalize_username("0123456789abcdef0123456789abcdef")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "API key" in str(exc)


def test_simkl_connect_requires_secret(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENTFORGE_FILM_DB", str(tmp_path / "film.sqlite"))
    monkeypatch.setenv("SIMKL_CLIENT_ID", "abc")
    monkeypatch.delenv("SIMKL_CLIENT_SECRET", raising=False)

    class _Cfg:
        def get(self, key, default=None):
            return ""

    monkeypatch.setattr("agentforge.film.oauth.get_config", lambda: _Cfg())
    from agentforge.film.oauth import start_login

    try:
        start_login()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "SECRET" in str(exc)


def test_normalize_username_from_url():
    assert normalize_username("https://simkl.com/someone/watchlist") == "someone"


def test_extract_media_movie():
    item = {
        "status": "completed",
        "user_rating": 8,
        "movie": {
            "title": "Alien",
            "year": 1979,
            "ids": {"simkl": 1, "tmdb": "348", "imdb": "tt0078748"},
            "genres": ["Science Fiction"],
        },
    }
    out = extract_media(item)
    assert out and out["type"] == "movie"
    assert out["simkl_id"] == 1
    assert out["tmdb_id"] == 348
    assert out["status"] == "completed"


def test_flatten_sync_watchlist(monkeypatch):
    from agentforge.film.simkl import fetch_sync_watchlist

    payload = {
        "movies": [{"status": "completed", "movie": {"title": "Alien", "ids": {"simkl": 1}}}],
        "shows": [{"status": "watching", "show": {"title": "The Expanse", "ids": {"simkl": 2}}}],
        "anime": [],
    }

    def fake_request(path, *, params=None, token=""):
        assert token == "tok"
        if path.endswith("/movies"):
            return {"movies": payload["movies"]}
        if path.endswith("/shows"):
            return {"shows": payload["shows"]}
        return {"anime": []}

    monkeypatch.setattr("agentforge.film.simkl._request", fake_request)
    items = fetch_sync_watchlist("tok")
    assert len(items) == 2
    first, second = extract_media(items[0]), extract_media(items[1])
    assert first and first["title"] == "Alien"
    assert second and second["type"] == "tv"


def test_simkl_import_skips_existing(tmp_path: Path, monkeypatch):
    db = tmp_path / "film.sqlite"
    monkeypatch.setenv("AGENTFORGE_FILM_DB", str(db))
    store = FilmStore(db)
    store.upsert_media({"type": "movie", "title": "Alien", "tmdb_id": 348, "simkl_id": 1})

    monkeypatch.setattr(
        "agentforge.film.import_simkl.fetch_public_watchlist",
        lambda username: [
            {
                "status": "completed",
                "movie": {"title": "Alien", "year": 1979, "ids": {"simkl": 1, "tmdb": "348"}},
            },
            {
                "status": "plantowatch",
                "movie": {"title": "Arrival", "year": 2016, "ids": {"simkl": 2, "tmdb": "329865"}},
            },
        ],
    )
    monkeypatch.setattr(
        "agentforge.film.import_simkl.fetch_media",
        lambda kind, tid: {"type": kind, "title": "Arrival", "year": 2016, "tmdb_id": tid},
    )
    monkeypatch.setattr("agentforge.film.import_simkl.vector_upsert", lambda row: True)
    monkeypatch.setattr("agentforge.film.import_simkl.point_exists", lambda tid: False)
    monkeypatch.setattr("agentforge.film.import_simkl.access_token", lambda: "")
    stats = import_watchlist("someone")
    assert stats["imported"] == 1
    assert stats["skipped"] == 1
    assert stats["qdrant_filled"] == 1
    assert store.has_simkl_id(2)


def test_default_collection_is_media_fingerprints(monkeypatch):
    class _Cfg:
        def get(self, key, default=None):
            return ""

    monkeypatch.setattr("agentforge.film.vectors.get_config", lambda: _Cfg())
    assert collection_name() == "media_fingerprints"
