from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from zeus.app.main import create_app
from zeus.config import Settings
from zeus.workbench.demo import build_demo_context, seed_demo
from zeus.workbench.router import WorkbenchContext, router
from zeus.workbench.security import AuthConfig, hash_password

PASSWORD = "mat-khau-thu-nghiem"


@dataclass
class World:
    ctx: WorkbenchContext
    ids: dict[str, str]
    client: TestClient

    def csrf(self, path: str = "/wb/command") -> str:
        html = self.client.get(path).text
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        assert m, "không thấy csrf_token trong form"
        return m.group(1)


@pytest_asyncio.fixture
async def world() -> World:
    auth = AuthConfig(password_hash=hash_password(PASSWORD, iterations=1000), session_key=b"k" * 32)
    ctx = build_demo_context(auth)
    ids = await seed_demo(ctx)
    app = create_app(Settings(env="test"), routers=[router])
    app.state.workbench = ctx
    client = TestClient(app, base_url="http://testserver")
    return World(ctx, ids, client)


def login(w: World, password: str = PASSWORD, username: str = "operator") -> Any:
    token = w.csrf("/wb/login")
    return w.client.post("/wb/login", data={"username": username, "password": password, "csrf_token": token}, follow_redirects=False)


@pytest_asyncio.fixture
async def authed(world: World) -> World:
    r = login(world)
    assert r.status_code == 303
    return world
