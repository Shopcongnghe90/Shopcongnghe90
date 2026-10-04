"""Typed Tool Gateway: cửa duy nhất cho side-effect.

validate JSON Schema -> tenant -> idempotency -> policy -> approval gate -> execute (timeout) -> audit_log.
LLM không có shell tự do: chỉ action có ActionSpec trong registry mới chạy được.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Protocol

from zeus.contracts.interfaces import ApprovalRequired, PolicyDenied, ToolProvider
from zeus.contracts.models import (
    ActionResult,
    ActionSpec,
    ApprovalRequest,
    ApprovalStatus,
    PolicyDecision,
    PolicyEffect,
    Task,
    TypedAction,
    utcnow,
)
from zeus.policy.engine import DefaultPolicyEngine, effective_risk
from zeus.policy.idempotency import IdempotencyStore, InMemoryIdempotency
from zeus.policy.jsonschema import validate


class AuditSink(Protocol):
    async def record(
        self, *, tenant_id: str, actor: str, action: str, subject_id: str | None = None, risk: str | None = None,
        task_id: str | None = None, trace_id: str | None = None, details: dict[str, Any] | None = None, subject_type: str = "action",
    ) -> None: ...


class InMemoryAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record(self, **row: Any) -> None:
        self.rows.append(row)


class PgAudit:
    """Ghi vào audit_log (append-only, migration 000)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def record(
        self, *, tenant_id: str, actor: str, action: str, subject_id: str | None = None, risk: str | None = None,
        task_id: str | None = None, trace_id: str | None = None, details: dict[str, Any] | None = None, subject_type: str = "action",
    ) -> None:
        from zeus.storage.db import aconnect

        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO audit_log (tenant_id, actor, action, subject_type, subject_id, risk, trace_id, task_id, details)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (tenant_id, actor, action, subject_type, subject_id, risk, trace_id, task_id, json.dumps(details or {}, default=str)),
            )


def derive_idempotency_key(action: TypedAction) -> str:
    body = json.dumps({"t": action.tenant_id, "n": action.name, "a": action.args}, sort_keys=True, default=str)
    return "idem_" + hashlib.sha256(body.encode()).hexdigest()[:32]


class RemoteToolProvider:
    """Đăng ký ActionSpec cho action do WORKER thực thi (policy/approval/audit vẫn ở control).
    Control không bao giờ execute trực tiếp: dispatch qua AssignmentQueue (orchestration)."""

    remote = True

    def __init__(self, specs: Sequence[ActionSpec]) -> None:
        self._specs = list(specs)

    def specs(self) -> list[ActionSpec]:
        return list(self._specs)

    async def execute(self, action: TypedAction, spec: ActionSpec) -> ActionResult:
        raise PolicyDenied(f"{spec.name} là action của worker; phải dispatch qua AssignmentQueue")


class DefaultToolGateway:
    def __init__(
        self,
        providers: Sequence[ToolProvider],
        policy: DefaultPolicyEngine,
        approvals: Any,
        audit: AuditSink | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self.policy = policy
        self.idempotency: IdempotencyStore = idempotency or InMemoryIdempotency()
        self.approvals = approvals
        self.audit: AuditSink = audit or InMemoryAudit()
        self._routes: dict[str, tuple[ToolProvider, ActionSpec]] = {}
        for p in providers:
            for s in p.specs():
                if s.name in self._routes:
                    raise ValueError(f"duplicate action spec {s.name}")
                self._routes[s.name] = (p, s)

    # ---------------------------------------------------------------- registry
    def list_actions(self) -> list[ActionSpec]:
        return [s for _, s in self._routes.values()]

    def spec(self, name: str) -> ActionSpec | None:
        r = self._routes.get(name)
        return r[1] if r else None

    # ---------------------------------------------------------------- pipeline
    async def _audit(self, action: TypedAction, event: str, spec: ActionSpec | None, details: dict[str, Any]) -> None:
        await self.audit.record(
            tenant_id=action.tenant_id,
            actor=f"agent:{action.requested_by}",
            action=event,
            subject_id=action.action_id,
            risk=(effective_risk(spec, self.policy.config).value if spec else None),
            task_id=action.task_id,
            trace_id=action.trace.trace_id if action.trace else None,
            details={"name": action.name, **details},
        )

    async def authorize(self, action: TypedAction, task: Task | None = None) -> tuple[ActionSpec, PolicyDecision]:
        """Mọi kiểm tra trước thực thi. Ném PolicyDenied / ApprovalRequired; trả (spec, decision) nếu được chạy."""
        route = self._routes.get(action.name)
        if route is None:
            await self._audit(action, "policy.denied", None, {"rule_ids": ["G-UNKNOWN-ACTION"]})
            raise PolicyDenied(f"unknown action {action.name}")
        spec = route[1]
        errs = validate(action.args, spec.input_schema) if spec.input_schema else []
        if errs:
            await self._audit(action, "policy.denied", spec, {"rule_ids": ["G-SCHEMA"], "errors": errs[:5]})
            raise PolicyDenied("invalid args: " + "; ".join(errs[:5]))
        decision = self.policy.evaluate(action, spec, task)
        if decision.effect is PolicyEffect.DENY:
            await self._audit(action, "policy.denied", spec, {"rule_ids": decision.rule_ids, "reasons": decision.reasons})
            raise PolicyDenied("; ".join(decision.reasons))
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            await self._require_approval(action, spec, decision)
        return spec, decision

    async def _require_approval(self, action: TypedAction, spec: ActionSpec, decision: PolicyDecision) -> None:
        found = [a for a in await self.approvals.list(action.tenant_id) if a.action.action_id == action.action_id]
        for a in found:
            if a.status is ApprovalStatus.APPROVED and a.action.name == action.name and a.action.args == action.args:
                return
        pending = [a for a in found if a.status is ApprovalStatus.PENDING]
        if pending:
            raise ApprovalRequired(pending[0])
        req = await self.approvals.request(
            ApprovalRequest(
                tenant_id=action.tenant_id,
                task_id=action.task_id,
                action=action,
                risk=effective_risk(spec, self.policy.config),
                summary_vi=f"Duyệt thao tác {spec.name}: {spec.description or 'không mô tả'}",
                expires_at=utcnow() + timedelta(seconds=self.policy.config.approval_timeout_s),
            )
        )
        await self._audit(action, "approval.requested", spec, {"approval_id": req.approval_id, "rule_ids": decision.rule_ids})
        raise ApprovalRequired(req)

    async def execute(self, action: TypedAction, task: Task | None = None) -> ActionResult:
        spec, decision = await self.authorize(action, task)
        provider = self._routes[action.name][0]
        if getattr(provider, "remote", False):
            raise PolicyDenied(f"{spec.name} là action của worker; phải dispatch qua AssignmentQueue")
        key = action.idempotency_key or (derive_idempotency_key(action) if spec.external else None)
        if key:
            state, prior = await self.idempotency.begin(action.tenant_id, key)
            if state == "done" and prior is not None:
                await self._audit(action, "tool.idempotent_replay", spec, {"key": key})
                return prior
            if state == "pending":
                # lần trước đã chiếm khoá nhưng không có kết quả (process chết/timeout/đang chạy): không chạy lại hành động ngoài
                await self._audit(action, "tool.idempotent_unknown", spec, {"key": key})
                return ActionResult(
                    action_id=action.action_id, ok=False, executed_by="control",
                    error=f"không rõ kết quả lần thực thi trước (idempotency {key}); kiểm tra thủ công trước khi chạy lại",
                )
        timed_out = False
        try:
            result = await asyncio.wait_for(provider.execute(action, spec), timeout=spec.timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            result = ActionResult(action_id=action.action_id, ok=False, error=f"timeout after {spec.timeout_s}s", executed_by="control")
        except Exception as exc:  # noqa: BLE001 - tool lỗi không được làm sập gateway
            result = ActionResult(action_id=action.action_id, ok=False, error=f"{type(exc).__name__}: {exc}", executed_by="control")
        if key:
            if result.ok or timed_out:  # timeout: có thể đã có tác dụng => ghi nhận, KHÔNG cho chạy lại tự động
                await self.idempotency.finish(action.tenant_id, key, result)
            else:  # thất bại đã biết: nhả khoá để retry
                await self.idempotency.release(action.tenant_id, key)
        await self._audit(action, "tool.executed" if result.ok else "tool.failed", spec, {"ok": result.ok, "rule_ids": decision.rule_ids, "key": key})
        return result


__all__ = ["DefaultToolGateway", "RemoteToolProvider", "InMemoryAudit", "PgAudit", "AuditSink", "derive_idempotency_key"]
