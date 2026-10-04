"""Chuẩn hoá văn bản tiếng Việt: bỏ dấu, hạ chữ thường — dùng chung cho intent/risk (khớp có/không dấu)."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")


def fold(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return _WS.sub(" ", t.lower()).strip()
