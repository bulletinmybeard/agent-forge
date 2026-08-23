"""OpenRouteService tools — geocode, reverse, directions, POIs.

Always registered. Each tool errors until ``ORS_API_KEY`` or
``tools.ors.api_key`` is set. Coordinates are ``[lat, lon]`` (agent-facing);
the client converts to ORS ``[lon, lat]``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from agentforge.trips import ors
from agentforge.trips.ors import PROFILES, OrsError

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(exc: Exception) -> str:
    if isinstance(exc, OrsError):
        return _dumps({"error": exc.message, "status": exc.status})
    return _dumps({"error": str(exc)})


def _parse_points(raw: str | list, *, min_count: int = 1) -> list[list[float]]:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise OrsError(400, "coordinates are required.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrsError(400, "coordinates must be a JSON array of [lat, lon] pairs.") from exc
    else:
        parsed = raw
    if not isinstance(parsed, list) or not parsed:
        raise OrsError(400, "coordinates must be a JSON array of [lat, lon] pairs.")
    points: list[list[float]] = []
    for item in parsed:
        if isinstance(item, dict) and "lat" in item and "lon" in item:
            points.append([float(item["lat"]), float(item["lon"])])
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise OrsError(400, "Each coordinate must be [lat, lon] or {lat, lon}.")
        points.append([float(item[0]), float(item[1])])
    if len(points) < min_count:
        raise OrsError(400, f"Need at least {min_count} coordinate pair(s).")
    return points


@tool(locality="remote")
def ors_geocode(text: str, lat: float = 0.0, lon: float = 0.0) -> str:
    """Geocode a place name or address with OpenRouteService.

    When to use: Resolve "home", an address, or a venue to lat/lon before routing.
    When NOT to use: You already have coordinates (use ors_route / ors_reverse).
    Input: text - query (address, city, venue). lat/lon - optional focus point
        so nearby matches rank first (pass 0,0 to skip).
    Output: JSON {results: [{label, lat, lon, lat_lon: [lat, lon]}, ...]}, best
        match first. Copy lat/lon by name — do not swap them.
    """
    try:
        focus_lat = lat if lat else None
        focus_lon = lon if lon else None
        results = ors.geocode(text, lat=focus_lat, lon=focus_lon)
        return _dumps({"results": results})
    except Exception as exc:
        return _error(exc)


@tool(locality="remote")
def ors_reverse(lat: float, lon: float) -> str:
    """Reverse-geocode a lat/lon to an address label.

    When to use: Label a map click or a POI that only has coordinates.
    Input: lat, lon in WGS84.
    Output: JSON {label, lat, lon}.
    """
    try:
        return _dumps(ors.reverse(lat, lon))
    except Exception as exc:
        return _error(exc)


@tool(locality="remote")
def ors_route(profile: str, coordinates: str) -> str:
    """Calculate a driving-car or foot-walking route through ordered waypoints.

    When to use: After geocoding origin, destination, and any stops. Pass every
        enabled waypoint in visit order (origin first, destination last).
    When NOT to use: Searching for restaurants — use ors_pois or web_search first.
    Input: profile - "driving-car" or "foot-walking". coordinates - JSON array of
        {"lat": 52.07, "lon": 4.41} objects (preferred) or [lat, lon] pairs. At least two.
    Output: JSON {distance_m, duration_s, coordinates: [[lat,lon],...], legs: [{distance_m, duration_s}]}.
        One leg per consecutive waypoint pair.
    """
    try:
        points = _parse_points(coordinates, min_count=2)
        if profile not in PROFILES:
            raise OrsError(400, f"profile must be one of: {', '.join(PROFILES)}.")
        route = ors.directions(profile, points)
        return _dumps(route)
    except Exception as exc:
        return _error(exc)


@tool(locality="remote")
def ors_pois(
    category_group: str,
    lat: float = 0.0,
    lon: float = 0.0,
    linestring: str = "",
    buffer_m: int = 500,
) -> str:
    """Find OpenStreetMap POIs near a point or along a route.

    When to use: Restaurants, tourism, lodging along a just-computed route.
        Prefer a downsampled route linestring so results sit on the way, not
        only at the destination. Do not dump the full polyline; every 20th point
        (max ~40) is enough.
    Input: category_group - food, tourism, accommodation, shops, transport, or
        natural. lat/lon - search around a point. linestring - JSON [[lat,lon],...]
        sampled from ors_route.coordinates (wins over lat/lon). buffer_m - search radius.
    Output: JSON {results: [{name, lat, lon, category, osm_id}, ...]}.
        POIs have no opening hours — verify those with web_fetch.
    """
    try:
        line = _parse_points(linestring, min_count=2) if linestring.strip() else None
        results = ors.pois(
            category_group,
            lat=lat or None,
            lon=lon or None,
            linestring=line,
            buffer_m=buffer_m,
        )
        return _dumps({"results": results})
    except Exception as exc:
        return _error(exc)


def register_ors_tools(registry: ToolRegistry) -> int:
    """Register OpenRouteService tools. Always present; they error without a key."""
    registry.register_category_hint(
        "ORS",
        "OpenRouteService geocoding and routing. Use ors_geocode then ors_route "
        f"with profile {', '.join(PROFILES)}. ors_pois finds food/tourism along a "
        "route linestring. Coordinates are [lat, lon].",
    )
    tools = [ors_geocode, ors_reverse, ors_route, ors_pois]
    for func in tools:
        registry.register(func, category="ORS")
        logger.debug("Registered ORS tool: %s", func.__name__)
    return len(tools)
