from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agentforge.trips import service
from agentforge.trips.models import is_trip_id
from agentforge.trips.ors import OrsError
from agentforge.trips.renderer import render_trip
from agentforge.trips.store import TripStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trips"])


class RouteRequest(BaseModel):
    enabled_stop_ids: list[str] = []


class PatchRequest(BaseModel):
    enabled_stop_ids: list[str] | None = None
    trip: dict | None = None


def init_trips(root: Path) -> None:
    service.set_store(TripStore(root))
    logger.info("Trip store at %s", root)


def _trip_id(raw: str) -> str:
    ident = raw[:-5] if raw.endswith(".html") else raw
    if not is_trip_id(ident):
        raise HTTPException(status_code=404, detail="Trip not found")
    return ident


def _load(trip_id: str) -> dict:
    trip = service.load(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return service.hydrate(trip)


@router.get("/trips/{trip_id}", response_class=HTMLResponse)
async def trip_page(trip_id: str):
    ident = _trip_id(trip_id)
    trip = _load(ident)
    html = render_trip(trip, api_base="")
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/trips/{trip_id}")
async def trip_json(trip_id: str):
    return _load(_trip_id(trip_id))


@router.post("/api/trips/{trip_id}/route")
async def trip_reroute(trip_id: str, body: RouteRequest):
    ident = _trip_id(trip_id)
    try:
        return service.reroute(ident, body.enabled_stop_ids)
    except KeyError:
        raise HTTPException(status_code=404, detail="Trip not found") from None
    except OrsError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/trips/{trip_id}")
async def trip_patch(trip_id: str, body: PatchRequest):
    ident = _trip_id(trip_id)
    if body.trip is not None:
        try:
            result = service.publish(body.trip, trip_id=ident, compute_route=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OrsError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        return result["trip"]
    try:
        return service.reroute(ident, body.enabled_stop_ids)
    except KeyError:
        raise HTTPException(status_code=404, detail="Trip not found") from None
    except OrsError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
