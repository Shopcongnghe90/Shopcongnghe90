"""Shopee Open Platform push (đơn hàng...).

Chữ ký: HMAC-SHA256 hex, header ``Authorization``, khoá = push key (partner key), chuỗi ký = ``push_url + "|" + body``.

CHƯA KIỂM CHỨNG: chuỗi ký chính xác (có/không ``|``, có/không partner_id), tên header, cấu trúc payload
(code/shop_id/timestamp/data.ordersn/status). Phần này dựng theo tài liệu công khai và PHẢI được đối chiếu với push
thật của shop (gate trước khi bật `shopee.enabled`). Test chỉ chứng minh tính nhất quán nội bộ.
Không có outbound: chat Shopee dùng Trợ lý Chat AI có sẵn; chat API chỉ khi shop được whitelist (ADR-008).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from zeus.channels.base import Clock, ct_equal, header, hmac_sha256_hex, is_fresh
from zeus.contracts.models import ActionSpec, Channel, Event, EventKind


class ShopeeAdapter:
    channel = Channel.SHOPEE

    def __init__(self, push_key: str, push_url: str, max_age_s: float = 600, clock: Clock = time.time) -> None:
        if not push_key or not push_url:
            raise ValueError("push_key/push_url rỗng")
        self._key, self._url, self._max_age, self._clock = push_key, push_url, max_age_s, clock

    def sign(self, body: bytes) -> str:
        return hmac_sha256_hex(self._key, self._url.encode() + b"|" + body)

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        if not ct_equal(header(headers, "Authorization"), self.sign(body)):
            return False
        try:
            ts = json.loads(body).get("timestamp")
        except (ValueError, AttributeError):
            return False
        return ts is None or is_fresh(ts, self._clock(), self._max_age)

    def normalize(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        if not self.verify(headers, body):
            raise PermissionError("invalid signature")
        p = json.loads(body)
        data = p.get("data") or {}
        sn = data.get("ordersn")
        shop = p.get("shop_id")
        status = data.get("status")
        ident = f"{shop}:{sn}:{status}:{data.get('update_time', p.get('timestamp'))}" if sn else f"{shop}:{p.get('code')}:{p.get('timestamp')}"
        text = f"Shopee push code={p.get('code')} shop={shop}" + (f" order={sn} status={status}" if sn else "")
        return [
            Event(
                channel=self.channel,
                kind=EventKind.ORDER if sn else EventKind.WEBHOOK,
                external_id=ident,
                text=text,
                signature_verified=True,
                untrusted=True,
                metadata={"code": p.get("code"), "shop_id": shop, "ordersn": sn, "status": status},
            )
        ]

    def outbound_specs(self) -> list[ActionSpec]:
        return []
