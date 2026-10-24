"""SQLite library for movies and TV shows."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from agentforge.config import get_config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
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
    simkl_id INTEGER UNIQUE,
    imdb_id TEXT,
    genres TEXT,
    director TEXT,
    cast TEXT,
    created_at INTEGER DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS user_media (
    media_id INTEGER PRIMARY KEY REFERENCES media(id),
    status TEXT NOT NULL,
    user_rating INTEGER
);

CREATE INDEX IF NOT EXISTS idx_media_type ON media(type);
CREATE INDEX IF NOT EXISTS idx_media_title ON media(title);

CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at INTEGER DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS list_items (
    list_id INTEGER REFERENCES lists(id) ON DELETE CASCADE,
    media_id INTEGER REFERENCES media(id),
    added_at INTEGER DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (list_id, media_id)
);

CREATE INDEX IF NOT EXISTS idx_list_items_list ON list_items(list_id);
CREATE INDEX IF NOT EXISTS idx_list_items_media ON list_items(media_id);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    show_tmdb_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    name TEXT,
    overview TEXT,
    air_date TEXT,
    runtime INTEGER,
    still_path TEXT,
    vote_average REAL,
    tmdb_episode_id INTEGER,
    source TEXT NOT NULL DEFAULT 'tmdb',
    UNIQUE(show_tmdb_id, season, episode)
);

CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_tmdb_id, season);
"""

_local = threading.local()


def db_path() -> Path:
    env = os.environ.get("AGENTFORGE_FILM_DB", "").strip()
    if env:
        return Path(env).expanduser()
    cfg = str(get_config().get("tools.film.db_path", "") or "").strip()
    if cfg:
        return Path(cfg).expanduser()
    return Path("data/film_db.sqlite")


def get_store() -> FilmStore:
    store = getattr(_local, "store", None)
    path = db_path()
    if store is None or store.path != path:
        store = FilmStore(path)
        _local.store = store
    return store


class FilmStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(media)").fetchall()}
        if "simkl_id" not in cols:
            self._conn.execute("ALTER TABLE media ADD COLUMN simkl_id INTEGER")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_media_simkl ON media(simkl_id)")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS lists (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS list_items (
                list_id INTEGER REFERENCES lists(id) ON DELETE CASCADE,
                media_id INTEGER REFERENCES media(id),
                added_at INTEGER DEFAULT (strftime('%s', 'now')),
                PRIMARY KEY (list_id, media_id)
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_list_items_list ON list_items(list_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_list_items_media ON list_items(media_id)")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY,
                show_tmdb_id INTEGER NOT NULL,
                season INTEGER NOT NULL,
                episode INTEGER NOT NULL,
                name TEXT,
                overview TEXT,
                air_date TEXT,
                runtime INTEGER,
                still_path TEXT,
                vote_average REAL,
                tmdb_episode_id INTEGER,
                source TEXT NOT NULL DEFAULT 'tmdb',
                UNIQUE(show_tmdb_id, season, episode)
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_tmdb_id, season)")

    def upsert_media(self, row: dict[str, Any], *, status: str = "plantowatch") -> dict[str, Any]:
        tmdb_raw = row.get("tmdb_id")
        tmdb_id = int(tmdb_raw) if tmdb_raw not in (None, "") else None
        simkl_raw = row.get("simkl_id")
        simkl_id = int(simkl_raw) if simkl_raw not in (None, "") else None
        existing = None
        if tmdb_id is not None:
            existing = self.get_by_tmdb_id(tmdb_id)
        if existing is None and simkl_id is not None:
            existing = self.get_by_simkl_id(simkl_id)
        fields = (
            row.get("type") or "movie",
            row.get("title") or "",
            row.get("year"),
            row.get("overview"),
            row.get("runtime"),
            row.get("poster_path"),
            row.get("vote_average"),
            row.get("vote_count"),
            tmdb_id,
            simkl_id,
            row.get("imdb_id"),
            row.get("genres"),
            row.get("director"),
            row.get("cast"),
        )
        if existing:
            self._conn.execute(
                """UPDATE media SET type=?, title=?, year=?, overview=?, runtime=?,
                   poster_path=?, vote_average=?, vote_count=?, tmdb_id=?, simkl_id=?,
                   imdb_id=?, genres=?, director=?, cast=?, updated_at=strftime('%s', 'now')
                   WHERE id=?""",
                (*fields, int(existing["id"])),
            )
            media_id = int(existing["id"])
        else:
            cur = self._conn.execute(
                """INSERT INTO media
                   (type, title, year, overview, runtime, poster_path, vote_average,
                    vote_count, tmdb_id, simkl_id, imdb_id, genres, director, cast)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                fields,
            )
            if cur.lastrowid is None:
                raise RuntimeError("media INSERT returned no rowid")
            media_id = int(cur.lastrowid)
        rating = row.get("user_rating")
        self._conn.execute(
            """INSERT INTO user_media (media_id, status, user_rating)
               VALUES (?, ?, ?)
               ON CONFLICT(media_id) DO UPDATE SET status=excluded.status,
                 user_rating=COALESCE(excluded.user_rating, user_media.user_rating)""",
            (media_id, status, rating),
        )
        self._conn.commit()
        if tmdb_id is not None:
            return self.get_by_tmdb_id(tmdb_id) or {}
        if simkl_id is not None:
            return self.get_by_simkl_id(simkl_id) or {}
        # Neither id given — nothing to look the row back up by.
        return {}

    def get_by_simkl_id(self, simkl_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT m.*, u.status, u.user_rating
               FROM media m LEFT JOIN user_media u ON u.media_id = m.id
               WHERE m.simkl_id = ?""",
            (int(simkl_id),),
        ).fetchone()
        return dict(row) if row else None

    def has_simkl_id(self, simkl_id: int) -> bool:
        row = self._conn.execute("SELECT 1 FROM media WHERE simkl_id = ?", (int(simkl_id),)).fetchone()
        return row is not None

    def get_by_tmdb_id(self, tmdb_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT m.*, u.status, u.user_rating
               FROM media m LEFT JOIN user_media u ON u.media_id = m.id
               WHERE m.tmdb_id = ?""",
            (int(tmdb_id),),
        ).fetchone()
        return dict(row) if row else None

    def get_by_title(self, title: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT m.*, u.status, u.user_rating
               FROM media m LEFT JOIN user_media u ON u.media_id = m.id
               WHERE m.title LIKE ? COLLATE NOCASE
               ORDER BY m.year DESC
               LIMIT 20""",
            (f"%{title.strip()}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, *, media_type: str = "") -> list[dict[str, Any]]:
        q = f"%{query.strip()}%"
        if media_type in ("movie", "tv"):
            rows = self._conn.execute(
                """SELECT m.*, u.status, u.user_rating
                   FROM media m LEFT JOIN user_media u ON u.media_id = m.id
                   WHERE m.type = ? AND (m.title LIKE ? COLLATE NOCASE
                        OR ifnull(m.genres,'') LIKE ? COLLATE NOCASE
                        OR ifnull(m.overview,'') LIKE ? COLLATE NOCASE)
                   ORDER BY m.vote_average DESC
                   LIMIT 20""",
                (media_type, q, q, q),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT m.*, u.status, u.user_rating
                   FROM media m LEFT JOIN user_media u ON u.media_id = m.id
                   WHERE m.title LIKE ? COLLATE NOCASE
                        OR ifnull(m.genres,'') LIKE ? COLLATE NOCASE
                        OR ifnull(m.overview,'') LIKE ? COLLATE NOCASE
                   ORDER BY m.vote_average DESC
                   LIMIT 20""",
                (q, q, q),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_status_all(self, status: str) -> int:
        self._conn.execute(
            """INSERT INTO user_media (media_id, status, user_rating)
               SELECT id, ?, NULL FROM media
               WHERE id NOT IN (SELECT media_id FROM user_media)""",
            (status,),
        )
        cur = self._conn.execute("UPDATE user_media SET status = ?", (status,))
        self._conn.commit()
        return int(cur.rowcount)

    def set_status_for_list(self, list_id: int, status: str) -> int:
        cur = self._conn.execute(
            """UPDATE user_media SET status = ?
               WHERE media_id IN (SELECT media_id FROM list_items WHERE list_id = ?)""",
            (status, int(list_id)),
        )
        self._conn.execute(
            """INSERT INTO user_media (media_id, status, user_rating)
               SELECT li.media_id, ?, NULL
               FROM list_items li
               WHERE li.list_id = ?
                 AND li.media_id NOT IN (SELECT media_id FROM user_media)""",
            (status, int(list_id)),
        )
        self._conn.commit()
        return int(cur.rowcount)

    def set_status(self, media_id: int, status: str) -> bool:
        self._conn.execute(
            """INSERT INTO user_media (media_id, status, user_rating)
               VALUES (?, ?, NULL)
               ON CONFLICT(media_id) DO UPDATE SET status=excluded.status""",
            (int(media_id), status),
        )
        self._conn.commit()
        return True

    def stats(self) -> dict[str, Any]:
        total_row = self._conn.execute("SELECT COUNT(*) AS n FROM media").fetchone()
        total = int(total_row["n"] if total_row is not None else 0)
        by_type: dict[str, int] = {}
        for row in self._conn.execute("SELECT type AS k, COUNT(*) AS n FROM media GROUP BY type"):
            by_type[str(row["k"] or "unknown")] = int(row["n"])
        by_status: dict[str, int] = {}
        for row in self._conn.execute(
            """SELECT ifnull(u.status, 'unset') AS k, COUNT(*) AS n
               FROM media m LEFT JOIN user_media u ON u.media_id = m.id
               GROUP BY ifnull(u.status, 'unset')"""
        ):
            by_status[str(row["k"])] = int(row["n"])
        return {"total": total, "by_type": by_type, "by_status": by_status}

    def create_list(self, name: str, description: str = "") -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("list name is empty")
        cur = self._conn.execute(
            "INSERT INTO lists (name, description) VALUES (?, ?)",
            (name, description.strip() or None),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM lists WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else {}

    def resolve_list(self, spec: str) -> dict[str, Any] | None:
        spec = (spec or "").strip()
        if not spec:
            return None
        if spec.isdigit():
            row = self._conn.execute("SELECT * FROM lists WHERE id = ?", (int(spec),)).fetchone()
            return dict(row) if row else None
        rows = self._conn.execute(
            "SELECT * FROM lists WHERE name = ? COLLATE NOCASE",
            (spec,),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            raise ValueError(f"multiple lists named '{spec}' — pass the numeric id")
        return None

    def list_lists(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT l.*, COUNT(li.media_id) AS item_count
               FROM lists l
               LEFT JOIN list_items li ON li.list_id = l.id
               GROUP BY l.id
               ORDER BY l.name COLLATE NOCASE"""
        ).fetchall()
        return [dict(r) for r in rows]

    def list_items(self, list_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT m.*, u.status, u.user_rating
               FROM list_items li
               JOIN media m ON m.id = li.media_id
               LEFT JOIN user_media u ON u.media_id = m.id
               WHERE li.list_id = ?
               ORDER BY li.added_at""",
            (int(list_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_to_list(self, list_id: int, media_id: int) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO list_items (list_id, media_id) VALUES (?, ?)",
            (int(list_id), int(media_id)),
        )
        self._conn.execute(
            "UPDATE lists SET updated_at = strftime('%s', 'now') WHERE id = ?",
            (int(list_id),),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def remove_from_list(self, list_id: int, media_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM list_items WHERE list_id = ? AND media_id = ?",
            (int(list_id), int(media_id)),
        )
        self._conn.execute(
            "UPDATE lists SET updated_at = strftime('%s', 'now') WHERE id = ?",
            (int(list_id),),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def clear_list(self, list_id: int) -> int:
        cur = self._conn.execute("DELETE FROM list_items WHERE list_id = ?", (int(list_id),))
        self._conn.execute(
            "UPDATE lists SET updated_at = strftime('%s', 'now') WHERE id = ?",
            (int(list_id),),
        )
        self._conn.commit()
        return int(cur.rowcount)

    def delete_list(self, list_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM lists WHERE id = ?", (int(list_id),))
        self._conn.commit()
        return cur.rowcount > 0

    def upsert_episodes(self, show_tmdb_id: int, rows: list[dict[str, Any]]) -> int:
        n = 0
        for row in rows:
            self._conn.execute(
                """INSERT INTO episodes
                   (show_tmdb_id, season, episode, name, overview, air_date,
                    runtime, still_path, vote_average, tmdb_episode_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'tmdb')
                   ON CONFLICT(show_tmdb_id, season, episode) DO UPDATE SET
                     name=excluded.name,
                     overview=excluded.overview,
                     air_date=excluded.air_date,
                     runtime=excluded.runtime,
                     still_path=excluded.still_path,
                     vote_average=excluded.vote_average,
                     tmdb_episode_id=excluded.tmdb_episode_id,
                     source='tmdb'""",
                (
                    int(show_tmdb_id),
                    int(row["season"]),
                    int(row["episode"]),
                    row.get("name") or None,
                    row.get("overview") or None,
                    row.get("air_date") or None,
                    row.get("runtime"),
                    row.get("still_path"),
                    row.get("vote_average"),
                    row.get("tmdb_episode_id"),
                ),
            )
            n += 1
        self._conn.commit()
        return n

    def get_season(self, show_tmdb_id: int, season: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM episodes
               WHERE show_tmdb_id = ? AND season = ?
               ORDER BY episode""",
            (int(show_tmdb_id), int(season)),
        ).fetchall()
        return [dict(r) for r in rows]

    def library_tmdb_ids(self) -> set[int]:
        rows = self._conn.execute("SELECT tmdb_id FROM media WHERE tmdb_id IS NOT NULL").fetchall()
        return {int(r[0]) for r in rows if r[0] is not None}

    def close(self) -> None:
        self._conn.close()
