"""Import a public Simkl watchlist into film_db."""

from __future__ import annotations

import logging
from typing import Any

from agentforge.film.simkl import (
    access_token,
    default_username,
    extract_media,
    fetch_public_watchlist,
    fetch_sync_watchlist,
)
from agentforge.film.store import get_store
from agentforge.film.tmdb import fetch_media, find_tmdb_id_by_imdb
from agentforge.film.vectors import point_exists
from agentforge.film.vectors import upsert_media as vector_upsert

logger = logging.getLogger(__name__)


def import_watchlist(username: str = "") -> dict[str, Any]:
    source = "sync"
    user = (username or default_username()).strip()
    try:
        token = access_token()
    except RuntimeError:
        token = ""
    if token:
        items = fetch_sync_watchlist(token)
        user = user or "authenticated"
    elif user:
        items = fetch_public_watchlist(user)
        source = "public"
        if not items:
            raise RuntimeError(
                "Public Simkl watchlist was empty. Connect the V2 app first: "
                "open /api/film/simkl/connect, approve, then retry import."
            )
    else:
        raise RuntimeError(
            "Simkl is not connected. Open /api/film/simkl/connect (or @film connect Simkl), "
            "approve the app, then retry import."
        )
    store = get_store()
    imported = skipped = failed = qdrant_filled = 0
    errors: list[str] = []
    total = len(items)
    logger.info("simkl import: %d items to process (%s)", total, source)

    def _tick(i: int) -> None:
        if i % 25 == 0 or i == total:
            logger.info(
                "simkl import progress %d/%d (imported=%d skipped=%d qdrant_filled=%d failed=%d)",
                i,
                total,
                imported,
                skipped,
                qdrant_filled,
                failed,
            )

    for i, item in enumerate(items, 1):
        extracted = extract_media(item)
        if not extracted:
            _tick(i)
            continue
        simkl_id = extracted.get("simkl_id")
        if simkl_id and store.has_simkl_id(int(simkl_id)):
            skipped += 1
            existing = store.get_by_simkl_id(int(simkl_id))
            tid = existing.get("tmdb_id") if existing else None
            if existing and tid and point_exists(int(tid)) is False:
                if vector_upsert(existing):
                    qdrant_filled += 1
            _tick(i)
            continue
        try:
            kind = extracted["type"]
            tmdb_id = None
            raw_tmdb = extracted.get("tmdb_id")
            if raw_tmdb not in (None, ""):
                tmdb_id = int(raw_tmdb)
            elif extracted.get("imdb_id"):
                tmdb_id = find_tmdb_id_by_imdb(str(extracted["imdb_id"]), kind)
            row: dict[str, Any]
            if tmdb_id:
                try:
                    row = fetch_media(kind, tmdb_id)
                except Exception:
                    row = {
                        "type": kind,
                        "title": extracted["title"],
                        "year": extracted.get("year"),
                        "overview": extracted.get("overview") or "",
                        "tmdb_id": tmdb_id,
                        "imdb_id": extracted.get("imdb_id"),
                        "genres": ", ".join(extracted.get("genres") or []),
                        "director": extracted.get("director") or "",
                    }
            else:
                row = {
                    "type": kind,
                    "title": extracted["title"],
                    "year": extracted.get("year"),
                    "overview": extracted.get("overview") or "",
                    "tmdb_id": None,
                    "imdb_id": extracted.get("imdb_id"),
                    "genres": ", ".join(extracted.get("genres") or []),
                    "director": extracted.get("director") or "",
                }
            row["simkl_id"] = simkl_id
            row["user_rating"] = extracted.get("user_rating")
            saved = store.upsert_media(row, status=extracted.get("status") or "completed")
            if saved.get("tmdb_id"):
                vector_upsert(saved)
            imported += 1
        except Exception as exc:
            failed += 1
            title = extracted.get("title") or "?"
            errors.append(f"{title}: {exc}")
            logger.warning("simkl import failed for %s: %s", title, exc)
        _tick(i)
    return {
        "username": user,
        "source": source,
        "fetched": len(items),
        "imported": imported,
        "skipped": skipped,
        "qdrant_filled": qdrant_filled,
        "failed": failed,
        "errors": errors[:20],
    }
