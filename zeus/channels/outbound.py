"""Outbound kênh = ToolProvider (typed action ``channel.<kênh>.send_message``). Chỉ được gọi qua ToolGateway.

Adapter KHÔNG tự gửi; module này là nơi DUY NHẤT trong zeus/channels dùng HTTP client. Spec là R2 + external nên
policy của A luôn yêu cầu approval. Messenger: ngoài cửa sổ 24h chỉ cho phép tag HUMAN_AGENT do người vận hành
(<= 7 ngày), còn lại PolicyDenied.

CHƯA KIỂM CHỨNG với nền tảng thật: đường dẫn/tham số gửi của Zalo OA và phiên bản Graph API (cấu hình trong channels.yaml).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from zeus.channels.base import split_text
from zeus.contracts.interfaces import PolicyDenied
from zeus.contracts.models import ActionResult, ActionSpec, Channel, TypedAction

TokenGetter = Callable[[], str | None]
MESSENGER_WINDOW = timedelta(hours=24)
HUMAN_AGENT_WINDOW = timedelta(days=7)
CHUNK = {Channel.ZALO_BOT: 2000, Channel.ZALO_OA: 2000, Channel.MESSENGER: 2000}


def check_messenger_window(args: dict[str, Any], now: datetime) -> bool:
    """True nếu gửi tự động được phép; False nếu phải dùng HUMAN_AGENT; ném PolicyDenied nếu không thể gửi."""
    raw = args.get("last_inbound_at")
    if not raw:
        raise PolicyDenied("Messenger: thiếu last_inbound_at, không chứng minh được cửa sổ 24h")
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyDenied("Messenger: last_inbound_at không hợp lệ") from exc
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = now - last
    if age <= MESSENGER_WINDOW:
        return True
    if args.get("messaging_tag") == "HUMAN_AGENT" and args.get("human_operator") and age <= HUMAN_AGENT_WINDOW:
        return False
    raise PolicyDenied("Messenger: ngoài 24h — chỉ người vận hành được trả lời (tag HUMAN_AGENT, tối đa 7 ngày)")


class ChannelOutboundProvider:
    def __init__(
        self,
        specs: Sequence[ActionSpec],
        tokens: dict[Channel, TokenGetter],
        http: httpx.AsyncClient,
        bases: dict[str, str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._specs = list(specs)
        self._tokens, self._http, self._now = tokens, http, now
        b = {
            "zalo_bot": "https://bot-api.zaloplatforms.com",
            "zalo_oa": "https://openapi.zalo.me",
            "messenger": "https://graph.facebook.com/v21.0",
        }
        b.update(bases or {})
        self._bases = b

    def specs(self) -> list[ActionSpec]:
        return list(self._specs)

    async def execute(self, action: TypedAction, spec: ActionSpec) -> ActionResult:
        parts = spec.name.split(".")
        if len(parts) != 3 or parts[0] != "channel" or parts[2] != "send_message":
            raise PolicyDenied(f"outbound không hỗ trợ {spec.name}")
        channel = Channel(parts[1])
        to, text = action.args.get("to"), action.args.get("text")
        if not isinstance(to, str) or not to or not isinstance(text, str) or not text.strip():
            return ActionResult(action_id=action.action_id, ok=False, error="thiếu to/text", executed_by="control")
        auto_ok = check_messenger_window(action.args, self._now()) if channel is Channel.MESSENGER else True
        chunks = split_text(text, CHUNK[channel])
        if action.dry_run:
            return ActionResult(action_id=action.action_id, ok=True, output={"dry_run": True, "chunks": len(chunks)}, executed_by="control")
        token = self._tokens.get(channel, lambda: None)()
        if not token:
            return ActionResult(action_id=action.action_id, ok=False, error=f"token kênh {channel.value} chưa cấu hình", executed_by="control")
        ids: list[str] = []
        for i, chunk in enumerate(chunks):
            try:
                resp = await self._send(channel, token, to, chunk, auto_ok)
            except httpx.HTTPError as exc:
                return ActionResult(
                    action_id=action.action_id, ok=False, output={"sent_chunks": i}, error=f"lỗi mạng: {type(exc).__name__}", executed_by="control"
                )
            if resp.status_code >= 300:
                return ActionResult(
                    action_id=action.action_id, ok=False, output={"sent_chunks": i}, error=f"HTTP {resp.status_code} từ {channel.value}", executed_by="control"
                )
            ids.append(str(_message_id(resp)))
        return ActionResult(action_id=action.action_id, ok=True, output={"sent_chunks": len(chunks), "message_ids": ids}, executed_by="control")

    async def _send(self, channel: Channel, token: str, to: str, text: str, auto_ok: bool) -> httpx.Response:
        if channel is Channel.ZALO_BOT:
            return await self._http.post(f"{self._bases['zalo_bot']}/bot{token}/sendMessage", json={"chat_id": to, "text": text})
        if channel is Channel.ZALO_OA:
            return await self._http.post(
                f"{self._bases['zalo_oa']}/v3.0/oa/message/cs", headers={"access_token": token}, json={"recipient": {"user_id": to}, "message": {"text": text}}
            )
        body: dict[str, Any] = {"recipient": {"id": to}, "message": {"text": text}, "messaging_type": "RESPONSE"}
        if not auto_ok:
            body.update(messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")
        return await self._http.post(f"{self._bases['messenger']}/me/messages", headers={"Authorization": f"Bearer {token}"}, json=body)


def _message_id(resp: httpx.Response) -> Any:
    try:
        j = resp.json()
    except ValueError:
        return ""
    if not isinstance(j, dict):
        return ""
    return j.get("message_id") or (j.get("result") or {}).get("message_id") or (j.get("data") or {}).get("message_id") or ""
