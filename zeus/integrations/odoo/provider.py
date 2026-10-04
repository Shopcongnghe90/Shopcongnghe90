"""ToolProvider Odoo: ``erp.<model_với_dấu_gạch_dưới>.<method>`` (vd ``erp.sale_order.action_confirm``).

Chỉ phát hành spec cho các (model, method) có trong allowlist. Đọc = R0; ghi = R2, không hoàn tác tự động,
và provider TỰ kiểm tra approval APPROVED khớp action_id/args (ngoài lớp policy của ToolGateway).
"""

from __future__ import annotations

from typing import Any

from zeus.contracts.interfaces import ApprovalStore, PolicyDenied
from zeus.contracts.models import ActionResult, ActionSpec, RiskLevel, TypedAction
from zeus.integrations.common import require_approved
from zeus.integrations.odoo.client import READ_METHODS, ApprovalProof, OdooError, OdooJson2Client


def action_name(model: str, method: str) -> str:
    return f"erp.{model.replace('.', '_')}.{method}"


class OdooToolProvider:
    def __init__(self, client: OdooJson2Client, approvals: ApprovalStore | None = None) -> None:
        self._client = client
        self._approvals = approvals
        self._table: dict[str, tuple[str, str, str]] = {}
        acc = client.access
        for model in sorted(acc.read_models):
            for method in sorted(READ_METHODS):
                self._table[action_name(model, method)] = (model, method, "read")
        if approvals is not None:  # không có ApprovalStore => không phát hành spec ghi
            for model, methods in acc.write_methods.items():
                for method in sorted(methods):
                    self._table[action_name(model, method)] = (model, method, "write")

    def specs(self) -> list[ActionSpec]:
        out = []
        for name, (model, method, kind) in sorted(self._table.items()):
            out.append(
                ActionSpec(
                    name=name,
                    description=f"Odoo JSON-2 {model}.{method}" + (" (ghi, cần duyệt)" if kind == "write" else " (chỉ đọc)"),
                    risk=RiskLevel.R0 if kind == "read" else RiskLevel.R2,
                    external=kind == "write",
                    reversible=kind == "read",
                    idempotent=kind == "read",
                    owner="D",
                    required_capabilities=["odoo_client"],
                    input_schema={"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}, "params": {"type": "object"}}},
                )
            )
        return out

    async def execute(self, action: TypedAction, spec: ActionSpec) -> ActionResult:
        entry = self._table.get(action.name)
        if entry is None:
            raise PolicyDenied(f"ERP action không có trong allowlist: {action.name}")
        model, method, kind = entry
        proof: ApprovalProof | None = None
        if kind == "write":
            proof = ApprovalProof(await require_approved(self._approvals, action), action.action_id)
        if action.dry_run and kind == "write":
            return ActionResult(action_id=action.action_id, ok=True, output={"dry_run": True, "would_call": f"{model}.{method}"}, executed_by="control")
        ids: Any = action.args.get("ids")
        try:
            data = await self._client.call(model, method, ids=ids, params=action.args.get("params") or {}, approval=proof)
        except OdooError as exc:
            return ActionResult(action_id=action.action_id, ok=False, error=str(exc), executed_by="control")
        return ActionResult(action_id=action.action_id, ok=True, output={"result": data}, executed_by="control")
