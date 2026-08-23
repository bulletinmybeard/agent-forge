from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

_STATIC = Path(__file__).resolve().parent / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def render_trip(trip: dict[str, Any], api_base: str = "") -> str:
    title = str(trip.get("title") or "Trip")
    payload = json.dumps(trip, ensure_ascii=False).replace("<", "\\u003c")
    html = _read("trip.html")
    html = html.replace("__TITLE__", escape(title))
    html = html.replace("__CSS__", _read("trip.css"))
    html = html.replace("__JS__", _read("trip.js"))
    html = html.replace("__TRIP_JSON__", payload)
    html = html.replace("__API_BASE__", json.dumps(api_base))
    return html
