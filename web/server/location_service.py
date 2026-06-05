"""Location resolution service for AgentForge.

Two resolution strategies:

  GPS   lat/lon → Nominatim (OSM) reverse geocoding
                → timezonefinder for IANA timezone (offline, no API)

  IP    client IP → DbIP-City-lite .mmdb (offline, bundled database)
                  → falls back gracefully if .mmdb not present

Both return a consistent LocationInfo dict:
  {city, country, timezone, lat, lon, local_time, source}
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DbIP .mmdb path — override via DBIP_MMDB_PATH env var
# ---------------------------------------------------------------------------

_MMDB_PATH = Path(
    os.environ.get("DBIP_MMDB_PATH", "") or Path(__file__).parent.parent.parent / "data" / "dbip-city-lite.mmdb"
)

# Lazy-loaded TimezoneFinder instance (heavy to create; reuse across requests)
_tz_finder = None


def _get_tz_finder():
    global _tz_finder
    if _tz_finder is None:
        try:
            from timezonefinder import TimezoneFinder

            _tz_finder = TimezoneFinder()
        except ImportError:
            logger.warning("timezonefinder not installed — timezone lookup disabled")
    return _tz_finder


def _format_local_time(tz_name: str) -> str:
    """Return the current local time string for the given IANA timezone."""
    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        return now.strftime("%A %d %B %Y, %H:%M")
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now().strftime("%A %d %B %Y, %H:%M")


def _tz_from_coords(lat: float, lon: float) -> str:
    """Return IANA timezone string for the given coordinates (offline)."""
    tf = _get_tz_finder()
    if tf is None:
        return "UTC"
    try:
        return tf.timezone_at(lat=lat, lng=lon) or "UTC"
    except Exception as exc:
        logger.debug("timezonefinder.timezone_at failed: %s", exc)
        return "UTC"


# ---------------------------------------------------------------------------
# GPS resolution — Nominatim reverse geocoding
# ---------------------------------------------------------------------------


async def resolve_from_coords(lat: float, lon: float) -> dict | None:
    """Reverse geocode (lat, lon) via Nominatim and derive timezone offline.

    Returns a LocationInfo dict or None on complete failure.
    """
    tz_name = _tz_from_coords(lat, lon)

    city = country = None
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&addressdetails=1"
        async with httpx.AsyncClient(
            timeout=5.0,
            headers={"User-Agent": "agentforge/1.0 (self-hosted knowledge assistant)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        address = data.get("address", {})
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )
        country = address.get("country")
    except Exception as exc:
        logger.warning("Nominatim reverse geocode failed for (%.4f, %.4f): %s", lat, lon, exc)

    return {
        "city": city,
        "country": country,
        "timezone": tz_name,
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "local_time": _format_local_time(tz_name),
        "source": "gps",
    }


# ---------------------------------------------------------------------------
# IP resolution — DbIP-City-lite .mmdb
# ---------------------------------------------------------------------------


def resolve_from_ip(ip: str) -> dict | None:
    """Resolve approximate location from an IP address using DbIP-City-lite.

    Returns None if the .mmdb file is not present or the lookup fails.
    Run ``scripts/download_dbip.sh`` once to install the database.
    """
    if not _MMDB_PATH.exists():
        logger.debug(
            "DbIP .mmdb not found at %s — skipping IP lookup. Run scripts/download_dbip.sh to install.",
            _MMDB_PATH,
        )
        return None

    try:
        import maxminddb
    except ImportError:
        logger.warning("maxminddb not installed — IP location lookup disabled")
        return None

    try:
        with maxminddb.open_database(str(_MMDB_PATH)) as reader:
            record = reader.get(ip)
    except Exception as exc:
        logger.warning("DbIP mmdb lookup failed for %s: %s", ip, exc)
        return None

    if not record:
        return None

    # DbIP-City-lite uses the same MaxMind GeoIP2 .mmdb schema
    city = (record.get("city") or {}).get("names", {}).get("en")
    country = (record.get("country") or {}).get("names", {}).get("en")
    loc = record.get("location") or {}
    tz_name = loc.get("time_zone") or "UTC"
    lat = loc.get("latitude")
    lon = loc.get("longitude")

    return {
        "city": city,
        "country": country,
        "timezone": tz_name,
        "lat": lat,
        "lon": lon,
        "local_time": _format_local_time(tz_name),
        "source": "ip",
    }
