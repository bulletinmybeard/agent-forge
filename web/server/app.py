"""FastAPI application — AgentForge Chat web server.

Runs alongside (or integrated with) agentforge. Provides the React SPA
frontend and WebSocket backend that calls agentforge's search pipeline
directly (via Python imports, not HTTP).

Run standalone::

    cd agentforge
    uvicorn web.server.app:app --reload --port 8200

Or integrate into agentforge's existing app (see main.py).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# agentforge root — for resolving config-relative paths and app.* imports
SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.config import settings as af_settings
from app.security import enforce_auth_policy, install_api_key_auth, install_internal_auth

from . import api as api_module
from . import ws_endpoint
from .api import internal as internal_router
from .api import router as api_router
from .api import set_database as set_api_database
from .api import set_upload_config
from .api_memory import router as memory_router
from .botty_endpoint import router as botty_router
from .botty_endpoint import set_database as set_botty_database
from .canvas.api import init_canvas_api
from .canvas.api import router as canvas_api_router
from .canvas.database import CanvasDatabase
from .catalog_api import router as catalog_api_router
from .config import settings
from .configs.api import router as configs_api_router
from .connectors.api import router as connectors_api_router
from .database import ChatDatabase
from .model_catalog.api import router as model_catalog_router
from .monitor_service import init_monitor, shutdown_monitor
from .prompt_lab.database.manager import PromptLabDatabase
from .scheduler_service import init_scheduler, shutdown_scheduler
from .services.api import router as services_api_router
from .ws_endpoint import init_runtime
from .ws_endpoint import router as ws_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Uvicorn access log noise filter
# ---------------------------------------------------------------------------
# Health checks (Docker healthcheck, Vite proxy keep-alive) and the sidebar's
# session-list poll fire every few seconds and carry no diagnostic value.
# Drop them from the access log so real traffic remains visible.


class _QuietAccessFilter(logging.Filter):
    """Suppress repetitive access-log entries for polling/health endpoints."""

    _SKIP = frozenset(
        [
            "/api/health",
            "/api/sessions",
            "/api/knowledge",  # knowledge bar polls on mount
            "GET /health",  # plain /health variants
        ]
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(skip in msg for skip in self._SKIP)


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())

# Path to the built React app (created by `npm run build` in client/)
CLIENT_DIST = Path(__file__).parent.parent / "client" / "dist"


def _init_database() -> ChatDatabase:
    """Initialise the chat database for web GUI sessions.

    Uses a dedicated SQLite database under agentforge/data/ so it
    doesn't interfere with agentforge's own data stores.
    """
    # Ensure agentforge's app package is importable
    if str(SERVICE_ROOT) not in sys.path:
        sys.path.insert(0, str(SERVICE_ROOT))

    # Read web-specific DB path from agentforge config.yaml
    config_path = SERVICE_ROOT / "config.yaml"
    db_path = "data/web_chat.db"  # default

    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        web_cfg = cfg.get("web", {})
        db_path = web_cfg.get("database_path", db_path)

    db_full_path = SERVICE_ROOT / db_path
    db = ChatDatabase(db_full_path)
    db.create_tables()
    return db


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AgentForge Chat server starting on %s:%s", settings.host, settings.port)

    # Initialise database
    db = _init_database()
    set_api_database(db)

    # Configure file uploads
    config_path = SERVICE_ROOT / "config.yaml"
    upload_path = SERVICE_ROOT / "data" / "uploads"
    max_size = 75
    max_files = 25

    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        web_cfg = cfg.get("web", {})
        upload_path = SERVICE_ROOT / web_cfg.get("uploads_path", "data/uploads")
        max_size = web_cfg.get("max_file_size_mb", max_size)
        max_files = web_cfg.get("max_files_per_request", max_files)

    set_upload_config(upload_path, max_size_mb=int(max_size), max_files=int(max_files))

    # Serve uploaded files (images need to be accessible for thumbnails)
    upload_path.mkdir(parents=True, exist_ok=True)
    _uploads_base = upload_path.resolve()

    @app.get("/uploads/{filepath:path}")
    async def _serve_upload(filepath: str):
        candidate = (_uploads_base / filepath).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(_uploads_base):
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(candidate, headers={"X-Content-Type-Options": "nosniff"})

    # Make database available to WebSocket handler
    ws_endpoint.set_database(db)

    # Initialise the search runtime in a background thread so startup is instant.
    # The WebSocket handler waits for the ready-gate before accepting queries.
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, init_runtime)
    logger.info("SearchRuntime initialising in background thread…")

    # Initialise the scheduler service (APScheduler)
    try:
        init_scheduler(db)
        logger.info("SchedulerService started")
    except Exception as exc:
        logger.warning("SchedulerService failed to start: %s — scheduler mode will be unavailable", exc)

    # Initialise the monitor service (APScheduler for website change monitoring)
    try:
        init_monitor(db)
        logger.info("MonitorService started")
    except Exception as exc:
        logger.warning("MonitorService failed to start: %s — monitor mode will be unavailable", exc)

    # Initialise Prompt Lab (separate DB — persists /api/prompt-lab/* runs)
    if af_settings.prompt_lab.enabled:
        try:
            prompt_lab_db_path = SERVICE_ROOT / "data" / "prompt_lab.db"
            prompt_lab_db = PromptLabDatabase(prompt_lab_db_path)
            prompt_lab_db.create_tables()
            api_module.init_prompt_lab_db(prompt_lab_db)
            logger.info("Prompt Lab initialised (db: %s)", prompt_lab_db_path)
        except Exception as exc:
            logger.warning("Prompt Lab failed to initialise: %s — history will be unavailable", exc)
    else:
        logger.info("Prompt Lab disabled (prompt_lab.enabled=false)")

    # Initialise Canvas (shares the same SQLite file as chat messages)
    if af_settings.canvas.enabled:
        try:
            canvas_db = CanvasDatabase(db.db_path)
            canvas_db.create_tables()
            init_canvas_api(canvas_db)
            ws_endpoint.set_canvas_database(canvas_db)
            logger.info("Canvas initialised (db: %s)", db.db_path)
        except Exception as exc:
            logger.warning("Canvas failed to initialise: %s — canvas features will be unavailable", exc)
    else:
        logger.info("Canvas disabled (canvas.enabled=false)")

    # Initialise Botty — Session Awareness Layer
    if af_settings.botty.enabled:
        try:
            set_botty_database(db)
            logger.info("Botty initialised")
        except Exception as exc:
            logger.warning("Botty failed to initialise: %s — /ws/botty will be unavailable", exc)
    else:
        logger.info("Botty disabled (botty.enabled=false)")

    yield

    # Shutdown monitor before closing
    try:
        shutdown_monitor()
    except Exception:
        pass

    # Shutdown scheduler before closing
    try:
        shutdown_scheduler()
    except Exception:
        pass
    logger.info("AgentForge Chat server shutting down")


app = FastAPI(title="AgentForge Chat", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Global exception handler — catches anything that escapes route handlers.
# FastAPI's default 500 handler logs nothing useful; this ensures the full
# traceback + request context always appears in the application log.
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception: %s %s — %s",
        request.method,
        request.url.path,
        exc,
    )
    # Full detail is in the log above; don't leak exception internals to clients.
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Request timing middleware — logs all 4xx/5xx responses with duration so
# slow or failing requests are always visible in the log regardless of which
# route handled them.
# ---------------------------------------------------------------------------


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
        duration = time.perf_counter() - start
        if response.status_code >= 400:
            logger.warning(
                "HTTP %d  %s %s  (%.2fs)",
                response.status_code,
                request.method,
                request.url.path,
                duration,
            )
        return response
    except Exception:
        duration = time.perf_counter() - start
        logger.exception(
            "Request crashed: %s %s  (%.2fs)",
            request.method,
            request.url.path,
            duration,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )


# CORS — allow the Vite dev server during development.
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
# Wildcard origin + credentials is rejected by browsers anyway and would be a
# footgun if it weren't — only allow credentials with an explicit origin list.
_cors_allow_credentials = "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional API-key auth (off unless security.api_keys is set). Guards HTTP
# routes except /health + /internal; the WebSocket is checked in ws_endpoint.
# enforce_auth_policy aborts the boot if the Docker socket is mounted (or
# AGENTFORGE_REQUIRE_AUTH is set) with no keys — this surface drives the agent.
enforce_auth_policy("agent web")
install_api_key_auth(app)
# Shared-secret gate on /internal/* (worker->web callbacks); no-op unless
# AGENTFORGE_INTERNAL_TOKEN is set.
install_internal_auth(app)

# WebSocket endpoint
app.include_router(ws_router)

# REST API endpoints
app.include_router(api_router)

# Internal endpoints — called by the native worker (not the browser)
app.include_router(internal_router)

# Memory Settings — list / delete facts + conversation memory entries
app.include_router(memory_router)

app.include_router(services_api_router)

# Configs viewer — read-only YAML inspection (verifies deployment landed)
app.include_router(configs_api_router)

# Canvas endpoints — gated by canvas.enabled in config.yaml
if af_settings.canvas.enabled:
    app.include_router(canvas_api_router)

# Catalog API — unified model-metadata across LLM providers (Redis-cached)
app.include_router(catalog_api_router)

# Model Catalog UI backend — equivalence finder + (later) search + pull
app.include_router(model_catalog_router)

# Connectors — external service integrations (Gmail, Drive, etc.)
app.include_router(connectors_api_router)

# Botty — Session Awareness Layer (WebSocket); gated by botty.enabled in config.yaml
if af_settings.botty.enabled:
    app.include_router(botty_router)


# Health check
@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "agentforge-web"}


# In production: serve the built React app as static files.
# This must be registered LAST so it doesn't shadow API/WS routes.
if CLIENT_DIST.is_dir():
    _SPA_INDEX = CLIENT_DIST / "index.html"

    # Serve static assets (JS, CSS, images) from dist/
    app.mount("/assets", StaticFiles(directory=str(CLIENT_DIST / "assets")), name="spa-assets")

    # SPA catch-all: any non-API/non-WS path serves index.html so
    # client-side routing (React Router) handles it.
    _DIST_ROOT = CLIENT_DIST.resolve()

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str):
        # Serve actual files from dist/ if they exist (favicon, etc.). Resolve and
        # confine to dist/ so a crafted path like `../../etc/passwd` can't escape.
        candidate = (CLIENT_DIST / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(_DIST_ROOT):
            return FileResponse(candidate)
        return FileResponse(_SPA_INDEX)


def main() -> None:
    """Entry point for ``python -m web.server.app``."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
    uvicorn.run(
        "web.server.app:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        timeout_keep_alive=120,  # keep HTTP connections alive for up to 120s (default: 5s)
    )


if __name__ == "__main__":
    main()
