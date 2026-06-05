"""Services dashboard REST API — read-only probes, no write actions in v1.

Mounted under ``/api/services``. The frontend polls ``GET /api/services``
roughly every 10 seconds for the live grid, and requests
``GET /api/services/{name}/logs`` on demand when the user opens the
detail drawer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from . import probe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/services", tags=["services"])

# Self-restart guard: an HTTP restart of agentforge-web's own container would
# kill the process mid-response, leaving the UI hanging. Refuse up-front and
# point the user at the deploy script which handles rolling restarts.
# The running container's own name is available in hostname(5) inside Docker.
_SELF_CONTAINER_HOSTNAME = os.environ.get("HOSTNAME", "")


def _is_self(name: str) -> bool:
    """Return True if ``name`` refers to the agentforge-web container itself."""
    if not _SELF_CONTAINER_HOSTNAME:
        return False
    n = (name or "").lower()
    h = _SELF_CONTAINER_HOSTNAME.lower()
    # Docker container names may or may not include the compose project prefix.
    return n == h or n.endswith(f"-{h}") or h.startswith(n)


@router.get("")
def list_services() -> dict:
    """Return the current state of every known service + host-service probe."""
    return probe.probe_all()


@router.get("/{name}/logs")
def get_service_logs(name: str, lines: int = 200) -> dict:
    """Fetch tail-N logs for a Docker container (by name or id).

    ``lines`` is clamped to 1..2000. Only container logs are served — host
    services (ollama, redis) return 404 since they don't expose logs via
    this path.
    """
    if name in ("ollama", "redis"):
        raise HTTPException(
            status_code=404,
            detail=f"{name} is a host service — logs aren't exposed via this endpoint",
        )
    return probe.fetch_container_logs(name, lines=lines)


@router.post("/{name}/restart")
def restart_service(name: str) -> dict:
    """Restart a Docker container by name or id.

    Rejects:
      * host services (ollama, redis) — use systemd on the host instead.
      * agentforge-web itself — would kill the API mid-response; use the deploy
        script for rolling restarts.
    """
    if name in ("ollama", "redis"):
        raise HTTPException(
            status_code=400,
            detail=f"{name} is a host service — restart via systemd on the remote",
        )
    if _is_self(name) or "agentforge-web" in name.lower():
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to restart agentforge-web from the dashboard — it would "
                "kill this API mid-response. Use scripts/deploy-from-ally.sh "
                "(or docker compose restart agentforge-web on the host) instead."
            ),
        )
    result = probe.restart_container(name)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error") or "restart failed")
    return result


@router.get("/{name}/logs/stream")
async def stream_service_logs(name: str, tail: int = 50):
    """Server-Sent Events endpoint streaming a container's logs live.

    Wraps Docker SDK's blocking log iterator in a producer thread + async
    queue so we don't stall the event loop. Client closes the stream via
    ``EventSource.close()``; we detect the disconnect and stop the producer.
    """
    if name in ("ollama", "redis"):
        raise HTTPException(
            status_code=404,
            detail=f"{name} is a host service — live logs aren't exposed",
        )

    # Build the iterator up-front so HTTP errors surface as proper 4xx/5xx
    # rather than SSE-level errors the client can't distinguish.
    try:
        log_iter = probe.stream_container_logs(name, tail=tail)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()
    stop_event = threading.Event()

    def _produce():
        try:
            for chunk in log_iter:
                if stop_event.is_set():
                    break
                try:
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
                except RuntimeError:
                    # Event loop closed while we were running — give up quietly.
                    break
        except Exception as exc:  # noqa: BLE001
            err = str(exc).encode()
            try:
                loop.call_soon_threadsafe(q.put_nowait, err)
            except RuntimeError:
                pass
        finally:
            try:
                loop.call_soon_threadsafe(q.put_nowait, None)
            except RuntimeError:
                pass

    threading.Thread(target=_produce, daemon=True).start()

    async def _event_stream():
        try:
            # Send a heartbeat comment so the client knows the stream is alive
            # even during quiet periods. SSE comment syntax: "line starting with :".
            yield ": connected\n\n"
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if chunk is None:
                    break
                # Docker logs() with timestamps=True prefixes each line with an RFC3339
                # timestamp. Yield the raw line — the frontend prepends its own styling.
                text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
                for line in text.splitlines():
                    yield f"data: {line}\n\n"
        finally:
            stop_event.set()
            # Best-effort close on the Docker stream.
            try:
                log_iter.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable buffering on nginx / proxies
        },
    )
