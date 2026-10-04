"""Tiện ích chung cho adapter kênh: so sánh hằng thời gian, HMAC, tách tin, chống trùng."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Protocol

Clock = Callable[[], float]


def ct_equal(a: str, b: str) -> bool:
    """So sánh chuỗi hằng thời gian (không rò độ dài khớp)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hmac_sha256_hex(key: str | bytes, msg: bytes) -> str:
    k = key.encode("utf-8") if isinstance(key, str) else key
    return hmac.new(k, msg, hashlib.sha256).hexdigest()


def header(headers: Mapping[str, str], name: str) -> str:
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return ""


def to_seconds(ts: float | int | str | None) -> float | None:
    """Timestamp giây hoặc mili-giây -> giây. None nếu không hợp lệ."""
    try:
        v = float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 1e11 else v


def is_fresh(ts: float | int | str | None, now: float, max_age_s: float) -> bool:
    sec = to_seconds(ts)
    return sec is not None and abs(now - sec) <= max_age_s


def split_text(text: str, limit: int = 2000) -> list[str]:
    """Chia văn bản thành các phần <= limit ký tự, ưu tiên cắt ở xuống dòng/khoảng trắng.

    Không bao giờ vượt limit, không tạo phần rỗng, không cắt đôi ký tự (làm việc trên str Unicode).
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    out: list[str] = []
    rest = text.strip()
    while rest:
        if len(rest) <= limit:
            out.append(rest)
            break
        window = rest[:limit]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:  # không có điểm cắt đẹp ở nửa sau => cắt cứng
            cut = limit
        part = rest[:cut].rstrip()
        if part:
            out.append(part)
        rest = rest[cut:].lstrip()
    return out


class Dedupe(Protocol):
    async def claim(self, key: str) -> bool:
        """True nếu đây là lần đầu thấy key (và đã đánh dấu); False nếu trùng."""
        ...

    async def release(self, key: str) -> None: ...


class MemoryDedupe:
    def __init__(self, ttl_s: float = 86400, max_items: int = 100_000, clock: Clock = time.time) -> None:
        self._ttl, self._max, self._clock = ttl_s, max_items, clock
        self._seen: OrderedDict[str, float] = OrderedDict()

    async def claim(self, key: str) -> bool:
        now = self._clock()
        while self._seen and (len(self._seen) >= self._max or next(iter(self._seen.values())) < now - self._ttl):
            self._seen.popitem(last=False)
        if key in self._seen:
            return False
        self._seen[key] = now
        return True

    async def release(self, key: str) -> None:
        self._seen.pop(key, None)


__all__ = ["Clock", "Dedupe", "MemoryDedupe", "ct_equal", "header", "hmac_sha256_hex", "is_fresh", "split_text", "time"]
