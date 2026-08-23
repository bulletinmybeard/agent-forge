from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentforge.trips import models, schedule, service, store
from agentforge.trips.ors import OrsError


def _trip(**overrides) -> dict:
    trip = {
        "title": "Test trip",
        "profile": "driving-car",
        "departure": "2026-08-23T09:00:00+02:00",
        "origin": {"id": "home", "label": "Home", "lat": 52.52, "lon": 13.40, "required": True},
        "destination": {"id": "end", "label": "End", "lat": 53.55, "lon": 9.99, "required": True},
        "stops": [
            {
                "id": "lunch",
                "label": "Lunch",
                "lat": 52.13,
                "lon": 11.64,
                "optional": True,
                "enabled": True,
                "dwell_min": 45,
            }
        ],
    }
    trip.update(overrides)
    return models.validate_trip(trip)


def test_reroute_skips_disabled_stops(tmp_path: Path, monkeypatch):
    trips = store.TripStore(tmp_path)
    service.set_store(trips)
    saved = trips.save(_trip())

    captured = {}

    def fake_directions(profile, coordinates):
        captured["coordinates"] = coordinates
        return {
            "distance_m": 10.0,
            "duration_s": 20.0,
            "coordinates": coordinates,
            "legs": [{"distance_m": 10.0, "duration_s": 20.0}],
        }

    monkeypatch.setattr(service.ors, "directions", fake_directions)
    updated = service.reroute(saved["id"], enabled_stop_ids=[])
    assert [p[0] for p in captured["coordinates"]] == [52.52, 53.55]
    assert updated["stops"][0]["enabled"] is False


def test_reroute_can_disable_non_optional_stop(tmp_path: Path, monkeypatch):
    service.set_store(store.TripStore(tmp_path))
    trip = _trip()
    trip["stops"][0]["optional"] = False
    saved = service.get_store().save(trip)

    def fake_directions(profile, coordinates):
        return {
            "distance_m": 10.0,
            "duration_s": 20.0,
            "coordinates": coordinates,
            "legs": [{"distance_m": 10.0, "duration_s": 20.0}],
        }

    monkeypatch.setattr(service.ors, "directions", fake_directions)
    updated = service.reroute(saved["id"], enabled_stop_ids=[])
    assert updated["stops"][0]["enabled"] is False
    assert updated["route"]["duration_s"] == 20.0
    assert updated["schedule"]


def test_publish_saves_when_routing_fails(tmp_path: Path, monkeypatch):
    service.set_store(store.TripStore(tmp_path))

    def boom(profile, coordinates):
        raise OrsError(502, "OpenRouteService HTTP 502 (upstream gateway). Retry.")

    monkeypatch.setattr(service.ors, "directions", boom)
    result = service.publish(_trip())
    assert result["url"] == f"/trips/{result['id']}"
    assert result["route_error"]
    loaded = service.load(result["id"])
    assert loaded is not None
    assert loaded["origin"]["id"] == "home"


def test_hydrate_refetches_when_legs_missing(tmp_path: Path, monkeypatch):
    service.set_store(store.TripStore(tmp_path))
    trip = _trip()
    trip["route"] = {
        "distance_m": 210000.0,
        "duration_s": 9000.0,
        "coordinates": [[52.52, 13.40], [53.55, 9.99]],
        "legs": [],
    }
    saved = service.get_store().save(trip)

    def fake_directions(profile, coordinates):
        assert len(coordinates) == 3
        return {
            "distance_m": 210000.0,
            "duration_s": 9000.0,
            "coordinates": coordinates,
            "legs": [
                {"distance_m": 197000.0, "duration_s": 8400.0},
                {"distance_m": 13000.0, "duration_s": 600.0},
            ],
        }

    monkeypatch.setattr(service.ors, "directions", fake_directions)
    hydrated = service.hydrate(saved)
    assert len(hydrated["route"]["legs"]) == 2
    assert hydrated["schedule"]
    assert hydrated["schedule"][0]["event"] == "depart"
    arrive_dest = [e for e in hydrated["schedule"] if e["place_id"] == "end" and e["event"] == "arrive"]
    assert arrive_dest


def test_normalize_departure_rolls_past_year_forward():
    now = datetime(2026, 8, 22, 20, 0, tzinfo=timezone(timedelta(hours=2)))
    out = schedule.normalize_departure("2024-09-27T09:00:00+02:00", now=now)
    assert out.startswith("2026-08-23T09:00:00")


def test_publish_disables_far_detour(tmp_path: Path, monkeypatch):
    service.set_store(store.TripStore(tmp_path))
    trip = _trip()
    trip["stops"] = [
        {
            "id": "wurstkuchl",
            "label": "Historische Wurstkuchl, Regensburg",
            "lat": 49.02,
            "lon": 12.09,
            "optional": True,
            "enabled": True,
            "kind": "restaurant",
            "dwell_min": 45,
        }
    ]
    trip = models.validate_trip(trip)

    def fake_directions(profile, coordinates):
        lats = [c[0] for c in coordinates]
        if any(lat < 50 for lat in lats):
            return {
                "distance_m": 1_428_000.0,
                "duration_s": 44_820.0,
                "coordinates": coordinates,
                "legs": [{"distance_m": 700_000.0, "duration_s": 22_000.0}] * (len(coordinates) - 1),
            }
        n = max(1, len(coordinates) - 1)
        return {
            "distance_m": 211_000.0,
            "duration_s": 9_000.0,
            "coordinates": coordinates,
            "legs": [{"distance_m": 211_000.0 / n, "duration_s": 9_000.0 / n}] * n,
        }

    monkeypatch.setattr(service.ors, "directions", fake_directions)
    result = service.publish(trip)
    assert result["trip"]["stops"][0]["enabled"] is False
    assert result["rejected_stops"]
    assert result["rejected_stops"][0]["id"] == "wurstkuchl"
    assert result["trip"]["route"]["distance_m"] == 211_000.0


def test_publish_keeps_on_corridor_stop(tmp_path: Path, monkeypatch):
    service.set_store(store.TripStore(tmp_path))

    def fake_directions(profile, coordinates):
        extra = 600 if len(coordinates) > 2 else 0
        n = max(1, len(coordinates) - 1)
        return {
            "distance_m": 211_000.0 + extra * 10,
            "duration_s": 9_000.0 + extra,
            "coordinates": coordinates,
            "legs": [{"distance_m": 100.0, "duration_s": 100.0}] * n,
        }

    monkeypatch.setattr(service.ors, "directions", fake_directions)
    result = service.publish(_trip())
    assert result["trip"]["stops"][0]["enabled"] is True
    assert "rejected_stops" not in result


def test_publish_returns_url(tmp_path: Path, monkeypatch):
    service.set_store(store.TripStore(tmp_path))

    def fake_directions(profile, coordinates):
        return {
            "distance_m": 1.0,
            "duration_s": 2.0,
            "coordinates": coordinates,
            "legs": [{"distance_m": 1.0, "duration_s": 2.0}],
        }

    monkeypatch.setattr(service.ors, "directions", fake_directions)
    result = service.publish(_trip())
    assert result["url"] == f"/trips/{result['id']}"
    assert result["trip"]["route"]["distance_m"] == 1.0
