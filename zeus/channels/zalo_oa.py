"""Zalo Official Account Open API.

Chữ ký: ``X-ZEvent-Signature`` = sha256(appId + data + timestamp + OASecretKey) (hex; chấp nhận tiền tố ``mac=``),
``data`` = body thô, ``timestamp`` lấy từ body. Timestamp nằm trong chuỗi ký nên kiểm tra độ tươi = chống replay thật.
Chỉ xử lý ``user_send_text``; sự kiện nhóm GMF cần gói trả phí (gate G6) => bỏ qua.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping

from zeus.channels.base import Clock, ct_equal, header, is_fresh
from zeus.channels.zalo_bot import send_spec
from zeus.contracts.models import ActionSpec, Channel, ChannelIdentity, Event, EventKind


class ZaloOAAdapter:
    channel = Channel.ZALO_OA

    def __init__(self, app_id: str, oa_secret: str, max_age_s: float = 300, clock: Clock = time.time) -> None:
        if not app_id or not oa_secret:
            raise ValueError("app_id/oa_secret rỗng")
        self._app_id, self._secret, self._max_age, self._clock = app_id, oa_secret, max_age_s, clock

    def expected_signature(self, body: bytes, timestamp: str) -> str:
        return hashlib.sha256((self._app_id + body.decode("utf-8", "replace") + timestamp + self._secret).encode()).hexdigest()

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        got = header(headers, "X-ZEvent-Signature").removeprefix("mac=")
        try:
            payload = json.loads(body)
            ts = payload["timestamp"]
        except (ValueError, KeyError, TypeError):
            return False
        if not is_fresh(ts, self._clock(), self._max_age):
            return False
        return ct_equal(got, self.expected_signature(body, str(ts)))

    def normalize(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        if not self.verify(headers, body):
            raise PermissionError("invalid signature")
        p = json.loads(body)
        if p.get("event_name") != "user_send_text":
            return []
        msg, sender = p.get("message") or {}, p.get("sender") or {}
        if not msg.get("text") or sender.get("id") is None:
            return []
        return [
            Event(
                channel=self.channel,
                kind=EventKind.MESSAGE,
                external_id=str(msg.get("msg_id") or f"{sender['id']}:{p['timestamp']}"),
                sender=ChannelIdentity(channel_user_id=str(sender["id"]), conversation_id=str(sender["id"])),
                text=str(msg["text"]),
                signature_verified=True,
                untrusted=True,
                metadata={"oa_id": (p.get("recipient") or {}).get("id"), "event_timestamp": p["timestamp"]},
            )
        ]

    def outbound_specs(self) -> list[ActionSpec]:
        return [send_spec(self.channel)]
