from __future__ import annotations

import json
from pathlib import Path

import yaml

from agentforge.tools import trip_tools
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.trip_tools import register_trip_tools
from agentforge.trips import models, service, store

ROOT = Path(__file__).resolve().parents[1]

TRIP_TOOLS = [
    "ors_geocode",
    "ors_reverse",
    "ors_route",
    "ors_pois",
    "trip_publish",
    "trip_get",
    "wiki_place_image",
    "web_search",
    "web_fetch",
    "web_fetch_rendered",
]


def _load_example_agents() -> dict:
    with open(ROOT / "custom_agents.example.yaml") as fh:
        return dict((yaml.safe_load(fh) or {}).get("agents", {}))


def test_ors_and_trip_tools_route_remote():
    with open(ROOT / "tool_routing.yaml") as fh:
        cfg = yaml.safe_load(fh)
    remote_tools: list[str] = []
    for rule in cfg.get("rules") or []:
        if rule.get("role") == "remote":
            remote_tools.extend(rule.get("tools") or [])
    for name in ("ors_geocode", "ors_route", "ors_pois", "trip_publish", "trip_get", "wiki_place_image"):
        assert name in remote_tools


def test_trip_agent_declared_in_example():
    agents = _load_example_agents()
    trip = agents.get("trip")
    assert isinstance(trip, dict)
    assert "@trip" in trip.get("aliases", [])
    assert "@tripplanner" in trip.get("aliases", [])
    tools = trip.get("tools")
    missing = [name for name in TRIP_TOOLS if name not in tools]
    assert not missing, f"@trip is missing tools: {missing}"
    prompt_path = ROOT / trip["system_prompt"]
    assert prompt_path.is_file()


def test_trip_publish_and_get(tmp_path: Path, monkeypatch):
    service.set_store(store.TripStore(tmp_path))
    registry = ToolRegistry()
    assert register_trip_tools(registry) == 3

    def fake_directions(profile, coordinates):
        return {
            "distance_m": 3.0,
            "duration_s": 4.0,
            "coordinates": coordinates,
            "legs": [{"distance_m": 3.0, "duration_s": 4.0}],
        }

    monkeypatch.setattr("agentforge.trips.service.ors.directions", fake_directions)
    raw = json.dumps(
        {
            "title": "Walk",
            "profile": "foot-walking",
            "origin": {"id": "a", "label": "A", "lat": 52.52, "lon": 13.40},
            "destination": {"id": "b", "label": "B", "lat": 52.53, "lon": 13.41},
        }
    )
    published = json.loads(trip_tools.trip_publish(raw))
    assert published["url"].startswith("/trips/")
    loaded = json.loads(trip_tools.trip_get(published["id"]))
    assert loaded["title"] == "Walk"
    assert models.is_trip_id(loaded["id"])


def test_wiki_place_image_returns_https_thumbnail(monkeypatch):
    monkeypatch.setattr(
        trip_tools,
        "_wiki_summary",
        lambda name, lang: {
            "title": "Rheinturm",
            "thumbnail": {"source": "https://upload.wikimedia.org/wikipedia/commons/r.jpg"},
        }
        if lang == "de"
        else None,
    )
    payload = json.loads(trip_tools.wiki_place_image("Rheinturm"))
    assert payload["image_url"].startswith("https://upload.wikimedia.org/")
    assert payload["source"] == "de.wikipedia.org"


def test_wiki_place_image_errors_without_thumbnail(monkeypatch):
    monkeypatch.setattr(trip_tools, "_wiki_summary", lambda name, lang: {"title": name})
    payload = json.loads(trip_tools.wiki_place_image("Not A Place"))
    assert "error" in payload
