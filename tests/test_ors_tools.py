from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from agentforge.tools.registry import ToolRegistry
from agentforge.trips import ors as ors_mod


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _pelias_feature(label: str, lon: float, lat: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"label": label, "name": label.split(",")[0]},
    }


def test_pelias_results_use_lat_lon_not_ors_order():
    payload = {
        "features": [
            _pelias_feature("Alexanderplatz, Berlin", 13.4132, 52.5219),
            {"type": "Feature", "geometry": {"coordinates": [1]}, "properties": {}},
        ]
    }
    results = ors_mod._pelias_results(payload)
    assert results[0]["label"] == "Alexanderplatz, Berlin"
    assert results[0]["lat"] == pytest.approx(52.5219)
    assert results[0]["lon"] == pytest.approx(13.4132)
    assert results[0]["lat_lon"] == [52.5219, 13.4132]


def test_parse_directions_converts_lonlat_and_segments():
    payload = {
        "features": [
            {
                "properties": {
                    "summary": {"distance": 1360.4, "duration": 960.0},
                    "segments": [
                        {"distance": 800.0, "duration": 500.0},
                        {"distance": 560.4, "duration": 460.0},
                    ],
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[13.40, 52.52], [13.41, 52.53], [9.99, 53.55]],
                },
            }
        ]
    }
    route = ors_mod._parse_directions(payload)
    assert route["distance_m"] == pytest.approx(1360.4)
    assert route["duration_s"] == pytest.approx(960.0)
    assert route["coordinates"][0] == [52.52, 13.40]
    assert route["coordinates"][-1] == [53.55, 9.99]
    assert len(route["legs"]) == 2
    assert route["legs"][0]["duration_s"] == pytest.approx(500.0)


def test_parse_directions_empty_features_raises():
    with pytest.raises(ors_mod.OrsError) as exc:
        ors_mod._parse_directions({"features": []})
    assert exc.value.status == 404


def test_lonlat_rejects_out_of_range():
    assert ors_mod._as_lonlat(91, 0) is None
    assert ors_mod._as_lonlat(52.52, 13.40) == [13.40, 52.52]


def test_geocode_without_key_errors(monkeypatch):
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    monkeypatch.setattr(ors_mod, "_config_api_key", lambda: "")
    with pytest.raises(ors_mod.OrsError) as exc:
        ors_mod.geocode("Berlin")
    assert exc.value.status == 503
    assert "ORS_API_KEY" in exc.value.message


def test_geocode_calls_search_and_returns_results(monkeypatch):
    monkeypatch.setattr(ors_mod, "_api_key", lambda: "test-key")
    payload = {"features": [_pelias_feature("Hamburg, Germany", 9.9937, 53.5511)]}

    def fake_urlopen(request, timeout=0, context=None):
        assert "geocode/search" in request.full_url
        assert request.get_header("Authorization") == "test-key"
        return _FakeResponse(payload)

    with patch.object(ors_mod, "urlopen", fake_urlopen):
        results = ors_mod.geocode("Hamburg")
    assert results[0]["lat"] == pytest.approx(53.5511)


def test_directions_rejects_unknown_profile(monkeypatch):
    monkeypatch.setattr(ors_mod, "_api_key", lambda: "test-key")
    with pytest.raises(ors_mod.OrsError) as exc:
        ors_mod.directions("cycling-regular", [[52.52, 13.40], [53.55, 9.99]])
    assert exc.value.status == 400


def test_directions_posts_lonlat_waypoints(monkeypatch):
    monkeypatch.setattr(ors_mod, "_api_key", lambda: "test-key")
    captured: dict = {}

    def fake_urlopen(request, timeout=0, context=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "features": [
                    {
                        "properties": {
                            "summary": {"distance": 100.0, "duration": 20.0},
                            "segments": [{"distance": 100.0, "duration": 20.0}],
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[13.40, 52.52], [9.99, 53.55]],
                        },
                    }
                ]
            }
        )

    with patch.object(ors_mod, "urlopen", fake_urlopen):
        route = ors_mod.directions("driving-car", [[52.52, 13.40], [53.55, 9.99]])

    assert "directions/driving-car/geojson" in captured["url"]
    assert captured["body"]["coordinates"] == [[13.40, 52.52], [9.99, 53.55]]
    assert captured["body"]["instructions"] is True
    assert route["coordinates"][0] == [52.52, 13.40]
    assert route["legs"][0]["duration_s"] == pytest.approx(20.0)


def test_ors_tools_register():
    registry = ToolRegistry()
    count = __import__("agentforge.tools.ors_tools", fromlist=["register_ors_tools"]).register_ors_tools(registry)
    names = registry.list_tools()
    assert count == 4
    assert "ors_geocode" in names
    assert "ors_reverse" in names
    assert "ors_route" in names
    assert "ors_pois" in names


def test_ors_geocode_tool_returns_json(monkeypatch):
    from agentforge.tools import ors_tools

    monkeypatch.setattr(
        ors_tools.ors,
        "geocode",
        lambda text, lat=None, lon=None: [{"label": text, "lat": 52.0, "lon": 4.0}],
    )
    raw = ors_tools.ors_geocode("Berlin")
    payload = json.loads(raw)
    assert payload["results"][0]["label"] == "Berlin"


def test_geocode_ranks_matching_label_ahead_of_street_number_collision(monkeypatch):
    monkeypatch.setattr(ors_mod, "_api_key", lambda: "test-key")
    payload = {
        "features": [
            _pelias_feature("Hauptstraße 42, Zillingdorf, NO, Austria", 16.32, 47.85),
            _pelias_feature("Keizersgracht 42, Amsterdam, NH, Netherlands", 4.8876, 52.3721),
        ]
    }

    with patch.object(ors_mod, "urlopen", lambda *a, **k: _FakeResponse(payload)):
        results = ors_mod.geocode("Keizersgracht 42, Amsterdam, Netherlands")
    assert "Amsterdam" in results[0]["label"]
    assert results[0]["lat"] == pytest.approx(52.3721)


def test_geocode_does_not_pin_country(monkeypatch):
    monkeypatch.setattr(ors_mod, "_api_key", lambda: "test-key")
    seen = {}

    def fake_urlopen(request, timeout=0, context=None):
        seen["url"] = request.full_url
        return _FakeResponse({"features": [_pelias_feature("x", 4.3, 52.0)]})

    with patch.object(ors_mod, "urlopen", fake_urlopen):
        ors_mod.geocode("Keizersgracht 42, Amsterdam, Netherlands")
    assert "boundary.country" not in seen["url"]


def test_ors_html_502_is_rewritten():
    msg = ors_mod._ors_message(502, "<html><head><title>502 Bad Gateway</title></head></html>")
    assert "502" in msg
    assert "<html" not in msg.lower()


def test_request_retries_http_502(monkeypatch):
    monkeypatch.setattr(ors_mod, "_api_key", lambda: "test-key")
    attempts = {"n": 0}

    def fake_urlopen(request, timeout=0, context=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise HTTPError(
                request.full_url,
                502,
                "Bad Gateway",
                Message(),
                BytesIO(b"<html>502</html>"),
            )
        return _FakeResponse({"features": [_pelias_feature("Hamburg, Germany", 9.99, 53.55)]})

    monkeypatch.setattr(ors_mod, "_sleep", lambda *_a, **_k: None)
    with patch.object(ors_mod, "urlopen", fake_urlopen):
        results = ors_mod.geocode("Hamburg")
    assert attempts["n"] == 3
    assert results[0]["lat"] == pytest.approx(53.55)


def test_ors_route_tool_parses_object_coordinates(monkeypatch):
    from agentforge.tools import ors_tools

    def fake_directions(profile, coordinates):
        assert coordinates == [[52.52, 13.40], [53.55, 9.99]]
        return {"distance_m": 1.0, "duration_s": 2.0, "coordinates": coordinates, "legs": []}

    monkeypatch.setattr(ors_tools.ors, "directions", fake_directions)
    raw = ors_tools.ors_route(
        profile="driving-car",
        coordinates='[{"lat": 52.52, "lon": 13.40}, {"lat": 53.55, "lon": 9.99}]',
    )
    assert json.loads(raw)["distance_m"] == 1.0


def test_ors_route_tool_parses_json_coordinates(monkeypatch):
    from agentforge.tools import ors_tools

    def fake_directions(profile, coordinates):
        assert profile == "driving-car"
        assert coordinates == [[52.52, 13.40], [53.55, 9.99]]
        return {"distance_m": 1.0, "duration_s": 2.0, "coordinates": coordinates, "legs": []}

    monkeypatch.setattr(ors_tools.ors, "directions", fake_directions)
    raw = ors_tools.ors_route(
        profile="driving-car",
        coordinates="[[52.52, 13.40], [53.55, 9.99]]",
    )
    payload = json.loads(raw)
    assert payload["distance_m"] == 1.0
