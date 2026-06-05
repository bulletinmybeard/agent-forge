"""SAQ queue helpers — one shared queue plus one per-role tools queue.

The shared queue (``agentforge:shared``) handles agent jobs, monitor checks, and
scheduled commands. Tool-dispatch jobs land on a dedicated per-role queue
whose name is read from ``tool_routing.yaml``.

Adding a new worker host:

    1. Add a ``role`` block (with ``queue:``) in ``tool_routing.yaml``.
    2. Set ``AGENTFORGE_WORKER_ROLE=<name>`` on that host.
    3. Run ``saq -v web.server.queue.settings_tools.settings`` there.

Redis connection tuning mirrors the existing ``config.py`` so the local <-> remote
Wi-Fi link behaves the same under both queues.
"""

from __future__ import annotations

import logging
import os

import redis.asyncio as aioredis
from saq.queue.redis import RedisQueue

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
QUEUE_NAME = "agentforge:shared"

_REDIS_KWARGS = {
    "socket_timeout": 30,
    "socket_connect_timeout": 10,
    "retry_on_timeout": True,
    "health_check_interval": 15,
    "socket_keepalive": True,
}


def _build_client() -> aioredis.Redis:
    return aioredis.from_url(REDIS_URL, **_REDIS_KWARGS)


def get_queue() -> RedisQueue:
    """Build a fresh SAQ queue bound to a new aioredis client.

    No module-level singleton: SAQ workers each create their own queue at
    startup; FastAPI handlers and APScheduler create short-lived ones per call.
    """
    return RedisQueue(_build_client(), name=QUEUE_NAME)


def get_tool_queue_for_role(role: str) -> RedisQueue:
    """Return the SAQ tool queue whose consumer can execute *role* tools.

    The role -> queue mapping comes from ``tool_routing.yaml``.
    """
    from agentforge.tools.routing import get_queue_for_role

    queue_name = get_queue_for_role(role)
    return RedisQueue(_build_client(), name=queue_name)
