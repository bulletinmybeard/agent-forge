from __future__ import annotations

import json
import ssl
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from agentforge.trips import service
from agentforge.trips.ors import OrsError

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(exc: Exception) -> str:
    if isinstance(exc, OrsError):
        return _dumps({"error": exc.message, "status": exc.status})
    return _dumps({"error": str(exc)})


_WIKI_UA = "agentforge-trip/0.1"
_WIKI_TIMEOUT = 12


def _wiki_summary(name: str, lang: str) -> dict | None:
    slug = quote(name.strip().replace(" ", "_"), safe="_()")
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{slug}"
    request = Request(url, headers={"User-Agent": _WIKI_UA, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=_WIKI_TIMEOUT, context=ssl.create_default_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError, TimeoutError):
        return None
    return payload if isinstance(payload, dict) else None


@tool(locality="remote")
def wiki_place_image(name: str, lang: str = "de") -> str:
    """Return a real Wikimedia thumbnail URL for a named place.

    When to use: Before putting an image in the itinerary markdown or
        trip_publish image_url. Never invent a fileadmin/_processed_ URL.
    When NOT to use: You already have a URL that appeared in a web_fetch result.
    Input: name - place title (e.g. "Rheinturm", "Königsallee"). lang - Wikipedia
        language code, default de; falls back to en.
    Output: JSON {title, image_url, source} or {error} if there is no thumbnail.
    """
    query = (name or "").strip()
    if len(query) < 2:
        return _dumps({"error": "name is required."})
    langs = []
    for code in (lang.strip() or "de", "de", "en"):
        if code and code not in langs:
            langs.append(code)
    for code in langs:
        payload = _wiki_summary(query, code)
        if not payload:
            continue
        thumb = payload.get("thumbnail") or {}
        image_url = str(thumb.get("source") or "").strip()
        if image_url.startswith("https://"):
            return _dumps(
                {
                    "title": payload.get("title") or query,
                    "image_url": image_url,
                    "source": f"{code}.wikipedia.org",
                }
            )
    return _dumps({"error": f"No Wikipedia thumbnail for {query!r}."})


@tool(locality="remote")
def trip_publish(trip_json: str, trip_id: str = "") -> str:
    """Save a planned trip and return the interactive map URL.

    When to use: After geocoding, routing, and picking stops. The map page is
        generated from this JSON — do not write HTML yourself.
    Input: trip_json - JSON object with title, profile (driving-car or
        foot-walking), departure (ISO-8601 with offset), origin {id,label,lat,lon},
        optional destination, stops [{id,label,lat,lon,optional,enabled,kind,
        dwell_min,opens,closes,notes,image_url}], optional itinerary_md and
        route from ors_route. trip_id - reuse an existing UUID to update.
    Output: JSON {id, url, trip}. Put the url in the final markdown as
        [Open interactive map](url).
    """
    try:
        raw = json.loads(trip_json) if isinstance(trip_json, str) else trip_json
        if not isinstance(raw, dict):
            raise ValueError("trip_json must be a JSON object.")
        ident = trip_id.strip() or None
        result = service.publish(raw, trip_id=ident, compute_route=True)
        payload = {"id": result["id"], "url": result["url"]}
        if result.get("route_error"):
            payload["route_error"] = result["route_error"]
        if result.get("rejected_stops"):
            payload["rejected_stops"] = result["rejected_stops"]
        return _dumps(payload)
    except Exception as exc:
        return _error(exc)


@tool(locality="remote")
def trip_get(trip_id: str) -> str:
    """Load a previously published trip JSON.

    When to use: Follow-ups like "drop the restaurant" or "leave later".
    Input: trip_id - UUID from trip_publish.
    Output: JSON trip object, or {error}.
    """
    try:
        trip = service.load(trip_id.strip())
        if trip is None:
            return _dumps({"error": "Trip not found."})
        return _dumps(trip)
    except Exception as exc:
        return _error(exc)


def register_trip_tools(registry: ToolRegistry) -> int:
    registry.register_category_hint(
        "Trip",
        "Persist a trip plan as an interactive map. Call trip_publish with "
        "structured JSON after ors_route; include the returned url in the answer. "
        "Use trip_get to edit an existing trip on follow-up. "
        "wiki_place_image returns a real Wikimedia URL — never invent image links.",
    )
    tools = [wiki_place_image, trip_publish, trip_get]
    for func in tools:
        registry.register(func, category="Trip")
        logger.debug("Registered trip tool: %s", func.__name__)
    return len(tools)
