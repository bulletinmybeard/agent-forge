import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.routes.health import router as health_router
from app.routes.indexer import router as indexer_router
from app.routes.search import router as search_router

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Noise suppression
# ---------------------------------------------------------------------------
# 1. Uvicorn access log: drop health-check requests (Docker healthcheck fires
#    every 15 s and generates 4 log lines per cycle across both containers).
# 2. httpx/httpcore: drop per-request INFO lines for internal Qdrant calls
#    — the Qdrant health ping produces "GET /collections 200 OK" every 15 s.


class _QuietAccessFilter(logging.Filter):
    _SKIP = frozenset(["/health"])

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(s in record.getMessage() for s in self._SKIP)


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage background services that run alongside FastAPI."""
    tasks: list[asyncio.Task] = []

    # Ensure the KB collection exists (empty is fine) so /search and the chat
    # RAG path don't 404 on a fresh deployment. Idempotent + guarded.
    try:
        from app.services.vector_service import vector_service

        vector_service.ensure_collection()
    except Exception as e:
        logger.warning("Could not ensure KB collection on startup: %s", e)

    # Start the Slack bot if enabled
    if settings.slack.enabled:
        try:
            from app.slack.bot import start_background

            task = asyncio.create_task(start_background())
            tasks.append(task)
            logger.info("Slack bot starting in background")
        except ImportError as e:
            logger.error("Slack bot enabled but dependencies missing: %s", e)

    yield

    # Shutdown: cancel background tasks
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if tasks:
        logger.info("Background tasks stopped")


app = FastAPI(
    title="agentforge",
    version="0.1.0",
    description="AgentForge knowledge indexing and search service",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(indexer_router)
app.include_router(search_router)

# Optional API-key auth (off unless security.api_keys is set). Exempts /health.
# enforce_auth_policy aborts the boot if we're in a dangerous unauthenticated
# config (Docker socket mounted, or AGENTFORGE_REQUIRE_AUTH set) with no keys.
from app.security import enforce_auth_policy, install_api_key_auth

enforce_auth_policy("RAG API")
install_api_key_auth(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)
