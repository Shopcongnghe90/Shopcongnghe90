"""Phase 2 (2): idempotency bền cho action external trong ToolGateway (bảng tool_idempotency, migration 102).
Hồi quy chính nằm ở test_reliability_round2.py::test_r7_*; file này thêm: gọi ĐỒNG THỜI cùng khoá và khoá suy ra từ nội dung."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.control_plane.conftest import make_rig, worker_specs
from zeus.contracts.interfaces import ApprovalRequired
from zeus.contracts.models import ApprovalDecision, ApprovalStatus, TypedAction
from zeus.policy.gateway import DefaultToolGateway, RemoteToolProvider
from zeus.policy.idempotency import PgIdempotency
from zeus.storage.migrate import apply_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
pytestmark = [pytest.mark.pg]


@pytest.fixture()
async def rig(models_cfg, policy_cfg):
    return await make_rig(models_cfg, policy_cfg)


async def _approved(rig, gw, act):  # noqa: ANN001, ANN202
    with pytest.raises(ApprovalRequired) as ei:
        await gw.execute(act)
    await rig.approvals.decide(ApprovalDecision(approval_id=ei.value.request.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:h"))


async def test_concurrent_calls_with_same_key_execute_once(pg_dsn, rig):
    apply_migrations(pg_dsn, MIGRATIONS)
    gws = [DefaultToolGateway([rig.control_tool, RemoteToolProvider(worker_specs())], rig.gateway.policy, rig.approvals, rig.audit, idempotency=PgIdempotency(pg_dsn)) for _ in range(3)]
    act = TypedAction(name="channel.send_message", args={"text": "Chào"}, idempotency_key="tsk_c:send")
    await _approved(rig, gws[0], act)
    results = await asyncio.gather(*(gws[i % 3].execute(act) for i in range(9)))  # 3 "tiến trình" gateway, 9 lời gọi đồng thời
    assert len(rig.control_tool.executed) == 1  # đúng một lần thực thi hành động ngoài
    assert sum(1 for r in results if r.ok) >= 1 and all(r.ok or "không rõ" in (r.error or "") for r in results)  # còn lại: trả kết quả cũ hoặc báo "đang/không rõ"
    again = await gws[1].execute(act)
    assert again.ok and len(rig.control_tool.executed) == 1


async def test_key_derived_from_content_for_external_actions_without_explicit_key(pg_dsn, rig):
    apply_migrations(pg_dsn, MIGRATIONS)
    gw = DefaultToolGateway([rig.control_tool, RemoteToolProvider(worker_specs())], rig.gateway.policy, rig.approvals, rig.audit, idempotency=PgIdempotency(pg_dsn))
    a1 = TypedAction(name="channel.send_message", args={"text": "Giống nhau"})
    await _approved(rig, gw, a1)
    assert (await gw.execute(a1)).ok
    a2 = TypedAction(name="channel.send_message", args={"text": "Giống nhau"})  # action_id khác, cùng nội dung
    await _approved(rig, gw, a2)  # duyệt theo action_id nên cần duyệt riêng; nhưng KHÔNG gửi lần hai
    r2 = await gw.execute(a2)
    assert r2.ok and len(rig.control_tool.executed) == 1
