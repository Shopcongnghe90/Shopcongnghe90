"""PII redaction trước khi gửi cloud LLM (ADR-017). Regex deterministic: điện thoại VN, email, CCCD, thẻ."""

from __future__ import annotations

import re

_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("pii.email", "[EMAIL]", re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")),
    ("pii.phone", "[PHONE]", re.compile(r"(?<!\d)(?:\+?84|0)(?:[ .-]?\d){9}(?!\d)")),
    ("pii.card", "[CARD]", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
    ("pii.national_id", "[ID]", re.compile(r"(?<!\d)\d{12}(?!\d)")),
]


def detect_pii(text: str) -> list[str]:
    return redact(text)[1]


def redact(text: str) -> tuple[str, list[str]]:
    """Trả (văn bản đã che, các lớp PII tìm thấy)."""
    found: list[str] = []
    for cls, repl, rx in _RULES:
        new = rx.sub(repl, text)
        if new != text:
            found.append(cls)
            text = new
    return text, sorted(found)
