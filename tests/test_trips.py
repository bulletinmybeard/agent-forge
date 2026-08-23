from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentforge.trips import models, renderer, schedule, store


def _place(pid: str, lat: float, lon: float, **extra) -> dict:
    row = {"id": pid, "label": pid.title(), "lat": lat, "lon": lon}
    row.update(extra)
    return row


def _raw_trip(**overrides) -> dict:
    trip = {
        "title": "Berlin → Hamburg",
        "profile": "driving-car",
        "departure": "2026-08-23T09:00:00+02:00",
        "origin": _place("home", 52.52, 13.40, required=True),
        "destination": _place("studio", 53.55, 9.99, required=True),
        "stops": [
            _place(
                "lunch",
                52.13,
                11.64,
                optional=True,
                enabled=True,
                kind="restaurant",
                dwell_min=60,
                notes="German lunch",
            )
        ],
        "itinerary_md": "## 09:00 Leave home",
    }
    trip.update(overrides)
    return trip


def test_validate_trip_fills_id_and_defaults():
    trip = models.validate_trip(_raw_trip())
    assert models.is_trip_id(trip["id"])
    assert trip["origin"]["required"] is True
    assert trip["stops"][0]["enabled"] is True
    assert trip["stops"][0]["dwell_min"] == 60


def test_validate_trip_requires_origin_coords():
    raw = _raw_trip()
    del raw["origin"]["lat"]
    with pytest.raises(ValueError, match="lat"):
        models.validate_trip(raw)


def test_validate_trip_rejects_invented_profile():
    with pytest.raises(ValueError, match="profile"):
        models.validate_trip(_raw_trip(profile="transit"))


def test_validate_trip_city_stay_without_destination():
    raw = _raw_trip()
    raw["destination"] = None
    raw["profile"] = "foot-walking"
    trip = models.validate_trip(raw)
    assert trip["destination"] is None
    assert trip["profile"] == "foot-walking"


def test_validate_trip_rejects_script_in_id():
    with pytest.raises(ValueError, match="id"):
        models.validate_trip(_raw_trip(origin=_place("<script>", 52.0, 4.0)))


def test_waypoints_skip_disabled_optional_stops():
    trip = models.validate_trip(_raw_trip())
    trip["stops"][0]["enabled"] = False
    points = models.waypoints(trip)
    assert [p["id"] for p in points] == ["home", "studio"]


def test_validate_place_keeps_address_and_website():
    trip = models.validate_trip(
        _raw_trip(
            stops=[
                _place(
                    "lunch",
                    52.13,
                    11.64,
                    optional=True,
                    address="Ernst-August-Platz 1, Hannover",
                    website="https://example.com",
                    dwell_min=60,
                )
            ]
        )
    )
    assert trip["stops"][0]["address"].startswith("Ernst-August")
    assert trip["stops"][0]["website"].startswith("https://")


def test_waypoints_include_enabled_stop():
    trip = models.validate_trip(_raw_trip())
    points = models.waypoints(trip)
    assert [p["id"] for p in points] == ["home", "lunch", "studio"]


def test_schedule_adds_leg_duration_and_dwell():
    departure = datetime(2026, 8, 23, 7, 0, tzinfo=timezone.utc)
    places = [
        _place("home", 52.52, 13.40),
        _place("lunch", 52.13, 11.64, dwell_min=30),
        _place("studio", 53.55, 9.99),
    ]
    legs = [
        {"distance_m": 1000, "duration_s": 600},
        {"distance_m": 2000, "duration_s": 1200},
    ]
    events = schedule.compute_schedule(departure, places, legs)
    kinds = [(e["place_id"], e["event"]) for e in events]
    assert kinds[0] == ("home", "depart")
    assert kinds[1] == ("lunch", "arrive")
    assert kinds[2] == ("lunch", "depart")
    assert kinds[3] == ("studio", "arrive")
    assert events[1]["at"] == "2026-08-23T07:10:00+00:00"
    assert events[2]["at"] == "2026-08-23T07:40:00+00:00"
    assert events[3]["at"] == "2026-08-23T08:00:00+00:00"


def test_store_roundtrip(tmp_path: Path):
    trips = store.TripStore(tmp_path)
    saved = trips.save(models.validate_trip(_raw_trip()))
    loaded = trips.load(saved["id"])
    assert loaded is not None
    assert loaded["title"] == "Berlin → Hamburg"
    assert loaded["id"] == saved["id"]


def test_store_rejects_path_escape(tmp_path: Path):
    trips = store.TripStore(tmp_path)
    assert trips.load("../etc/passwd") is None
    assert trips.load("not-a-uuid") is None


def test_render_html_escapes_title_and_inlines_trip(tmp_path: Path):
    trip = models.validate_trip(_raw_trip(title="Hamburg <script>alert(1)</script>"))
    trip["route"] = {
        "distance_m": 100.0,
        "duration_s": 20.0,
        "coordinates": [[52.52, 13.40], [53.55, 9.99]],
        "legs": [{"distance_m": 100.0, "duration_s": 20.0}],
    }
    html = renderer.render_trip(trip, api_base="")
    assert "<script>alert(1)</script>" not in html
    assert "Hamburg" in html
    assert "window.TRIP" in html
    assert trip["id"] in html
    assert "lunch" in html
    assert "leaflet" in html.lower()
