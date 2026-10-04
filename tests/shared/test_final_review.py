"""Hồi quy review cuối: SEC-7 (login non-ASCII), SEC-8 (api dev không token)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI

from zeus.app.main import api_auth
from zeus.config import Settings
from zeus.workbench.security import AuthConfig, hash_password


def test_sec7_login_non_ascii_username_no_typeerror() -> None:
    cfg = AuthConfig(password_hash=hash_password("mk-dung", iterations=1000), username="operator")
    assert cfg.login("1.2.3.4", "người-dùng", "mk-dung") is False
    assert cfg.login("1.2.3.4", "operator", "mk-dung") is True


def _app(env: str) -> FastAPI:
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(api_auth(Settings(env=env)).dependency)])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    return app


async def _get(app: FastAPI, client: tuple[str, int]) -> int:
    t = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=t, base_url="http://zeus") as c:
        return (await c.get("/x")).status_code


async def test_sec8_dev_no_token_only_loopback_or_explicit_flag(monkeypatch) -> None:
    monkeypatch.delenv("ZEUS_API_TOKEN", raising=False)
    monkeypatch.delenv("ZEUS_ALLOW_NO_TOKEN", raising=False)
    app = _app("dev")
    assert await _get(app, ("127.0.0.1", 1)) == 200
    assert await _get(app, ("10.0.0.5", 1)) == 401
    monkeypatch.setenv("ZEUS_ALLOW_NO_TOKEN", "1")
    assert await _get(app, ("10.0.0.5", 1)) == 200
