"""Service probes — Docker containers + Ollama + Redis health.

Designed to degrade gracefully:
  * If the Docker socket isn't mounted, the container probes fail cleanly
    and return an ``error`` entry rather than raising.
  * If Ollama or Redis are unreachable, their entry shows ``state: down``.

The only hard dep is Docker SDK; everything else uses stdlib / redis-py /
httpx (all already in project deps).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

# The Docker socket path — standard location when mounted into the container
# via ``/var/run/docker.sock:/var/run/docker.sock:ro``.
_DOCKER_SOCKET = "/var/run/docker.sock"

_docker_client = None
_docker_last_error: str | None = None


def _get_docker_client():
    """Lazily build a Docker SDK client. Returns ``None`` when unavailable.

    Keeps the client around between requests since re-opening the unix
    socket each call adds meaningful overhead to a 10-second polling loop.

    Check order matters: the socket-exists check runs BEFORE ``import docker``
    so that a local dev env without the SDK installed still reports a
    meaningful "socket not mounted" error rather than the generic
    ImportError. Production always has both the socket and the SDK.
    """
    global _docker_client, _docker_last_error
    if _docker_client is not None:
        return _docker_client
    if not os.path.exists(_DOCKER_SOCKET):
        _docker_last_error = (
            f"Docker socket not found at {_DOCKER_SOCKET}. Mount it "
            f"via docker-compose.remote.yml: "
            f"`/var/run/docker.sock:/var/run/docker.sock:ro`."
        )
        logger.warning("services.probe: %s", _docker_last_error)
        return None
    try:
        import docker  # type: ignore

        client = docker.DockerClient(base_url=f"unix://{_DOCKER_SOCKET}")
        client.ping()
        _docker_client = client
        _docker_last_error = None
        logger.info("services.probe: Docker client connected via %s", _DOCKER_SOCKET)
        return client
    except Exception as exc:  # noqa: BLE001
        _docker_last_error = f"Docker client init failed: {exc}"
        logger.warning("services.probe: %s", _docker_last_error)
        return None


def _format_uptime_seconds(started_at_iso: str | None) -> int:
    """Return seconds elapsed since ``started_at_iso``, or 0 when unparseable."""
    if not started_at_iso:
        return 0
    s = started_at_iso
    # Docker returns "2026-04-23T07:38:12.123456789Z" — too many fractional
    # digits for fromisoformat on Python <3.11; truncate to microseconds.
    if "." in s:
        head, _, frac_tz = s.partition(".")
        # frac_tz = "123456789Z" → keep 6 digits then whatever trails (Z or tz offset).
        frac = ""
        rest = frac_tz
        for ch in frac_tz:
            if ch.isdigit() and len(frac) < 6:
                frac += ch
            else:
                break
        rest = frac_tz[len(frac) :]
        s = f"{head}.{frac}{rest}"
    s = s.replace("Z", "+00:00")
    try:
        started = datetime.fromisoformat(s)
    except ValueError:
        return 0
    now = datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    delta = (now - started).total_seconds()
    return max(0, int(delta))


def probe_docker_containers() -> list[dict[str, Any]]:
    """Return one entry per running / recently-stopped Docker container.

    Each entry:
        {
          "name":        str,   # e.g., "agentforge-agentforge-web-1"
          "service":     str,   # compose service name, e.g., "agentforge-web"
          "image":       str,
          "state":       str,   # running | exited | created | paused | restarting
          "status":      str,   # human-readable (e.g., "Up 14 minutes (healthy)")
          "health":      str,   # healthy | unhealthy | starting | none
          "uptime_s":    int,
          "ports":       list[str],
          "host":        "remote",
          "type":        "container",
        }
    """
    client = _get_docker_client()
    if client is None:
        return []
    try:
        containers = client.containers.list(all=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("services.probe: containers.list failed: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for c in containers:
        try:
            attrs = c.attrs or {}
            state = attrs.get("State", {}) or {}
            config = attrs.get("Config", {}) or {}
            labels = config.get("Labels") or {}
            health = (state.get("Health", {}) or {}).get("Status", "none")
            started_at = state.get("StartedAt")
            ports = []
            port_bindings = (attrs.get("NetworkSettings", {}) or {}).get("Ports") or {}
            for container_port, bindings in port_bindings.items():
                if not bindings:
                    continue
                for b in bindings:
                    host_port = (b or {}).get("HostPort")
                    if host_port:
                        ports.append(f"{host_port}:{container_port.split('/')[0]}")
            out.append(
                {
                    "id": c.id[:12] if c.id else "",
                    "name": c.name,
                    "service": labels.get("com.docker.compose.service", c.name),
                    "image": (c.image.tags[0] if c.image and c.image.tags else (c.attrs.get("Image") or "")),
                    "state": state.get("Status") or c.status or "unknown",
                    "status": c.attrs.get("State", {}).get("Status", "") or c.status or "",
                    "health": health or "none",
                    "uptime_s": _format_uptime_seconds(started_at) if state.get("Running") else 0,
                    "ports": ports,
                    "host": "remote",
                    "type": "container",
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("services.probe: container inspect failed: %s", exc)
    # Sort by service name for stable UI ordering.
    out.sort(key=lambda e: e["service"])
    return out


def restart_container(name: str) -> dict[str, Any]:
    """Restart a container by name or id.

    Returns ``{"ok": bool, "name": str, "elapsed_ms": int, "error": str | None}``.
    """
    client = _get_docker_client()
    if client is None:
        return {
            "ok": False,
            "name": name,
            "elapsed_ms": 0,
            "error": _docker_last_error or "Docker client unavailable.",
        }
    start = time.perf_counter()
    try:
        c = client.containers.get(name)
        c.restart(timeout=10)
        return {
            "ok": True,
            "name": name,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("services.probe: restart %s failed: %s", name, exc)
        return {
            "ok": False,
            "name": name,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
            "error": str(exc),
        }


def stream_container_logs(name: str, tail: int = 50):
    """Return a blocking iterator yielding raw log lines (``bytes``).

    The SSE endpoint wraps this in a producer thread so the yielding doesn't
    block the event loop. Caller is responsible for closing the underlying
    Docker stream when they're done (``.close()`` on the returned object).
    """
    client = _get_docker_client()
    if client is None:
        raise RuntimeError(_docker_last_error or "Docker client unavailable.")
    c = client.containers.get(name)
    # stream=True + follow=True gives us a socket-backed generator that
    # emits each log line as soon as the container writes it.
    return c.logs(stream=True, follow=True, tail=max(0, int(tail)), timestamps=True, stdout=True, stderr=True)


def fetch_container_logs(name: str, lines: int = 200) -> dict[str, Any]:
    """Return tail-N logs for a container by name or id.

    Response:
        {"name": str, "lines": int, "logs": str, "error": str | None}
    """
    client = _get_docker_client()
    if client is None:
        return {
            "name": name,
            "lines": 0,
            "logs": "",
            "error": _docker_last_error or "Docker client unavailable.",
        }
    try:
        c = client.containers.get(name)
        raw = c.logs(tail=max(1, min(int(lines), 2000)), timestamps=True, stdout=True, stderr=True)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return {"name": name, "lines": text.count("\n"), "logs": text, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "lines": 0, "logs": "", "error": str(exc)}


# ---------------------------------------------------------------------------
# Ollama (HTTP health probe to host)
# ---------------------------------------------------------------------------


def probe_ollama(url: str | None = None) -> dict[str, Any]:
    """Ping Ollama's ``/api/tags`` endpoint. Returns a normalised entry."""
    base = (url or os.environ.get("OLLAMA_HOST") or "http://host.docker.internal:11434").rstrip("/")
    start = time.perf_counter()
    try:
        import httpx

        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{base}/api/tags")
            resp.raise_for_status()
            tags = (resp.json() or {}).get("models") or []
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "ollama",
            "service": "ollama",
            "image": "",
            "state": "running",
            "status": f"ok ({len(tags)} models, {latency_ms}ms)",
            "health": "healthy",
            "uptime_s": None,  # not exposed
            "ports": ["11434"],
            "host": "remote",
            "type": "host_service",
            "endpoint": base,
        }
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "ollama",
            "service": "ollama",
            "image": "",
            "state": "down",
            "status": f"error: {exc}",
            "health": "unhealthy",
            "uptime_s": None,
            "ports": ["11434"],
            "host": "remote",
            "type": "host_service",
            "endpoint": base,
            "error": str(exc),
            "latency_ms": latency_ms,
        }


# ---------------------------------------------------------------------------
# Redis (direct ping)
# ---------------------------------------------------------------------------


def probe_redis(url: str | None = None) -> dict[str, Any]:
    """PING redis and return a normalised entry."""
    target = url or os.environ.get("REDIS_URL") or "redis://host.docker.internal:6379"
    start = time.perf_counter()
    try:
        import redis

        client = redis.from_url(target, socket_timeout=3.0)
        pong = client.ping()
        info = client.info("server") or {}
        uptime_s = int(info.get("uptime_in_seconds") or 0)
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "name": "redis",
            "service": "redis",
            "image": f"redis {info.get('redis_version', '?')}",
            "state": "running" if pong else "down",
            "status": f"pong ({latency_ms}ms)" if pong else "no response",
            "health": "healthy" if pong else "unhealthy",
            "uptime_s": uptime_s,
            "ports": ["6379"],
            "host": "remote",
            "type": "host_service",
            "endpoint": target,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "redis",
            "service": "redis",
            "image": "",
            "state": "down",
            "status": f"error: {exc}",
            "health": "unhealthy",
            "uptime_s": None,
            "ports": ["6379"],
            "host": "remote",
            "type": "host_service",
            "endpoint": target,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Aggregate probe
# ---------------------------------------------------------------------------


def probe_all() -> dict[str, Any]:
    """Single call used by ``GET /api/services``."""
    containers = probe_docker_containers()
    ollama = probe_ollama()
    redis_entry = probe_redis()
    return {
        "services": [*containers, ollama, redis_entry],
        "docker_available": _get_docker_client() is not None,
        "docker_error": _docker_last_error,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
