"""FastAPI app factory.

- ``create_app(settings, routers, system)``: app tối thiểu (``/healthz``); có ``system`` => mount toàn bộ hệ thống:
  Control API ``/api/v1`` (A), Worker API ``/worker/v1`` (C), evidence ``/internal/evidence`` (B),
  Workbench ``/wb`` + webhook ``/hooks/*`` (D).
- ``create_server_app()``: app production — lifespan kết nối Temporal + PostgreSQL rồi dựng ``System``.
  Chạy: ``uvicorn --factory zeus.app.main:create_server_app --host 127.0.0.1 --port 8080`` (xem docs/runbooks/SERVER_BOOTSTRAP.md).

Xác thực: ``/api/v1`` và ``/internal`` cần ``Authorization: Bearer <token>`` khi biến môi trường ``ZEUS_API_TOKEN``
(tên đổi được qua ``ZEUS_API_TOKEN_ENV``) có giá trị; env staging/prod mà thiếu token => 503 (fail closed).
``/worker/v1`` dùng token theo worker (C); ``/wb`` dùng đăng nhập + CSRF (D); ``/hooks`` dùng chữ ký kênh (D).
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

import zeus
from zeus.config import Settings
from zeus.contracts.api import Paths
from zeus.contracts.models import CONTRACTS_VERSION

if TYPE_CHECKING:
    from zeus.app.system import System

log = logging.getLogger("zeus.app")


def api_auth(settings: Settings) -> Any:
    """Dependency Bearer token cho /api/v1 và /internal (token đọc lúc gọi, so sánh hằng thời gian)."""

    async def check(authorization: str | None = Header(default=None)) -> None:
        token = settings.api_token()
        if token is None:
            if settings.env in ("staging", "prod"):
                raise HTTPException(503, f"chưa cấu hình {settings.api_token_env}")
            return
        expected = f"Bearer {token.get_secret_value()}"
        if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    return Depends(check)


def mount_system_routes(app: FastAPI, settings: Settings) -> None:
    """Gắn router của A/B/C/D (state được gắn riêng qua ``install_system``)."""
    from zeus.api.router import router as control_router
    from zeus.channels.router import router as hooks_router
    from zeus.evidence.router import build_router as build_evidence_router
    from zeus.workbench.router import router as workbench_router
    from zeus.workers.api import router as worker_router

    guard = [api_auth(settings)]
    app.include_router(control_router, dependencies=guard)
    app.include_router(worker_router)
    app.include_router(workbench_router)
    app.include_router(hooks_router)

    class _LazyEvidence:
        """Router evidence của B cần store lúc dựng; dùng proxy tới store của System gắn ở app.state."""

        def __getattr__(self, name: str) -> Any:
            system = getattr(app.state, "system", None)
            if system is None:
                raise HTTPException(503, "evidence store chưa được cấu hình")
            return getattr(system.evidence, name)

    app.include_router(build_evidence_router(_LazyEvidence()), prefix="/internal", dependencies=guard)  # type: ignore[arg-type]

    @app.get(Paths.READYZ)
    async def readyz(request: Request) -> dict[str, Any]:
        from zeus.storage.db import aconnect

        system = getattr(request.app.state, "system", None)
        if system is None:
            raise HTTPException(503, "system chưa được cấu hình")
        try:
            async with await aconnect(system.dsn, autocommit=True) as conn:
                await conn.execute("SELECT 1")
        except Exception:
            raise HTTPException(503, "database không sẵn sàng") from None
        return {"ready": True}

    @app.get("/internal/system", dependencies=guard)
    async def system_info(request: Request) -> dict[str, Any]:
        system = getattr(request.app.state, "system", None)
        if system is None:
            raise HTTPException(503, "system chưa được cấu hình")
        return {
            "task_queue": system.task_queue,
            "actions": sorted(s.name for s in system.deps.gateway.list_actions()),
            "channels": sorted(c.value for c in system.channels.adapters),
            "workers": [w.worker_id for w in await system.worker_services.registry.list()],
        }


def install_system(app: FastAPI, system: "System") -> None:
    from zeus.api.router import install as install_control

    app.state.system = system
    install_control(app, system.control)
    app.state.worker_services = system.worker_services
    app.state.workbench = system.workbench
    app.state.channels = system.channels


def create_app(settings: Settings | None = None, routers: Sequence[APIRouter] = (), system: "System | None" = None, *, lifespan: Any = None) -> FastAPI:
    settings = settings or (system.settings if system else Settings.from_env())
    app = FastAPI(title="ZeusVN Brain", version=zeus.__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings

    @app.get(Paths.HEALTHZ)
    async def healthz() -> dict[str, object]:
        sys_ = getattr(app.state, "system", None)
        return {
            "status": "ok",
            "version": zeus.__version__,
            "contracts_version": CONTRACTS_VERSION,
            "env": settings.env,
            "claude_cloud_available": settings.claude_cloud_available,
            "system": sys_ is not None,
        }

    for r in routers:
        app.include_router(r)
    if system is not None or lifespan is not None:
        mount_system_routes(app, settings)
    if system is not None:
        install_system(app, system)
    return app


def create_server_app(settings: Settings | None = None) -> FastAPI:
    """App production: lifespan kết nối Temporal (pydantic converter) và PostgreSQL, dựng System, gắn vào app."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from zeus.app.system import build_system
        from zeus.obs.logging import configure_logging
        from zeus.orchestration.client import connect

        configure_logging(settings.log_level)
        client = await connect(settings.temporal_address, settings.temporal_namespace)
        system = build_system(settings, temporal_client=client)
        install_system(app, system)
        log.info("ZeusVN Brain API sẵn sàng (env=%s, cloud=%s)", settings.env, settings.claude_cloud_available)
        yield

    return create_app(settings, lifespan=lifespan)


app = create_app()
