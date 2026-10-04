"""Tiện ích chung cho ToolProvider tích hợp: kiểm chứng approval độc lập (defence in depth, ngoài ToolGateway)."""

from __future__ import annotations

from zeus.contracts.interfaces import ApprovalStore, PolicyDenied
from zeus.contracts.models import ApprovalStatus, TypedAction


async def require_approved(approvals: ApprovalStore | None, action: TypedAction) -> str:
    """Trả approval_id đã APPROVED cho đúng action_id; nếu không có => PolicyDenied. Không có store => luôn từ chối."""
    if approvals is None:
        raise PolicyDenied("không có ApprovalStore: thao tác cần duyệt bị từ chối")
    for a in await approvals.list(action.tenant_id, ApprovalStatus.APPROVED):
        if a.action.action_id == action.action_id and a.action.name == action.name and a.action.args == action.args:
            return a.approval_id
    raise PolicyDenied(f"thao tác {action.name} chưa có approval APPROVED khớp action_id/args")
