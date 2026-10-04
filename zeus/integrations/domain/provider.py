"""Tên miền: interface nhà đăng ký + ToolProvider ``domain.check`` (R0) / ``domain.register`` (R3).

Chặn mua (fail-closed, kiểm tra theo thứ tự):
1. đã thu tiền: ``PaymentVerifier`` (nguồn sự thật bên ngoài, KHÔNG tin cờ do LLM đưa vào args);
2. .vn: khách tự hoàn tất eKYC (NĐ 147/2024) — ``EkycVerifier``;
3. approval APPROVED khớp action_id/args (R3, không hoàn được).
Chưa có nhà đăng ký thật: ``DomainRegistrar`` là Protocol để nối sau (Phase 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from zeus.contracts.interfaces import ApprovalStore, PolicyDenied
from zeus.contracts.models import ActionResult, ActionSpec, RiskLevel, TypedAction
from zeus.integrations.common import require_approved

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


@dataclass(frozen=True)
class Availability:
    domain: str
    available: bool
    price_vnd: int | None = None


@dataclass(frozen=True)
class Registration:
    domain: str
    registrar_ref: str
    expires_on: str | None = None


@runtime_checkable
class DomainRegistrar(Protocol):
    async def check(self, domain: str) -> Availability: ...

    async def register(self, domain: str, years: int, customer_ref: str) -> Registration: ...


@runtime_checkable
class PaymentVerifier(Protocol):
    async def is_payment_confirmed(self, tenant_id: str, payment_ref: str, domain: str) -> bool: ...


@runtime_checkable
class EkycVerifier(Protocol):
    async def customer_ekyc_done(self, tenant_id: str, customer_ref: str, domain: str) -> bool: ...


def _valid(domain: object) -> str:
    if not isinstance(domain, str) or not _DOMAIN_RE.match(domain.lower()):
        raise PolicyDenied("tên miền không hợp lệ")
    return domain.lower()


class DomainToolProvider:
    def __init__(self, registrar: DomainRegistrar, payments: PaymentVerifier, ekyc: EkycVerifier, approvals: ApprovalStore | None) -> None:
        self._r, self._pay, self._ekyc, self._approvals = registrar, payments, ekyc, approvals

    def specs(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                name="domain.check", description="Kiểm tra tên miền còn trống", risk=RiskLevel.R0, idempotent=True, owner="D",
                input_schema={"type": "object", "required": ["domain"], "properties": {"domain": {"type": "string"}}},
            ),
            ActionSpec(
                name="domain.register", description="Đăng ký tên miền (R3: mất tiền, không hoàn; cần đã thu tiền + duyệt; .vn cần khách tự eKYC)",
                risk=RiskLevel.R3, external=True, reversible=False, owner="D",
                input_schema={"type": "object", "required": ["domain", "years", "customer_ref", "payment_ref"]},
            ),
        ]

    async def execute(self, action: TypedAction, spec: ActionSpec) -> ActionResult:
        a = action.args
        if action.name == "domain.check":
            av = await self._r.check(_valid(a.get("domain")))
            return ActionResult(action_id=action.action_id, ok=True, output={"domain": av.domain, "available": av.available, "price_vnd": av.price_vnd}, executed_by="control")
        if action.name != "domain.register":
            raise PolicyDenied(f"không hỗ trợ {action.name}")
        domain = _valid(a.get("domain"))
        years, customer, pay = a.get("years"), a.get("customer_ref"), a.get("payment_ref")
        if not (isinstance(years, int) and 1 <= years <= 10) or not customer or not pay:
            raise PolicyDenied("thiếu/sai years, customer_ref hoặc payment_ref")
        if not await self._pay.is_payment_confirmed(action.tenant_id, str(pay), domain):
            raise PolicyDenied("payment_confirmed=false: chưa thu tiền, không đăng ký tên miền")
        if domain.endswith(".vn") and not await self._ekyc.customer_ekyc_done(action.tenant_id, str(customer), domain):
            raise PolicyDenied("tên miền .vn: khách hàng phải tự hoàn tất eKYC trước")
        await require_approved(self._approvals, action)
        if action.dry_run:
            return ActionResult(action_id=action.action_id, ok=True, output={"dry_run": True, "domain": domain}, executed_by="control")
        reg = await self._r.register(domain, years, str(customer))
        return ActionResult(
            action_id=action.action_id, ok=True, output={"domain": reg.domain, "registrar_ref": reg.registrar_ref, "expires_on": reg.expires_on}, executed_by="control"
        )
