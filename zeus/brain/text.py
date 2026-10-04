"""Tiện ích văn bản: bỏ dấu tiếng Việt, tách token, ước lượng token."""

from __future__ import annotations

import math
import re
import unicodedata

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def strip_accents(text: str) -> str:
    """Chữ thường + bỏ dấu tiếng Việt (đ -> d) để FTS 'simple' khớp cả có dấu lẫn không dấu."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower()


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(strip_accents(text))


def estimate_tokens(text: str) -> int:
    """Ước lượng thô, bảo thủ cho tiếng Việt (~3 ký tự/token)."""
    return max(1, math.ceil(len(text) / 3)) if text else 0
