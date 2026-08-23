from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agentforge.trips import ors
from agentforge.trips.models import validate_trip, waypoints
from agentforge.trips.schedule import attach_schedule, normalize_departure
from agentforge.trips.store import TripStore

# Extra driving vs origin→destination allowed for an optional stop.
MAX_DETOUR_S = 45 * 60
MAX_DETOUR_M = 50_000

_store: TripStore | None = None


def default_root() -> Path:
    env = os.environ.get("AGENTFORGE_TRIPS_DIR", "").strip()
    if env:
        return Path(env)
    # Compose mounts ./data at /app/data. Tools workers don't run the web
    # lifespan, so they must land on the same volume without going through
    # site-packages (where this file lives after pip install).
    if Path("/app/data").is_dir():
        return Path("/app/data/trips")
    return Path.cwd() / "data" / "trips"


def get_store() -> TripStore:
    global _store
    if _store is None:
        _store = TripStore(default_root())
    return _store


def set_store(store: TripStore | None) -> None:
    global _store
    _store = store


def apply_enabled(trip: dict[str, Any], enabled_stop_ids: list[str] | None) -> dict[str, Any]:
    if enabled_stop_ids is None:
        return trip
    wanted = {str(item) for item in enabled_stop_ids}
    for stop in trip.get("stops") or []:
        stop["enabled"] = stop["id"] in wanted
    return trip


def _coords(places: list[dict[str, Any]]) -> list[list[float]]:
    return [[p["lat"], p["lon"]] for p in places]


def _core_waypoints(trip: dict[str, Any]) -> list[dict[str, Any]]:
    points = [trip["origin"]]
    for stop in trip.get("stops") or []:
        if not stop.get("optional"):
            points.append(stop)
    dest = trip.get("destination")
    if dest:
        points.append(dest)
    return points


def prune_detours(trip: dict[str, Any]) -> list[dict[str, Any]]:
    """Disable optional stops that send the drive far off origin→destination."""
    dest = trip.get("destination")
    if dest is None:
        return []
    core = _core_waypoints(trip)
    if len(core) < 2:
        return []
    try:
        base = ors.directions(trip["profile"], _coords(core))
    except ors.OrsError:
        return []

    rejected: list[dict[str, Any]] = []
    required = [s for s in (trip.get("stops") or []) if not s.get("optional")]
    kept: list[dict[str, Any]] = []
    origin = trip["origin"]
    for stop in trip.get("stops") or []:
        if not stop.get("optional") or not stop.get("enabled", True):
            continue
        trial = [origin, *required, *kept, stop, dest]
        try:
            routed = ors.directions(trip["profile"], _coords(trial))
        except ors.OrsError as exc:
            stop["enabled"] = False
            rejected.append({"id": stop["id"], "label": stop.get("label"), "reason": exc.message})
            continue
        extra_s = float(routed.get("duration_s") or 0) - float(base.get("duration_s") or 0)
        extra_m = float(routed.get("distance_m") or 0) - float(base.get("distance_m") or 0)
        if extra_s > MAX_DETOUR_S or extra_m > MAX_DETOUR_M:
            stop["enabled"] = False
            rejected.append(
                {
                    "id": stop["id"],
                    "label": stop.get("label"),
                    "extra_min": int(round(extra_s / 60)),
                    "extra_km": round(extra_m / 1000, 1),
                    "reason": (
                        "Detour too far from the origin→destination corridor. "
                        "Use a stop along the route (ors_pois), not a landmark in another region."
                    ),
                }
            )
            continue
        kept.append(stop)
    return rejected


def refresh_route(trip: dict[str, Any]) -> dict[str, Any]:
    points = waypoints(trip)
    if len(points) < 2:
        trip["route"] = {"distance_m": 0.0, "duration_s": 0.0, "coordinates": [], "legs": []}
        trip["schedule"] = []
        return trip
    coords = [[p["lat"], p["lon"]] for p in points]
    trip["route"] = ors.directions(trip["profile"], coords)
    return attach_schedule(trip)


def publish(raw: dict[str, Any], *, trip_id: str | None = None, compute_route: bool = True) -> dict[str, Any]:
    trip = validate_trip(raw, trip_id=trip_id)
    trip["departure"] = normalize_departure(trip.get("departure") or "")
    route_error: str | None = None
    rejected: list[dict[str, Any]] = []
    if compute_route:
        rejected = prune_detours(trip)
        try:
            trip = refresh_route(trip)
        except ors.OrsError as exc:
            route_error = exc.message
            trip["route"] = {"distance_m": 0.0, "duration_s": 0.0, "coordinates": [], "legs": []}
            trip["schedule"] = []
    elif (trip.get("route") or {}).get("coordinates"):
        trip = attach_schedule(trip)
    saved = get_store().save(trip)
    result: dict[str, Any] = {"id": saved["id"], "url": f"/trips/{saved['id']}", "trip": saved}
    if route_error:
        result["route_error"] = route_error
    if rejected:
        result["rejected_stops"] = rejected
    return result


def load(trip_id: str) -> dict[str, Any] | None:
    return get_store().load(trip_id)


def hydrate(trip: dict[str, Any]) -> dict[str, Any]:
    """Fill missing legs/schedule. Old trips were stored without ORS segments."""
    route = trip.get("route") or {}
    if not route.get("legs"):
        if len(waypoints(trip)) < 2:
            return trip
        try:
            trip = refresh_route(trip)
        except ors.OrsError:
            return trip
        return get_store().save(trip)
    if not trip.get("schedule"):
        trip = attach_schedule(trip)
        return get_store().save(trip)
    return trip


def reroute(trip_id: str, enabled_stop_ids: list[str] | None) -> dict[str, Any]:
    current = get_store().load(trip_id)
    if current is None:
        raise KeyError(trip_id)
    apply_enabled(current, enabled_stop_ids)
    current = refresh_route(current)
    return get_store().save(current)
