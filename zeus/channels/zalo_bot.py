"""Zalo Bot Platform (kênh chính, ADR-008).

Xác thực: header ``X-Bot-Api-Secret-Token`` so chuỗi hằng thời gian với secret đặt lúc setWebhook.
Header này TĨNH (không có timestamp) => không chống replay bằng chữ ký; chống trùng bằng ``external_id`` ở router.
Group: chỉ nhận khi @mention bot hoặc reply tin của bot; group trên Zalo Bot là tính năng thử nghiệm => luồng chính
không phụ thuộc group.

ĐỊNH DẠNG PAYLOAD: dựng theo tài liệu công khai (result.message.{from,chat,text,message_id}); chưa kiểm chứng với
bot thật — parser khoan dung và bỏ qua mọi thứ không nhận ra thay vì đoán.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from zeus.channels.base import ct_equal, header
from zeus.contracts.models import ActionSpec, Channel, ChannelIdentity, Event, EventKind, RiskLevel

ZALO_BOT_MAX_CHARS = 2000


def send_spec(channel: Channel, extra_props: dict[str, Any] | None = None) -> ActionSpec:
    props: dict[str, Any] = {"to": {"type": "string"}, "text": {"type": "string", "minLength": 1}}
    props.update(extra_props or {})
    return ActionSpec(
        name=f"channel.{channel.value}.send_message",
        version="1",
        description=f"Gửi tin nhắn ra kênh {channel.value} (cần duyệt)",
        risk=RiskLevel.R2,
        external=True,
        reversible=False,
        idempotent=False,
        owner="D",
        input_schema={"type": "object", "required": ["to", "text"], "properties": props},
    )


class ZaloBotAdapter:
    channel = Channel.ZALO_BOT

    def __init__(self, secret_token: str, bot_username: str | None = None) -> None:
        if not secret_token:
            raise ValueError("secret_token rỗng")
        self._secret = secret_token
        self._bot_username = (bot_username or "").lstrip("@").lower()

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return ct_equal(header(headers, "X-Bot-Api-Secret-Token"), self._secret)

    def _addressed(self, msg: dict[str, Any]) -> bool:
        if msg.get("reply_to_message"):
            return True
        if msg.get("mentions") or msg.get("is_mentioned") is True:
            return True
        text = str(msg.get("text") or "").lower()
        return bool(self._bot_username) and f"@{self._bot_username}" in text

    def normalize(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        if not self.verify(headers, body):
            raise PermissionError("invalid signature")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ValueError("body không phải JSON") from exc
        result = payload.get("result", payload) if isinstance(payload, dict) else None
        msg = result.get("message") if isinstance(result, dict) else None
        if not isinstance(msg, dict) or not msg.get("text"):
            return []  # sự kiện không phải tin nhắn văn bản
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        is_group = str(chat.get("chat_type", "")).upper() in {"GROUP", "SUPERGROUP"}
        addressed = self._addressed(msg)
        if is_group and not addressed:
            return []
        mid = msg.get("message_id")
        if mid is None or sender.get("id") is None:
            return []
        return [
            Event(
                channel=self.channel,
                kind=EventKind.MESSAGE,
                external_id=f"{chat.get('id', '')}:{mid}",
                sender=ChannelIdentity(
                    channel_user_id=str(sender["id"]),
                    display_name=sender.get("display_name") or sender.get("name"),
                    conversation_id=str(chat["id"]) if chat.get("id") is not None else None,
                    is_group=is_group,
                ),
                text=str(msg["text"]),
                signature_verified=True,
                untrusted=True,
                metadata={"addressed_to_bot": addressed},
            )
        ]

    def outbound_specs(self) -> list[ActionSpec]:
        return [send_spec(self.channel)]
