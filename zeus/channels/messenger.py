"""Messenger Platform (Fanpage). GET: ``hub.challenge``; POST: ``X-Hub-Signature-256`` = sha256=HMAC(app_secret, body).

Chữ ký không có timestamp => chống replay bằng dedupe ``mid`` ở router. Cửa sổ 24h: ghi nhận ``event_timestamp_ms``
trong metadata để outbound kiểm tra (xem outbound.py). Tin echo (do Page gửi) bị bỏ qua.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from zeus.channels.base import ct_equal, header, hmac_sha256_hex
from zeus.channels.zalo_bot import send_spec
from zeus.contracts.models import ActionSpec, Channel, ChannelIdentity, Event, EventKind


class MessengerAdapter:
    channel = Channel.MESSENGER

    def __init__(self, app_secret: str, verify_token: str) -> None:
        if not app_secret or not verify_token:
            raise ValueError("app_secret/verify_token rỗng")
        self._secret, self._verify_token = app_secret, verify_token

    def verify_challenge(self, params: Mapping[str, str]) -> str | None:
        """Trả hub.challenge nếu mode=subscribe và verify_token đúng, ngược lại None."""
        if params.get("hub.mode") == "subscribe" and ct_equal(params.get("hub.verify_token", ""), self._verify_token):
            return params.get("hub.challenge")
        return None

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        got = header(headers, "X-Hub-Signature-256")
        if not got.startswith("sha256="):
            return False
        return ct_equal(got[len("sha256=") :], hmac_sha256_hex(self._secret, body))

    def normalize(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        if not self.verify(headers, body):
            raise PermissionError("invalid signature")
        p = json.loads(body)
        if p.get("object") != "page":
            return []
        events: list[Event] = []
        for entry in p.get("entry") or []:
            for m in entry.get("messaging") or []:
                msg, sender = m.get("message") or {}, m.get("sender") or {}
                if msg.get("is_echo") or not msg.get("text") or not msg.get("mid") or not sender.get("id"):
                    continue
                events.append(
                    Event(
                        channel=self.channel,
                        kind=EventKind.MESSAGE,
                        external_id=str(msg["mid"]),
                        sender=ChannelIdentity(channel_user_id=str(sender["id"]), conversation_id=str(sender["id"])),
                        text=str(msg["text"]),
                        signature_verified=True,
                        untrusted=True,
                        metadata={"page_id": (m.get("recipient") or {}).get("id"), "event_timestamp_ms": m.get("timestamp")},
                    )
                )
        return events

    def outbound_specs(self) -> list[ActionSpec]:
        return [
            send_spec(
                self.channel,
                {"last_inbound_at": {"type": "string", "format": "date-time"}, "messaging_tag": {"enum": ["HUMAN_AGENT"]}, "human_operator": {"type": "string"}},
            )
        ]
