from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentforge.trips.models import is_trip_id, validate_trip


class TripStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, trip_id: str) -> Path | None:
        if not is_trip_id(trip_id):
            return None
        path = (self.root / f"{trip_id}.json").resolve()
        if not path.is_relative_to(self.root.resolve()):
            return None
        return path

    def save(self, trip: dict[str, Any]) -> dict[str, Any]:
        clean = validate_trip(trip, trip_id=trip.get("id"))
        path = self._path(clean["id"])
        if path is None:
            raise ValueError("Invalid trip id.")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return clean

    def load(self, trip_id: str) -> dict[str, Any] | None:
        path = self._path(trip_id)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return validate_trip(payload, trip_id=trip_id)
        except ValueError:
            return None
