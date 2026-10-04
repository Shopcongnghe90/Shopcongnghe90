"""Odoo 19 External JSON-2 API: ``POST {base}/json/2/<model>/<method>``.

- Header ``Authorization: bearer <api key>`` (key đọc lúc gọi, không lưu), ``X-Odoo-Database``.
- Body JSON: ``{"ids": [...], "context": {...}, <tham số tên>...}``.
- Mỗi lời gọi là MỘT transaction => chỉ gọi phương thức nghiệp vụ nguyên khối (vd ``action_confirm``).
- Allowlist: mặc định CHỈ ĐỌC. Phương thức ghi phải nằm trong ``OdooAccess.write_methods`` VÀ kèm ``ApprovalProof``.
  Các phương thức CRUD thô (write/create/unlink/...) bị chặn cứng, không thể đưa vào allowlist.
Chưa kiểm chứng với Odoo thật (marker ``live``); test dùng ``httpx.MockTransport``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from zeus.contracts.interfaces import PolicyDenied

READ_METHODS = frozenset({"search", "search_read", "read", "search_count", "fields_get", "name_search", "read_group"})
RAW_WRITE_METHODS = frozenset({"write", "create", "unlink", "copy", "execute", "execute_kw", "call_kw", "sudo", "with_user", "with_context", "invalidate_cache"})
_MODEL_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_METHOD_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class OdooError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Odoo HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class ApprovalProof:
    approval_id: str
    action_id: str


@dataclass(frozen=True)
class OdooAccess:
    """read_models: model được đọc. write_methods: {model: {phương thức nghiệp vụ được ghi}} — mặc định rỗng."""

    read_models: frozenset[str] = frozenset()
    write_methods: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for model, methods in self.write_methods.items():
            bad = {m for m in methods if m in READ_METHODS or m in RAW_WRITE_METHODS or m.startswith("_") or not _METHOD_RE.match(m)}
            if bad or not _MODEL_RE.match(model):
                raise ValueError(f"allowlist ghi không hợp lệ cho {model}: {sorted(bad)}")
        for m in self.read_models:
            if not _MODEL_RE.match(m):
                raise ValueError(f"model không hợp lệ: {m}")


class OdooJson2Client:
    def __init__(
        self,
        base_url: str,
        database: str,
        api_key: Callable[[], str | None],
        access: OdooAccess,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._db, self._key, self.access = database, api_key, access
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    def classify(self, model: str, method: str) -> str:
        """'read' | 'write'; ném PolicyDenied nếu không nằm trong allowlist."""
        if not _MODEL_RE.match(model) or not _METHOD_RE.match(method):
            raise PolicyDenied("model/method không hợp lệ")
        if method in RAW_WRITE_METHODS:
            raise PolicyDenied(f"phương thức thô {method} bị chặn; dùng phương thức nghiệp vụ nguyên khối")
        if method in READ_METHODS:
            if model in self.access.read_models:
                return "read"
            raise PolicyDenied(f"model {model} không có trong allowlist đọc")
        if method in self.access.write_methods.get(model, frozenset()):
            return "write"
        raise PolicyDenied(f"{model}.{method} không có trong allowlist")

    async def call(
        self,
        model: str,
        method: str,
        *,
        ids: list[int] | None = None,
        params: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        approval: ApprovalProof | None = None,
    ) -> Any:
        kind = self.classify(model, method)
        if kind == "write" and approval is None:
            raise PolicyDenied(f"{model}.{method} là thao tác ghi: cần ApprovalProof")
        key = self._key()
        if not key:
            raise PolicyDenied("chưa cấu hình API key Odoo")
        body: dict[str, Any] = dict(params or {})
        if "ids" in body or "context" in body:
            raise ValueError("params không được chứa khoá ids/context")
        if ids is not None:
            body["ids"] = ids
        body["context"] = dict(context or {"lang": "vi_VN"})
        resp = await self._http.post(
            f"{self._base}/json/2/{model}/{method}",
            json=body,
            headers={"Authorization": f"bearer {key}", "X-Odoo-Database": self._db, "Content-Type": "application/json; charset=utf-8"},
        )
        if resp.status_code >= 300:
            try:
                msg = str(resp.json().get("message") or resp.json().get("name") or "")[:300]
            except (ValueError, AttributeError):
                msg = resp.text[:300]
            raise OdooError(resp.status_code, msg.replace(key, "***"))
        return resp.json()
