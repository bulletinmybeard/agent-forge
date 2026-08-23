from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agentforge.trips.models import waypoints


def normalize_departure(value: str, *, now: datetime | None = None) -> str:
    """If the clock time is in the past (wrong year, yesterday), roll it forward."""
    parsed = parse_departure(value)
    current = now
    if current is None:
        current = datetime.now(parsed.tzinfo if parsed is not None else timezone.utc)
    elif current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if parsed is None:
        tomorrow = (current + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        return tomorrow.isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    current = current.astimezone(parsed.tzinfo)
    if parsed > current:
        return parsed.replace(microsecond=0).isoformat()
    candidate = parsed.replace(year=current.year, month=current.month, day=current.day, microsecond=0)
    if candidate <= current:
        candidate = candidate + timedelta(days=1)
    return candidate.isoformat()


def parse_departure(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def compute_schedule(
    departure: datetime,
    places: list[dict[str, Any]],
    legs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(places) < 2:
        return []
    events: list[dict[str, Any]] = []
    cursor = departure
    events.append({"at": _iso(cursor), "place_id": places[0]["id"], "event": "depart"})
    for index, leg in enumerate(legs):
        if index + 1 >= len(places):
            break
        duration = float(leg.get("duration_s") or 0)
        cursor = cursor + timedelta(seconds=duration)
        dest = places[index + 1]
        events.append({"at": _iso(cursor), "place_id": dest["id"], "event": "arrive"})
        dwell = int(dest.get("dwell_min") or 0)
        is_last = index + 1 == len(places) - 1
        if dwell > 0 and not is_last:
            cursor = cursor + timedelta(minutes=dwell)
            events.append({"at": _iso(cursor), "place_id": dest["id"], "event": "depart"})
    return events


def attach_schedule(trip: dict[str, Any]) -> dict[str, Any]:
    departure = parse_departure(str(trip.get("departure") or ""))
    route = trip.get("route") or {}
    legs = route.get("legs") or []
    places = waypoints(trip)
    if departure is None or len(places) < 2 or not legs:
        trip["schedule"] = []
        return trip
    trip["schedule"] = compute_schedule(departure, places, legs)
    return trip
