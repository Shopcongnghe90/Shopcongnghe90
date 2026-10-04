"""Kiểm tra tham số có kiểu (typed) của action đọc/kiểm chứng chạy trên worker: ``http.check``/``http.fetch``,
``file.checksum``, ``repo.tests.run``. Dùng ở HAI chỗ để cùng một luật:

- Planner (``zeus/planning``): chỉ sinh tham số từ entity của Intent khi qua kiểm tra (không qua => bước thủ công).
- Policy Engine (``P-PARAM-ALLOWLIST``): mọi action vào Tool Gateway (kể cả do model/API đề xuất) bị DENY nếu tham số
  ngoài allowlist. Worker vẫn kiểm lần nữa theo cấu hình riêng của nó (phòng thủ nhiều lớp).

Allowlist rỗng => từ chối hết (fail closed). Không phân giải DNS ở control; host dạng IP và ``localhost`` bị loại.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from zeus.contracts.models import ZeusModel

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9_./-]{0,200}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_URL_PORTS = {None, 80, 443}
MAX_URL = 2000

HTTP_ACTIONS = ("http.check", "http.fetch")


class ParamPolicy(ZeusModel):
    """Allowlist tham số typed action (cấu hình ở ``params:`` trong config/policy.yaml)."""

    http_allow_domains: list[str] = Field(default_factory=list)  # "shop.vn" (chính nó + miền con) hoặc ".shop.vn" (chỉ miền con)
    file_roots: list[str] = Field(default_factory=list)  # tiền tố thư mục tuyệt đối được băm checksum
    repo_roots: list[str] = Field(default_factory=list)  # tiền tố thư mục tuyệt đối được chạy pytest

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ParamPolicy":
        raw = raw or {}
        return cls(
            http_allow_domains=[str(x).strip().lower() for x in raw.get("http_allow_domains", []) if str(x).strip()],
            file_roots=[posixpath.normpath(str(x)) for x in raw.get("file_roots", []) if str(x).startswith("/")],
            repo_roots=[posixpath.normpath(str(x)) for x in raw.get("repo_roots", []) if str(x).startswith("/")],
        )

    # ------------------------------------------------------------------ từng loại tham số
    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if not host or host == "localhost" or host.endswith(".localhost") or not _DOMAIN_RE.match(host):
            return False  # IP literal, tên không phải FQDN công khai
        for d in self.http_allow_domains:
            bare = d.lstrip(".")
            if (d.startswith(".") and host.endswith("." + bare)) or (not d.startswith(".") and (host == bare or host.endswith("." + bare))):
                return True
        return False

    def url(self, value: Any) -> str | None:
        """URL http(s) hợp lệ (không userinfo, cổng chuẩn, host trong allowlist) -> URL đã bỏ fragment; sai => None."""
        if not isinstance(value, str) or not value or len(value) > MAX_URL or any(c.isspace() or ord(c) < 32 for c in value):
            return None
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        if port not in _URL_PORTS or not self.host_allowed(parts.hostname):
            return None
        try:
            ipaddress.ip_address(parts.hostname)
            return None
        except ValueError:
            pass
        return parts._replace(fragment="").geturl()

    def domain(self, value: Any) -> str | None:
        """Tên miền hợp lệ trong allowlist -> URL gốc https://<domain>/."""
        if not isinstance(value, str):
            return None
        d = value.strip().lower().rstrip(".")
        return f"https://{d}/" if _DOMAIN_RE.match(d) and self.host_allowed(d) else None

    @staticmethod
    def _path_under(value: Any, roots: list[str]) -> str | None:
        if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or len(value) > 1024 or ".." in value.split("/"):
            return None
        p = posixpath.normpath(value)
        return p if any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots) else None

    def file_path(self, value: Any) -> str | None:
        return self._path_under(value, self.file_roots)

    def repo_path(self, value: Any) -> str | None:
        return self._path_under(value, self.repo_roots)

    @staticmethod
    def sha256(value: Any) -> str | None:
        return value.lower() if isinstance(value, str) and _SHA256_RE.match(value.lower()) else None

    @staticmethod
    def target(value: Any) -> str | None:
        if not isinstance(value, str) or not _TARGET_RE.match(value) or value.startswith("-") or value.startswith("/") or ".." in value.split("/"):
            return None
        return value

    # ------------------------------------------------------------------ Policy Engine
    def violations(self, name: str, args: dict[str, Any]) -> list[str]:
        """Lý do tham số vi phạm allowlist (rỗng = hợp lệ hoặc action không thuộc nhóm có allowlist)."""
        if name in HTTP_ACTIONS:
            return [] if self.url(args.get("url")) else ["url ngoài allowlist domain hoặc không hợp lệ (http(s), cổng 80/443, không userinfo, không IP)"]
        if name == "file.checksum":
            out = [] if self.file_path(args.get("path")) else ["path ngoài file_roots hoặc không hợp lệ"]
            if args.get("expected_sha256") is not None and self.sha256(args.get("expected_sha256")) is None:
                out.append("expected_sha256 không phải SHA-256 hex")
            return out
        if name == "repo.tests.run":
            out = [] if self.repo_path(args.get("repo")) else ["repo ngoài repo_roots hoặc không hợp lệ"]
            if "target" in args and self.target(args.get("target")) is None:
                out.append("target không hợp lệ")
            return out
        return []
