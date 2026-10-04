"""FastAPI app factory tối thiểu. Chạy: ``uvicorn zeus.app.main:app --host 127.0.0.1 --port 8080``."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, FastAPI

import zeus
from zeus.config import Settings
from zeus.contracts.api import Paths
from zeus.contracts.models import CONTRACTS_VERSION


def create_app(settings: Settings | None = None, routers: Sequence[APIRouter] = ()) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="ZeusVN Brain", version=zeus.__version__, docs_url=None, redoc_url=None)
    app.state.settings = settings

    @app.get(Paths.HEALTHZ)
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "version": zeus.__version__,
            "contracts_version": CONTRACTS_VERSION,
            "env": settings.env,
            "claude_cloud_available": settings.claude_cloud_available,
        }

    for r in routers:
        app.include_router(r)
    return app


app = create_app()
