from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agentforge.config import get_config

DEFAULT_BASE = "https://api.openrouteservice.org"
PROFILES = ("driving-car", "foot-walking")
TIMEOUT_S = 20
USER_AGENT = "agentforge-trip/0.1"
MAX_QUERY_LEN = 200
COORD_ROUND = 5
MAX_RETRIES = 3
RETRY_STATUSES = frozenset({429, 502, 503})
_sleep = time.sleep

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# ORS category_group_ids (https://giscience.github.io/openrouteservice/api-reference/endpoints/poi/)
_POI_GROUP_IDS = {
    "food": [560],
    "tourism": [620],
    "accommodation": [100],
    "shops": [420],
    "transport": [580],
    "natural": [360],
}

_route_cache: dict[str, dict[str, Any]] = {}


class OrsError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _config_api_key() -> str:
    return str(get_config().get("tools.ors.api_key", "") or "").strip()


def _api_key() -> str:
    env = os.environ.get("ORS_API_KEY", "").strip()
    if env:
        return env
    return _config_api_key()


def _base_url() -> str:
    raw = str(get_config().get("tools.ors.base_url", "") or "").strip()
    return raw.rstrip("/") if raw else DEFAULT_BASE


def _as_lonlat(lat: float, lon: float) -> list[float] | None:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return [lon_f, lat_f]


def _pelias_results(payload: dict) -> list[dict]:
    results = []
    for feature in payload.get("features") or []:
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        props = feature.get("properties") or {}
        if len(coords) < 2:
            continue
        label = props.get("label") or props.get("name")
        if not label:
            continue
        lat = float(coords[1])
        lon = float(coords[0])
        results.append({"label": str(label), "lat": lat, "lon": lon, "lat_lon": [lat, lon]})
    return results


def _tokens(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(text.casefold()) if len(tok) >= 2}


def _rank_geocode(query: str, results: list[dict]) -> list[dict]:
    query_tokens = _tokens(query)
    if not query_tokens or len(results) < 2:
        return results

    def score(item: dict) -> float:
        label_tokens = _tokens(str(item.get("label") or ""))
        return len(query_tokens & label_tokens) / len(query_tokens)

    return sorted(results, key=score, reverse=True)


def _parse_directions(payload: dict) -> dict[str, Any]:
    features = payload.get("features") or []
    if not features:
        raise OrsError(404, "No route between those points.")
    feature = features[0]
    props = feature.get("properties") or {}
    summary = props.get("summary") or {}
    geometry = feature.get("geometry") or {}
    line = geometry.get("coordinates") or []
    if geometry.get("type") == "LineString":
        latlngs = [[pt[1], pt[0]] for pt in line if len(pt) >= 2]
    else:
        latlngs = []
    legs = []
    for segment in props.get("segments") or []:
        legs.append(
            {
                "distance_m": float(segment.get("distance") or 0),
                "duration_s": float(segment.get("duration") or 0),
            }
        )
    return {
        "distance_m": float(summary.get("distance") or 0),
        "duration_s": float(summary.get("duration") or 0),
        "coordinates": latlngs,
        "legs": legs,
    }


def _ors_message(status: int, detail: str) -> str:
    if status in (401, 403):
        return "OpenRouteService rejected the API key."
    if status == 404:
        return "No route between those points."
    if status == 429:
        return "OpenRouteService quota reached. Try later."
    if "<html" in (detail or "").casefold() or "<center>" in (detail or "").casefold():
        return f"OpenRouteService HTTP {status} (upstream gateway). Retry."
    return f"OpenRouteService error {status}: {detail or 'no detail'}"


def _request(
    method: str,
    url: str,
    body: dict | None = None,
    accept: str = "application/json",
) -> dict:
    key = _api_key()
    if not key:
        raise OrsError(503, "Set ORS_API_KEY in the environment or tools.ors.api_key.")

    data = None
    headers = {
        "Authorization": key,
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    last_error: OrsError | None = None
    for attempt in range(MAX_RETRIES):
        request = Request(url, data=data, headers=headers, method=method)
        context = ssl.create_default_context()
        try:
            with urlopen(request, timeout=TIMEOUT_S, context=context) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = OrsError(exc.code, _ors_message(exc.code, detail))
            if exc.code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                _sleep(0.4 * (attempt + 1))
                continue
            raise last_error from exc
        except URLError as exc:
            last_error = OrsError(502, f"Could not reach OpenRouteService ({exc.reason}).")
            if attempt < MAX_RETRIES - 1:
                _sleep(0.4 * (attempt + 1))
                continue
            raise last_error from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = OrsError(502, "OpenRouteService returned non-JSON.")
            if attempt < MAX_RETRIES - 1:
                _sleep(0.4 * (attempt + 1))
                continue
            raise last_error from exc
        if not isinstance(parsed, dict):
            raise OrsError(502, "OpenRouteService returned unexpected JSON.")
        return parsed

    raise last_error or OrsError(502, "OpenRouteService request failed.")


def geocode(text: str, lat: float | None = None, lon: float | None = None, size: int = 6) -> list[dict]:
    query = (text or "").strip()
    if not query or len(query) < 2:
        return []
    if len(query) > MAX_QUERY_LEN:
        raise OrsError(400, "Search is too long.")
    params: dict[str, str] = {"text": query, "size": str(max(1, min(int(size), 10)))}
    if lat is not None and lon is not None:
        params["focus.point.lat"] = str(lat)
        params["focus.point.lon"] = str(lon)
    payload = _request("GET", f"{_base_url()}/geocode/search?{urlencode(params)}")
    return _rank_geocode(query, _pelias_results(payload))


def reverse(lat: float, lon: float) -> dict:
    point = _as_lonlat(lat, lon)
    if point is None:
        raise OrsError(400, "Need lat and lon.")
    params = {
        "point.lat": f"{point[1]:.7f}",
        "point.lon": f"{point[0]:.7f}",
        "size": "1",
    }
    payload = _request("GET", f"{_base_url()}/geocode/reverse?{urlencode(params)}")
    results = _pelias_results(payload)
    label = results[0]["label"] if results else None
    return {"label": label, "lat": point[1], "lon": point[0]}


def _cache_key(profile: str, coordinates: list[list[float]]) -> str:
    rounded = [[round(lat, COORD_ROUND), round(lon, COORD_ROUND)] for lat, lon in coordinates]
    raw = json.dumps({"p": profile, "c": rounded}, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clear_route_cache() -> None:
    _route_cache.clear()


def directions(profile: str, coordinates: list[list[float]]) -> dict[str, Any]:
    if profile not in PROFILES:
        raise OrsError(400, f"Profile must be one of: {', '.join(PROFILES)}.")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise OrsError(400, "Need at least two [lat, lon] waypoints.")

    ors_coords: list[list[float]] = []
    for pair in coordinates:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise OrsError(400, "Each waypoint must be [lat, lon].")
        point = _as_lonlat(pair[0], pair[1])
        if point is None:
            raise OrsError(400, "Waypoint lat/lon out of range.")
        ors_coords.append(point)
    if any(ors_coords[i] == ors_coords[i + 1] for i in range(len(ors_coords) - 1)):
        raise OrsError(400, "Consecutive waypoints are the same point.")

    key = _cache_key(profile, [[c[1], c[0]] for c in ors_coords])
    cached = _route_cache.get(key)
    if cached is not None:
        return json.loads(json.dumps(cached))

    payload = _request(
        "POST",
        f"{_base_url()}/v2/directions/{profile}/geojson",
        {
            "coordinates": ors_coords,
            "instructions": True,
            "elevation": False,
        },
        accept="application/geo+json",
    )
    route = _parse_directions(payload)
    _route_cache[key] = route
    return json.loads(json.dumps(route))


def pois(
    category_group: str,
    lat: float | None = None,
    lon: float | None = None,
    linestring: list[list[float]] | None = None,
    buffer_m: int = 500,
    limit: int = 20,
) -> list[dict]:
    group_ids = _POI_GROUP_IDS.get(category_group)
    if not group_ids:
        raise OrsError(400, f"Unknown category_group. Use: {', '.join(sorted(_POI_GROUP_IDS))}.")
    buf = max(50, min(int(buffer_m), 2000))
    geometry: dict[str, Any]
    if linestring:
        coords = []
        for pair in linestring:
            point = _as_lonlat(pair[0], pair[1])
            if point is None:
                continue
            coords.append(point)
        if len(coords) < 2:
            raise OrsError(400, "linestring needs at least two [lat, lon] points.")
        # ORS POI buffer on long routes is expensive; sample down.
        if len(coords) > 80:
            step = max(1, len(coords) // 80)
            coords = coords[::step]
            if coords[-1] != linestring_last(linestring):
                last = _as_lonlat(linestring[-1][0], linestring[-1][1])
                if last:
                    coords.append(last)
        geometry = {
            "geojson": {"type": "LineString", "coordinates": coords},
            "buffer": buf,
        }
    else:
        point = _as_lonlat(lat if lat is not None else 0, lon if lon is not None else 0)
        if point is None or lat is None or lon is None:
            raise OrsError(400, "Need lat/lon or a linestring.")
        geometry = {
            "geojson": {"type": "Point", "coordinates": point},
            "buffer": buf,
        }

    payload = _request(
        "POST",
        f"{_base_url()}/pois",
        {
            "request": "pois",
            "geometry": geometry,
            "filters": {"category_group_ids": group_ids},
            "limit": max(1, min(int(limit), 50)),
        },
    )
    return _parse_pois(payload)


def linestring_last(linestring: list[list[float]]) -> list[float] | None:
    pair = linestring[-1]
    return _as_lonlat(pair[0], pair[1])


def _parse_pois(payload: dict) -> list[dict]:
    features = payload.get("features")
    if not isinstance(features, list):
        # some ORS POI responses nest under "pois"
        nested = payload.get("pois")
        if isinstance(nested, dict):
            features = nested.get("features") or []
        else:
            features = []
    results = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        props = feature.get("properties") or {}
        if len(coords) < 2:
            continue
        name = props.get("osm_tags", {}).get("name") if isinstance(props.get("osm_tags"), dict) else None
        name = name or props.get("name") or props.get("osm_id")
        if not name:
            continue
        results.append(
            {
                "name": str(name),
                "lat": float(coords[1]),
                "lon": float(coords[0]),
                "category": props.get("category_ids") or props.get("category"),
                "osm_id": props.get("osm_id"),
            }
        )
    return results
