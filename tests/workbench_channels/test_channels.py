"""Vector chữ ký (tính độc lập bằng hashlib/hmac, không dùng hàm của adapter), replay, dedupe, group, router."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from zeus.app.main import create_app
from zeus.channels.base import MemoryDedupe, split_text
from zeus.channels.config import build_adapters, build_context, load_yaml
from zeus.channels.messenger import MessengerAdapter
from zeus.channels.router import ChannelsContext, router
from zeus.channels.shopee import ShopeeAdapter
from zeus.channels.zalo_bot import ZaloBotAdapter
from zeus.channels.zalo_oa import ZaloOAAdapter
from zeus.config import Settings
from zeus.contracts.api import EventIngestResponse, Paths
from zeus.contracts.interfaces import ChannelAdapter
from zeus.contracts.models import Channel, Event, EventKind

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0


def j(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


# ------------------------------------------------------------------ Zalo Bot

BOT = ZaloBotAdapter("s3cret-token", bot_username="zeusbot")


def bot_msg(text="Xin chào", chat_type="PRIVATE", mid="m1", **extra):
    msg = {"message_id": mid, "text": text, "from": {"id": "u1", "display_name": "Khách A"}, "chat": {"id": "c1", "chat_type": chat_type}, **extra}
    return j({"ok": True, "result": {"event_name": "message.text.received", "message": msg}})


def test_zalo_bot_secret_header_vectors():
    body = bot_msg()
    assert BOT.verify({"X-Bot-Api-Secret-Token": "s3cret-token"}, body)
    assert BOT.verify({"x-bot-api-secret-token": "s3cret-token"}, body)  # không phân biệt hoa thường
    for bad in ("", "s3cret-toke", "s3cret-token ", "S3CRET-TOKEN", "s3cret-tokenX"):
        assert not BOT.verify({"X-Bot-Api-Secret-Token": bad}, body), bad
    assert not BOT.verify({}, body)
    with pytest.raises(PermissionError):
        BOT.normalize({}, body)
    with pytest.raises(ValueError):
        ZaloBotAdapter("")


def test_zalo_bot_normalizes_untrusted_event():
    (ev,) = BOT.normalize({"X-Bot-Api-Secret-Token": "s3cret-token"}, bot_msg())
    assert isinstance(ev, Event) and ev.channel is Channel.ZALO_BOT and ev.kind is EventKind.MESSAGE
    assert ev.untrusted is True and ev.signature_verified is True
    assert ev.text == "Xin chào" and ev.sender.channel_user_id == "u1" and ev.sender.is_group is False and ev.external_id == "c1:m1"


def test_zalo_bot_group_only_when_mentioned_or_reply():
    h = {"X-Bot-Api-Secret-Token": "s3cret-token"}
    assert BOT.normalize(h, bot_msg(chat_type="GROUP")) == []
    assert BOT.normalize(h, bot_msg("nói chuyện khác @someone", chat_type="GROUP")) == []
    (a,) = BOT.normalize(h, bot_msg("@ZeusBot kiểm tra đơn", chat_type="GROUP"))
    assert a.sender.is_group and a.metadata["addressed_to_bot"]
    (b,) = BOT.normalize(h, bot_msg("ok", chat_type="GROUP", reply_to_message={"message_id": "x"}))
    assert b.text == "ok"
    # tin riêng không cần mention
    assert len(BOT.normalize(h, bot_msg(chat_type="PRIVATE"))) == 1


def test_zalo_bot_ignores_non_text_and_garbage():
    h = {"X-Bot-Api-Secret-Token": "s3cret-token"}
    assert BOT.normalize(h, j({"ok": True, "result": {"message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 2}}}})) == []
    assert BOT.normalize(h, j([1, 2])) == []
    with pytest.raises(ValueError):
        BOT.normalize(h, b"not json")


def test_split_text_never_exceeds_limit_and_is_lossless():
    long = ("Đây là câu tiếng Việt khá dài. " * 200).strip()
    parts = split_text(long, 2000)
    assert len(parts) >= 3 and all(0 < len(p) <= 2000 for p in parts)
    assert " ".join(parts).split() == long.split()  # không mất/đổi từ
    blob = "ế" * 4500  # không có điểm cắt đẹp -> cắt cứng nhưng không vượt limit
    assert [len(p) for p in split_text(blob, 2000)] == [2000, 2000, 500]
    assert split_text("   ") == [] and split_text("ngắn") == ["ngắn"]
    assert split_text("a" * 2000) == ["a" * 2000]


# ------------------------------------------------------------------ Zalo OA

OA = ZaloOAAdapter("app123", "oa-secret", max_age_s=300, clock=lambda: NOW)


def oa_body(ts=int(NOW * 1000), text="Cho hỏi giá", event="user_send_text"):
    return j({"app_id": "app123", "event_name": event, "sender": {"id": "zu1"}, "recipient": {"id": "oa1"}, "message": {"msg_id": "mm1", "text": text}, "timestamp": str(ts)})


def oa_sig(body: bytes, ts, app="app123", secret="oa-secret") -> str:
    return hashlib.sha256((app + body.decode() + str(ts) + secret).encode()).hexdigest()


def test_zalo_oa_signature_vectors():
    ts = int(NOW * 1000)
    body = oa_body()
    good = oa_sig(body, ts)
    assert OA.verify({"X-ZEvent-Signature": good}, body)
    assert OA.verify({"X-ZEvent-Signature": "mac=" + good}, body)
    assert not OA.verify({"X-ZEvent-Signature": good[:-1] + ("0" if good[-1] != "0" else "1")}, body)
    assert not OA.verify({"X-ZEvent-Signature": oa_sig(body, ts, secret="khac")}, body)
    assert not OA.verify({"X-ZEvent-Signature": oa_sig(body, ts, app="khac")}, body)
    assert not OA.verify({}, body)
    assert not OA.verify({"X-ZEvent-Signature": good}, body + b" ")  # body bị sửa


def test_zalo_oa_replay_window():
    old_ts = int((NOW - 3600) * 1000)
    body = oa_body(ts=old_ts)
    assert not OA.verify({"X-ZEvent-Signature": oa_sig(body, old_ts)}, body)  # ký đúng nhưng quá cũ
    fut = int((NOW + 3600) * 1000)
    body = oa_body(ts=fut)
    assert not OA.verify({"X-ZEvent-Signature": oa_sig(body, fut)}, body)


def test_zalo_oa_normalize():
    ts = int(NOW * 1000)
    body = oa_body()
    (ev,) = OA.normalize({"X-ZEvent-Signature": oa_sig(body, ts)}, body)
    assert ev.channel is Channel.ZALO_OA and ev.untrusted and ev.signature_verified and ev.text == "Cho hỏi giá" and ev.external_id == "mm1"
    other = oa_body(event="user_send_group_text")  # GMF cần gói trả phí -> bỏ qua
    assert OA.normalize({"X-ZEvent-Signature": oa_sig(other, ts)}, other) == []
    with pytest.raises(PermissionError):
        OA.normalize({"X-ZEvent-Signature": "0" * 64}, body)


# ------------------------------------------------------------------ Messenger

FOX = b"The quick brown fox jumps over the lazy dog"
FOX_HMAC_KEY = "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"  # HMAC_SHA256("key", FOX) — vector chuẩn công khai
MS = MessengerAdapter("key", "verify-me")


def ms_body(mid="mid.1", echo=False, text="Alo shop"):
    msg = {"mid": mid, "text": text, **({"is_echo": True} if echo else {})}
    return j({"object": "page", "entry": [{"id": "p1", "messaging": [{"sender": {"id": "psid1"}, "recipient": {"id": "p1"}, "timestamp": 1700000000000, "message": msg}]}]})


def test_messenger_signature_vectors():
    assert MS.verify({"X-Hub-Signature-256": "sha256=" + FOX_HMAC_KEY}, FOX)  # vector chuẩn bên ngoài
    assert not MS.verify({"X-Hub-Signature-256": FOX_HMAC_KEY}, FOX)  # thiếu tiền tố
    assert not MS.verify({"X-Hub-Signature-256": "sha1=" + FOX_HMAC_KEY}, FOX)
    assert not MS.verify({"X-Hub-Signature-256": "sha256=" + FOX_HMAC_KEY}, FOX + b".")
    assert not MS.verify({}, FOX)
    body = ms_body()
    sig = "sha256=" + hmac.new(b"key", body, hashlib.sha256).hexdigest()
    (ev,) = MS.normalize({"X-Hub-Signature-256": sig}, body)
    assert ev.channel is Channel.MESSENGER and ev.untrusted and ev.signature_verified and ev.external_id == "mid.1" and ev.metadata["event_timestamp_ms"] == 1700000000000
    echo = ms_body(echo=True)
    assert MS.normalize({"X-Hub-Signature-256": "sha256=" + hmac.new(b"key", echo, hashlib.sha256).hexdigest()}, echo) == []


def test_messenger_hub_challenge():
    q = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1158201444"}
    assert MS.verify_challenge(q) == "1158201444"
    assert MS.verify_challenge({**q, "hub.verify_token": "sai"}) is None
    assert MS.verify_challenge({**q, "hub.mode": "unsubscribe"}) is None
    assert MS.verify_challenge({}) is None


# ------------------------------------------------------------------ Shopee (định dạng CHƯA kiểm chứng: chỉ test nhất quán nội bộ)

URL = "https://hooks.example.vn/hooks/shopee"
SP = ShopeeAdapter("push-key", URL, max_age_s=600, clock=lambda: NOW)


def sp_body(ts=int(NOW), sn="2410040ABC", status="READY_TO_SHIP"):
    return j({"code": 3, "shop_id": 99, "timestamp": ts, "data": {"ordersn": sn, "status": status, "update_time": ts}})


def test_shopee_hmac_vectors():
    body = sp_body()
    sig = hmac.new(b"push-key", URL.encode() + b"|" + body, hashlib.sha256).hexdigest()
    assert SP.verify({"Authorization": sig}, body)
    assert not SP.verify({"Authorization": sig}, body + b" ")
    assert not SP.verify({"Authorization": "0" * 64}, body)
    assert not SP.verify({}, body)
    assert not ShopeeAdapter("khac", URL, clock=lambda: NOW).verify({"Authorization": sig}, body)
    assert not ShopeeAdapter("push-key", URL + "/x", clock=lambda: NOW).verify({"Authorization": sig}, body)  # URL nằm trong chuỗi ký
    (ev,) = SP.normalize({"Authorization": sig}, body)
    assert ev.kind is EventKind.ORDER and ev.channel is Channel.SHOPEE and ev.untrusted and ev.metadata["ordersn"] == "2410040ABC"
    assert SP.outbound_specs() == []  # không outbound Shopee


def test_shopee_replay_window():
    body = sp_body(ts=int(NOW - 7200))
    assert not SP.verify({"Authorization": SP.sign(body)}, body)


# ------------------------------------------------------------------ protocol + config

def test_all_adapters_satisfy_protocol_and_specs_are_gated():
    for a in (BOT, OA, MS, SP):
        assert isinstance(a, ChannelAdapter)
        for s in a.outbound_specs():
            assert s.name == f"channel.{a.channel.value}.send_message" and s.external and s.risk.value in ("R2", "R3") and not s.reversible and s.owner == "D"


def test_config_enables_only_channels_with_secrets():
    cfg = load_yaml(ROOT / "config/channels.yaml")
    assert set(cfg["channels"]) == {"zalo_bot", "zalo_oa", "messenger", "shopee"}  # không có kênh Facebook cá nhân
    assert not [p for p in (ROOT / "zeus/channels").glob("*.py") if "personal" in p.name or "facebook" in p.name]
    assert build_adapters(cfg, {}) == {}
    ad = build_adapters(cfg, {"ZEUS_ZALO_BOT_SECRET_TOKEN": "t"})
    assert list(ad) == [Channel.ZALO_BOT]
    cfg["channels"]["messenger"]["enabled"] = True
    cfg["channels"]["shopee"].update(enabled=True, push_url=URL)
    ad = build_adapters(cfg, {"ZEUS_ZALO_BOT_SECRET_TOKEN": "t", "ZEUS_FB_APP_SECRET": "a", "ZEUS_FB_VERIFY_TOKEN": "v", "ZEUS_SHOPEE_PUSH_KEY": "k"})
    assert set(ad) == {Channel.ZALO_BOT, Channel.MESSENGER, Channel.SHOPEE}
    assert not (set(Channel) - {Channel.ZALO_BOT, Channel.ZALO_OA, Channel.MESSENGER, Channel.SHOPEE, Channel.WORKBENCH, Channel.API, Channel.ERP, Channel.SCHEDULER, Channel.INTERNAL})


# ------------------------------------------------------------------ router /hooks/*


def make_app(ingest_impl=None, **ctx_kw):
    received: list[Event] = []

    async def ingest(ev: Event) -> EventIngestResponse:
        if ingest_impl:
            await ingest_impl(ev)
        received.append(ev)
        return EventIngestResponse(event_id=ev.event_id, accepted=True)

    app = create_app(Settings(env="test"), routers=[router])
    app.state.channels = ChannelsContext(adapters={Channel.ZALO_BOT: BOT, Channel.ZALO_OA: OA, Channel.MESSENGER: MS, Channel.SHOPEE: SP}, ingest=ingest, dedupe=MemoryDedupe(), **ctx_kw)
    return TestClient(app), received


def test_router_rejects_bad_signature_and_does_not_ingest():
    c, got = make_app()
    for path, hdr in ((Paths.HOOK_ZALO_BOT, {"X-Bot-Api-Secret-Token": "sai"}), (Paths.HOOK_ZALO_OA, {"X-ZEvent-Signature": "0" * 64}), (Paths.HOOK_MESSENGER, {"X-Hub-Signature-256": "sha256=00"}), (Paths.HOOK_SHOPEE, {"Authorization": "00"})):
        r = c.post(path, content=b"{}", headers=hdr)
        assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}, path
    assert c.post(Paths.HOOK_ZALO_BOT, content=b"{}").status_code == 401
    assert got == []


def test_router_ingests_dedupes_and_retries_on_failure():
    boom = {"on": True}

    async def flaky(ev: Event):
        if boom["on"]:
            raise RuntimeError("down")

    c, got = make_app(flaky)
    h = {"X-Bot-Api-Secret-Token": "s3cret-token"}
    body = bot_msg(mid="dup1")
    assert c.post(Paths.HOOK_ZALO_BOT, content=body, headers=h).status_code == 503  # ingest lỗi -> nền tảng retry
    boom["on"] = False
    r = c.post(Paths.HOOK_ZALO_BOT, content=body, headers=h)
    assert r.status_code == 200 and r.json()["accepted"] == 1 and len(got) == 1
    r = c.post(Paths.HOOK_ZALO_BOT, content=body, headers=h)  # replay cùng message_id
    assert r.status_code == 200 and r.json() == {"accepted": 0, "duplicates": 1, "ignored": 0} and len(got) == 1
    assert got[0].untrusted and got[0].signature_verified


def test_router_oa_messenger_shopee_end_to_end():
    c, got = make_app()
    ts = int(NOW * 1000)
    # đặt clock thật gần NOW: OA/Shopee dùng clock cố định ở NOW nên payload dùng NOW
    b = oa_body()
    assert c.post(Paths.HOOK_ZALO_OA, content=b, headers={"X-ZEvent-Signature": oa_sig(b, ts)}).json()["accepted"] == 1
    b = ms_body()
    assert c.post(Paths.HOOK_MESSENGER, content=b, headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"key", b, hashlib.sha256).hexdigest()}).json()["accepted"] == 1
    b = sp_body()
    assert c.post(Paths.HOOK_SHOPEE, content=b, headers={"Authorization": SP.sign(b)}).json()["accepted"] == 1
    assert [e.channel for e in got] == [Channel.ZALO_OA, Channel.MESSENGER, Channel.SHOPEE]


def test_router_group_message_ignored_but_acked():
    c, got = make_app()
    r = c.post(Paths.HOOK_ZALO_BOT, content=bot_msg(chat_type="GROUP"), headers={"X-Bot-Api-Secret-Token": "s3cret-token"})
    assert r.status_code == 200 and r.json()["ignored"] == 1 and got == []


def test_router_messenger_get_challenge():
    c, _ = make_app()
    p = Paths.HOOK_MESSENGER
    r = c.get(p, params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "abc123"})
    assert r.status_code == 200 and r.text == "abc123"
    assert c.get(p, params={"hub.mode": "subscribe", "hub.verify_token": "sai", "hub.challenge": "abc123"}).status_code == 403


def test_router_body_limit_and_disabled_channel():
    c, _ = make_app(max_body_bytes=100)
    assert c.post(Paths.HOOK_ZALO_BOT, content=b"x" * 500, headers={"X-Bot-Api-Secret-Token": "s3cret-token"}).status_code == 413
    app = create_app(Settings(env="test"), routers=[router])
    app.state.channels = build_context({"channels": {}}, ingest=None)  # type: ignore[arg-type]
    assert TestClient(app).post(Paths.HOOK_SHOPEE, content=b"{}").status_code == 404
    assert TestClient(create_app(Settings(env="test"), routers=[router])).post(Paths.HOOK_SHOPEE, content=b"{}").status_code == 503


def test_router_malformed_signed_payload_is_400_not_500():
    c, got = make_app()
    b = b"{broken"
    assert c.post(Paths.HOOK_ZALO_BOT, content=b, headers={"X-Bot-Api-Secret-Token": "s3cret-token"}).status_code == 400
    assert got == []
