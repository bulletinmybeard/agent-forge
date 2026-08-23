from __future__ import annotations

import re
import uuid
from typing import Any

from agentforge.trips.ors import PROFILES

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_TITLE = 120
_MAX_LABEL = 200
_MAX_NOTES = 2000
_MAX_STOPS = 20
KINDS = ("origin", "destination", "restaurant", "sight", "lodging", "break", "cafe", "other")


def is_trip_id(value: str) -> bool:
    return bool(value) and bool(_UUID_RE.match(value))


def new_trip_id() -> str:
    return str(uuid.uuid4())


def _require_str(value: Any, field: str, *, max_len: int, allow_empty: bool = False) -> str:
    if value is None:
        if allow_empty:
            return ""
        raise ValueError(f"{field} is required.")
    text = str(value).strip()
    if not text and not allow_empty:
        raise ValueError(f"{field} is required.")
    if len(text) > max_len:
        raise ValueError(f"{field} is too long.")
    return text


def _coord(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number.") from exc
    return number


def validate_place(raw: Any, *, default_kind: str, required: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Place must be an object.")
    place_id = _require_str(raw.get("id"), "id", max_len=64)
    if not _ID_RE.match(place_id):
        raise ValueError("id must be 1-64 letters, digits, underscore, or hyphen.")
    lat = _coord(raw.get("lat"), "lat")
    lon = _coord(raw.get("lon"), "lon")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range.")
    kind = str(raw.get("kind") or default_kind).strip()
    if kind not in KINDS:
        kind = "other"
    optional = bool(raw.get("optional", not required))
    enabled = bool(raw.get("enabled", True))
    if required:
        optional = False
        enabled = True
    dwell = raw.get("dwell_min", 0)
    try:
        dwell_min = max(0, min(int(dwell), 24 * 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("dwell_min must be an integer.") from exc
    image_url = str(raw.get("image_url") or "").strip()
    if image_url and not (image_url.startswith("https://") or image_url.startswith("http://")):
        raise ValueError("image_url must be an http(s) URL.")
    website = str(raw.get("website") or "").strip()
    if website and not (website.startswith("https://") or website.startswith("http://")):
        raise ValueError("website must be an http(s) URL.")
    return {
        "id": place_id,
        "label": _require_str(raw.get("label"), "label", max_len=_MAX_LABEL),
        "lat": lat,
        "lon": lon,
        "kind": kind,
        "required": required,
        "optional": optional,
        "enabled": enabled,
        "dwell_min": dwell_min,
        "opens": _require_str(raw.get("opens"), "opens", max_len=32, allow_empty=True),
        "closes": _require_str(raw.get("closes"), "closes", max_len=32, allow_empty=True),
        "notes": _require_str(raw.get("notes"), "notes", max_len=_MAX_NOTES, allow_empty=True),
        "address": _require_str(raw.get("address"), "address", max_len=_MAX_LABEL, allow_empty=True),
        "website": website,
        "image_url": image_url,
    }


def validate_trip(raw: dict[str, Any], *, trip_id: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Trip must be an object.")
    given = str(raw.get("id") or "").strip()
    if trip_id:
        if given and given != trip_id:
            raise ValueError("Trip id does not match.")
        given = trip_id
    if given:
        if not is_trip_id(given):
            raise ValueError("id must be a UUID.")
        ident = given
    else:
        ident = new_trip_id()

    profile = str(raw.get("profile") or "driving-car").strip()
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(PROFILES)}.")

    origin = validate_place(raw.get("origin"), default_kind="origin", required=True)
    dest_raw = raw.get("destination")
    destination = None
    if dest_raw:
        destination = validate_place(dest_raw, default_kind="destination", required=True)

    stops_raw = raw.get("stops") or []
    if not isinstance(stops_raw, list):
        raise ValueError("stops must be an array.")
    if len(stops_raw) > _MAX_STOPS:
        raise ValueError(f"At most {_MAX_STOPS} stops.")
    stops = [validate_place(item, default_kind="other", required=False) for item in stops_raw]
    seen = {origin["id"]}
    if destination:
        seen.add(destination["id"])
    for stop in stops:
        if stop["id"] in seen:
            raise ValueError(f"Duplicate place id: {stop['id']}")
        seen.add(stop["id"])

    route = raw.get("route")
    if route is not None and not isinstance(route, dict):
        raise ValueError("route must be an object.")
    schedule_events = raw.get("schedule")
    if schedule_events is not None and not isinstance(schedule_events, list):
        raise ValueError("schedule must be an array.")

    return {
        "id": ident,
        "title": _require_str(raw.get("title"), "title", max_len=_MAX_TITLE),
        "profile": profile,
        "departure": _require_str(raw.get("departure"), "departure", max_len=40, allow_empty=True),
        "origin": origin,
        "destination": destination,
        "stops": stops,
        "itinerary_md": _require_str(raw.get("itinerary_md"), "itinerary_md", max_len=20_000, allow_empty=True),
        "route": route,
        "schedule": schedule_events or [],
    }


def waypoints(trip: dict[str, Any]) -> list[dict[str, Any]]:
    points = [trip["origin"]]
    for stop in trip.get("stops") or []:
        if stop.get("enabled", True):
            points.append(stop)
    dest = trip.get("destination")
    if dest:
        points.append(dest)
    return points
