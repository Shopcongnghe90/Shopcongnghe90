"""R3: dedupe webhook hai pha — tiến trình chết giữa claim và ingest không được làm mất event 24h."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zeus.channels.pg_dedupe import PgDedupe
from zeus.storage import apply_migrations

pytestmark = pytest.mark.pg


async def test_unconfirmed_claim_expires_but_confirmed_claim_sticks(pg_dsn):
    apply_migrations(pg_dsn, Path(__file__).resolve().parents[2] / "migrations")
    d = PgDedupe(pg_dsn, ttl_s=3600, inflight_s=1)
    assert await d.claim("zalo_bot:m1") is True
    assert await d.claim("zalo_bot:m1") is False  # đang xử lý
    await asyncio.sleep(1.2)
    assert await d.claim("zalo_bot:m1") is True  # tiến trình trước chết trước khi ingest: được xử lý lại (trước đây: giữ 24h)
    await d.confirm("zalo_bot:m1")
    await asyncio.sleep(1.2)
    assert await d.claim("zalo_bot:m1") is False  # đã ingest xong: dedupe bền
    await d.release("zalo_bot:m2")  # release khoá không tồn tại không lỗi


async def test_router_confirms_after_successful_ingest(pg_dsn):
    from zeus.channels.router import ChannelsContext, router
    from zeus.contracts.api import EventIngestResponse, Paths
    from zeus.contracts.models import Channel
    from zeus.testing.fakes import FakeChannelAdapter
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import json

    apply_migrations(pg_dsn, Path(__file__).resolve().parents[2] / "migrations")
    dedupe = PgDedupe(pg_dsn, inflight_s=0)  # claim chưa confirm sẽ bị chiếm lại ngay

    async def ingest(ev):  # noqa: ANN001
        return EventIngestResponse(event_id=ev.event_id, accepted=True, duplicate=False)

    adapter = FakeChannelAdapter("s", Channel.ZALO_BOT)
    app = FastAPI()
    app.include_router(router)
    app.state.channels = ChannelsContext(adapters={Channel.ZALO_BOT: adapter}, ingest=ingest, dedupe=dedupe)
    body = json.dumps({"id": "m-1", "text": "xin chào"}).encode()
    c = TestClient(app)
    h = {"X-Fake-Signature": adapter.sign(body)}
    r1 = c.post(Paths.HOOK_ZALO_BOT, content=body, headers=h)
    r2 = c.post(Paths.HOOK_ZALO_BOT, content=body, headers=h)
    assert r1.status_code == 200 and r1.json()["accepted"] == 1
    assert r2.json() == {"accepted": 0, "duplicates": 1, "ignored": 0}  # đã confirm => vẫn là trùng dù inflight_s=0
