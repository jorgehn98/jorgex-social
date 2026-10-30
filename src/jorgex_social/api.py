import logging
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from jorgex_social.access.deps import TokenDep
from jorgex_social.account.api import router as account_router
from jorgex_social.account.audit import AuditMiddleware
from jorgex_social.clock import Clock, SystemClock
from jorgex_social.config import Settings
from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.instagram import network_for
from jorgex_social.publishing.api import public_router as media_files_router
from jorgex_social.publishing.api import router as publishing_router
from jorgex_social.replies.api import router as replies_router
from jorgex_social.replies.intake import Intake
from jorgex_social.replies.webhook import router as webhook_router
from jorgex_social.stats.api import router as stats_router

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    docs = settings.simulation or settings.api_docs
    app = FastAPI(
        title="jorgex-social",
        version="0.1.0",
        openapi_url="/openapi.json" if docs else None,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
    )
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    app.state.network = network_for(settings)
    app.state.sessionmaker = make_sessionmaker(make_engine(settings))
    app.state.read_sessionmaker = make_sessionmaker(make_engine(settings, write=False))
    # The webhook records events through the same intake as the worker's polling.
    app.state.intake = Intake(app.state.network, retention=timedelta(days=settings.retention_days))
    app.state.human_pause = timedelta(minutes=settings.human_pause_minutes)

    @app.get("/health", tags=["system"])
    @app.head("/health", include_in_schema=False)  # for proxies and uptime checks
    def health(request: Request) -> JSONResponse:
        try:
            with request.app.state.read_sessionmaker() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            log.exception("health check failed")
            return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok", "simulation": request.app.state.settings.simulation})

    @app.get("/v1/token", tags=["access"])
    def whoami(token: TokenDep) -> dict:
        """What the calling token is allowed to do, so an agent can check before acting."""
        return {"name": token.name, "prefix": token.prefix, "permissions": token.permissions}

    app.add_middleware(AuditMiddleware)
    app.include_router(account_router)
    app.include_router(stats_router)
    app.include_router(publishing_router)
    app.include_router(media_files_router)
    app.include_router(replies_router)
    app.include_router(webhook_router)
    return app
