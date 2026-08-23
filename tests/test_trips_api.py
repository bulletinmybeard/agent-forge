from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentforge.trips import models, service, store
from web.server.trips.api import init_trips
from web.server.trips.api import router as trips_router


def _client(tmp_path: Path) -> TestClient:
    init_trips(tmp_path)
    app = FastAPI()
    app.include_router(trips_router)
    return TestClient(app)


def _save_trip(tmp_path: Path) -> dict:
    service.set_store(store.TripStore(tmp_path))
    return service.get_store().save(
        models.validate_trip(
            {
                "title": "Hamburg run",
                "profile": "driving-car",
                "departure": "2026-08-23T09:00:00+02:00",
                "origin": {"id": "home", "label": "Home", "lat": 52.52, "lon": 13.40},
                "destination": {"id": "end", "label": "End", "lat": 53.55, "lon": 9.99},
                "stops": [
                    {
                        "id": "lunch",
                        "label": "Lunch",
                        "lat": 52.13,
                        "lon": 11.64,
                        "optional": True,
                        "enabled": True,
                    }
                ],
                "route": {
                    "distance_m": 100.0,
                    "duration_s": 20.0,
                    "coordinates": [[52.52, 13.40], [53.55, 9.99]],
                    "legs": [{"distance_m": 100.0, "duration_s": 20.0}],
                },
            }
        )
    )


def test_get_trip_html(tmp_path: Path):
    trip = _save_trip(tmp_path)
    client = _client(tmp_path)
    response = client.get(f"/trips/{trip['id']}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Hamburg run" in response.text
    assert "window.TRIP" in response.text


def test_get_trip_html_suffix(tmp_path: Path):
    trip = _save_trip(tmp_path)
    client = _client(tmp_path)
    response = client.get(f"/trips/{trip['id']}.html")
    assert response.status_code == 200


def test_get_unknown_trip_404(tmp_path: Path):
    client = _client(tmp_path)
    response = client.get("/trips/not-a-uuid")
    assert response.status_code == 404


def test_reroute_endpoint_toggles_stop(tmp_path: Path, monkeypatch):
    trip = _save_trip(tmp_path)
    client = _client(tmp_path)

    def fake_directions(profile, coordinates):
        assert len(coordinates) == 2
        return {
            "distance_m": 50.0,
            "duration_s": 10.0,
            "coordinates": coordinates,
            "legs": [{"distance_m": 50.0, "duration_s": 10.0}],
        }

    monkeypatch.setattr("agentforge.trips.service.ors.directions", fake_directions)
    response = client.post(
        f"/api/trips/{trip['id']}/route",
        json={"enabled_stop_ids": []},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stops"][0]["enabled"] is False
    assert body["route"]["duration_s"] == 10.0
