import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from jorgex_social.access.deps import TokenDep
from jorgex_social.config import Settings
from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.stats.api import router as stats_router

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="jorgex-social", version="0.1.0")
    app.state.settings = settings
    app.state.sessionmaker = make_sessionmaker(make_engine(settings))

    @app.get("/health", tags=["system"])
    def health(request: Request) -> JSONResponse:
        try:
            with request.app.state.sessionmaker() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            log.exception("health check failed")
            return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok", "simulation": request.app.state.settings.simulation})

    @app.get("/v1/token", tags=["access"])
    def whoami(token: TokenDep) -> dict:
        """What the calling token is allowed to do, so an agent can check before acting."""
        return {"name": token.name, "prefix": token.prefix, "permissions": token.permissions}

    app.include_router(stats_router)
    return app
