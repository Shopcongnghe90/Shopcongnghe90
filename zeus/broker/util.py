"""Tiện ích JSON cho structured output của model (bỏ code fence, lấy object đầu tiên)."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_json_object(text: str) -> dict[str, Any]:
    """Trả dict từ output của model; ném ValueError nếu không phải JSON object."""
    t = _FENCE.sub("", text.strip())
    try:
        val = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model output is not JSON") from None
        try:
            val = json.loads(t[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"model output is not valid JSON: {exc}") from None
    if not isinstance(val, dict):
        raise ValueError("model output JSON is not an object")
    return val
