"""Site/SaaS: ``site.deploy`` (R2, tới VPS riêng của khách, không chung host ERP) và ``site.http_check`` (R0, sinh evidence HTTP).

Host đích phải nằm trong allowlist ``customer_hosts`` và KHÔNG thuộc ``forbidden_hosts`` (host ERP production).
http_check cũng chỉ gọi host trong allowlist (chống SSRF vào mạng nội bộ).
"""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from zeus.contracts.interfaces import ApprovalStore, PolicyDenied
from zeus.contracts.models import ActionResult, ActionSpec, RiskLevel, TypedAction
from zeus.integrations.common import require_approved


@runtime_checkable
class SiteDeployer(Protocol):
    """Triển khai artifact lên VPS (Phase 2: rsync/ssh qua worker ops). Trả metadata triển khai."""

    async def deploy(self, site_id: str, artifact_ref: str, host: str) -> dict[str, Any]: ...


class SiteToolProvider:
    def __init__(
        self,
        deployer: SiteDeployer,
        customer_hosts: Collection[str],
        forbidden_hosts: Collection[str] = (),
        approvals: ApprovalStore | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._d, self._hosts, self._forbidden, self._approvals = deployer, {h.lower() for h in customer_hosts}, {h.lower() for h in forbidden_hosts}, approvals
        self._http = http or httpx.AsyncClient(timeout=15.0, follow_redirects=False)

    def specs(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                name="site.deploy", description="Deploy site khách lên VPS riêng (R2, cần duyệt)", risk=RiskLevel.R2, external=True, reversible=False, owner="D",
                rollback_action=None, input_schema={"type": "object", "required": ["site_id", "artifact_ref", "host"]},
            ),
            ActionSpec(
                name="site.http_check", description="Kiểm tra HTTP site sau deploy (evidence)", risk=RiskLevel.R0, idempotent=True, owner="D",
                input_schema={"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}, "expect_status": {"type": "integer"}, "expect_text": {"type": "string"}}},
            ),
        ]

    def _host_ok(self, host: str) -> str:
        h = (host or "").lower()
        if h in self._forbidden:
            raise PolicyDenied("không được deploy/kiểm tra trên host ERP production")
        if h not in self._hosts:
            raise PolicyDenied(f"host {h!r} không nằm trong allowlist VPS khách")
        return h

    async def execute(self, action: TypedAction, spec: ActionSpec) -> ActionResult:
        a = action.args
        if action.name == "site.deploy":
            host = self._host_ok(str(a.get("host", "")))
            if not a.get("site_id") or not a.get("artifact_ref"):
                raise PolicyDenied("thiếu site_id/artifact_ref")
            await require_approved(self._approvals, action)
            if action.dry_run:
                return ActionResult(action_id=action.action_id, ok=True, output={"dry_run": True, "host": host}, executed_by="control")
            out = await self._d.deploy(str(a["site_id"]), str(a["artifact_ref"]), host)
            return ActionResult(action_id=action.action_id, ok=True, output={"host": host, **out}, executed_by="control")
        if action.name == "site.http_check":
            u = urlparse(str(a.get("url", "")))
            if u.scheme not in ("http", "https") or not u.hostname:
                raise PolicyDenied("url không hợp lệ")
            self._host_ok(u.hostname)
            t0 = time.perf_counter()
            try:
                r = await self._http.get(u.geturl())
            except httpx.HTTPError as exc:
                return ActionResult(action_id=action.action_id, ok=False, error=f"lỗi mạng: {type(exc).__name__}", executed_by="control")
            ms = int((time.perf_counter() - t0) * 1000)
            passed = r.status_code == int(a.get("expect_status", 200)) and (not a.get("expect_text") or str(a["expect_text"]) in r.text)
            return ActionResult(
                action_id=action.action_id, ok=passed, output={"status": r.status_code, "latency_ms": ms, "passed": passed},
                error=None if passed else "HTTP check không đạt", executed_by="control",
            )
        raise PolicyDenied(f"không hỗ trợ {action.name}")
