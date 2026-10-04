"""Router webhook ``/hooks/*``. Dùng: ``app.state.channels = ChannelsContext(...)`` rồi ``app.include_router(router)``.

Luồng: giới hạn kích thước -> verify chữ ký (sai => 401, không lộ lý do) -> normalize -> chống trùng -> ingest (Event Gateway
của A, được inject). Ingest lỗi => 503 và nhả khoá dedupe để nền tảng retry.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from zeus.channels.base import Dedupe, MemoryDedupe
from zeus.channels.messenger import MessengerAdapter
from zeus.contracts.api import EventIngestResponse, Paths
from zeus.contracts.interfaces import ChannelAdapter
from zeus.contracts.models import Channel, Event

log = logging.getLogger("zeus.channels")
Ingest = Callable[[Event], Awaitable[EventIngestResponse]]


@dataclass
class ChannelsContext:
    adapters: dict[Channel, ChannelAdapter]
    ingest: Ingest
    dedupe: Dedupe = field(default_factory=MemoryDedupe)
    max_body_bytes: int = 1_048_576


router = APIRouter()


def _ctx(request: Request) -> ChannelsContext:
    ctx = getattr(request.app.state, "channels", None)
    if ctx is None:
        raise HTTPException(503, "channels chưa cấu hình")
    return ctx


async def _handle(request: Request, channel: Channel) -> dict[str, int]:
    ctx = _ctx(request)
    adapter = ctx.adapters.get(channel)
    if adapter is None:
        raise HTTPException(404, "kênh chưa bật")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > ctx.max_body_bytes:
        raise HTTPException(413, "body quá lớn")
    body = await request.body()
    if len(body) > ctx.max_body_bytes:
        raise HTTPException(413, "body quá lớn")
    if not adapter.verify(request.headers, body):
        log.warning("webhook %s: chữ ký không hợp lệ", channel.value)
        raise HTTPException(401, "unauthorized")
    try:
        events = adapter.normalize(request.headers, body)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "payload không hợp lệ") from exc
    accepted = dupes = 0
    for ev in events:
        key = f"{channel.value}:{ev.external_id}" if ev.external_id else None
        if key and not await ctx.dedupe.claim(key):
            dupes += 1
            continue
        try:
            res = await ctx.ingest(ev)
        except Exception:
            if key:
                await ctx.dedupe.release(key)
            log.exception("ingest thất bại cho %s", channel.value)
            raise HTTPException(503, "ingest tạm thời lỗi") from None
        dupes += 1 if res.duplicate else 0
        accepted += 0 if res.duplicate else 1
    return {"accepted": accepted, "duplicates": dupes, "ignored": 0 if events else 1}


@router.post(Paths.HOOK_ZALO_BOT)
async def zalo_bot(request: Request) -> dict[str, int]:
    return await _handle(request, Channel.ZALO_BOT)


@router.post(Paths.HOOK_ZALO_OA)
async def zalo_oa(request: Request) -> dict[str, int]:
    return await _handle(request, Channel.ZALO_OA)


@router.post(Paths.HOOK_SHOPEE)
async def shopee(request: Request) -> dict[str, int]:
    return await _handle(request, Channel.SHOPEE)


@router.post(Paths.HOOK_MESSENGER)
async def messenger_post(request: Request) -> dict[str, int]:
    return await _handle(request, Channel.MESSENGER)


@router.get(Paths.HOOK_MESSENGER)
async def messenger_verify(request: Request) -> PlainTextResponse:
    adapter = _ctx(request).adapters.get(Channel.MESSENGER)
    if not isinstance(adapter, MessengerAdapter):
        raise HTTPException(404, "kênh chưa bật")
    challenge = adapter.verify_challenge(dict(request.query_params))
    if challenge is None:
        raise HTTPException(403, "forbidden")
    return PlainTextResponse(challenge)
