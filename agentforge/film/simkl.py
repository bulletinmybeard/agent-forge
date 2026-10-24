"""Simkl client."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agentforge import __version__
from agentforge.config import get_config

_BASE = "https://api.simkl.com"
_TIMEOUT = 30
_UA = f"agentforge/{__version__}"


def client_id() -> str:
    env = os.environ.get("SIMKL_CLIENT_ID", "").strip()
    if env:
        return env
    return str(get_config().get("tools.film.simkl_client_id", "") or "").strip()


def access_token() -> str:
    from agentforge.film.oauth import get_valid_token

    return get_valid_token()


def default_username() -> str:
    env = os.environ.get("SIMKL_USERNAME", "").strip()
    if env:
        return env
    return str(get_config().get("tools.film.simkl_username", "") or "").strip()


def normalize_username(raw: str) -> str:
    text = (raw or "").strip()
    url = re.search(r"simkl\.com/([A-Za-z0-9_-]+)", text, re.I)
    if url:
        text = url.group(1)
    if re.fullmatch(r"[0-9a-f]{32,}", text, re.I):
        raise RuntimeError(
            "tools.film.simkl_username looks like an API key, not a Simkl profile name. "
            "Set it to the public username (simkl.com/<username>), not simkl_client_id."
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise RuntimeError("Simkl username must be the public profile slug (letters, digits, _-).")
    return text


def _request(path: str, *, params: dict[str, str] | None = None, token: str = "") -> Any:
    key = client_id()
    if not key:
        raise RuntimeError("Simkl client id missing. Set SIMKL_CLIENT_ID or tools.film.simkl_client_id.")
    url = f"{_BASE}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {
        "Content-Type": "application/json",
        "simkl-api-key": key,
        "User-Agent": _UA,
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"Simkl HTTP {exc.code}: {body[:200]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Simkl connection error: {exc}") from exc


def _flatten_kind(items: list[Any], nested_key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        nested = raw.get(nested_key)
        if nested:
            merged = {**raw, nested_key: nested}
            out.append(merged)
        else:
            out.append(raw)
    return out


def fetch_sync_watchlist(token: str) -> list[dict[str, Any]]:
    """Phase-1 initial sync: /sync/all-items/{movies,shows,anime} sequentially."""
    cid = client_id()
    extra = {"client_id": cid, "app-name": "agentforge", "app-version": __version__}
    items: list[dict[str, Any]] = []
    mapping = (("movies", "movie"), ("shows", "show"), ("anime", "anime"))
    for path_kind, nested in mapping:
        data = _request(f"/sync/all-items/{path_kind}", params=extra, token=token)
        if isinstance(data, dict):
            bucket = data.get(path_kind) or data.get("movies") or data.get("shows") or data.get("anime") or []
            items.extend(_flatten_kind(bucket if isinstance(bucket, list) else [], nested))
        elif isinstance(data, list):
            items.extend(_flatten_kind(data, nested))
    return items


def fetch_public_watchlist(username: str) -> list[dict[str, Any]]:
    """Public /users/{username}/{movies|tv|anime} — same as playground getPublicWatchlist."""
    user = normalize_username(username)
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for kind in ("movies", "tv", "anime"):
        try:
            data = _request(f"/users/{user}/{kind}", params={"extended": "full"})
        except RuntimeError as exc:
            errors.append(f"{kind}: {exc}")
            continue
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            nested = data.get("movies") or data.get("shows") or data.get("anime") or data.get("items")
            if isinstance(nested, list):
                items.extend(nested)
            else:
                errors.append(f"{kind}: unexpected object keys {list(data)[:8]}")
        elif data:
            errors.append(f"{kind}: unexpected payload {type(data).__name__}")
    if not items and errors:
        raise RuntimeError("Simkl public watchlist empty and every type failed: " + "; ".join(errors))
    return items


def extract_media(item: dict[str, Any]) -> dict[str, Any] | None:
    """Same nesting as playground getMediaFromItem."""
    if item.get("movie"):
        media, kind = item["movie"], "movie"
    elif item.get("show"):
        media, kind = item["show"], "tv"
    elif item.get("anime"):
        media, kind = item["anime"], "tv"
    else:
        return None
    if not isinstance(media, dict):
        return None
    ids = media.get("ids") or {}
    status = item.get("status") or "completed"
    if status == "plantowatch":
        mapped = "plantowatch"
    elif status in ("watching", "hold", "dropped", "completed"):
        mapped = status
    else:
        mapped = "completed"
    tmdb_raw = ids.get("tmdb")
    return {
        "type": kind,
        "title": media.get("title") or "",
        "year": media.get("year"),
        "overview": media.get("overview") or "",
        "runtime": media.get("runtime"),
        "simkl_id": ids.get("simkl"),
        "tmdb_id": int(tmdb_raw) if isinstance(tmdb_raw, (int, str)) and str(tmdb_raw).isdigit() else tmdb_raw,
        "imdb_id": ids.get("imdb"),
        "genres": media.get("genres") or [],
        "director": media.get("director") or "",
        "status": mapped,
        "user_rating": item.get("user_rating"),
    }
