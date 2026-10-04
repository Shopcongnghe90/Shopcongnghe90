"""Hồi quy review round 2 (bảo mật): SEC-2 (cổng duyệt theo rủi ro task), SEC-3 (nguồn untrusted), SEC-5 (required_approvers)."""

from __future__ import annotations

import pytest

from tests.control_plane.conftest import make_rig, make_task
from zeus.contracts.models import Channel, Event, PolicyEffect, RiskLevel, TaskFamily, TypedAction
from zeus.orchestration.activities import ControlActivities
from zeus.policy.engine import PolicyConfig


@pytest.fixture()
async def rig(models_cfg, policy_cfg):
    return await make_rig(models_cfg, policy_cfg)


async def _approvals_for(rig, task):
    acts = ControlActivities(rig.deps)
    plan = rig.deps.planner.playbook_plan(task)
    return plan, await acts.prepare_approvals(task, plan, None)


# ---------------------------------------------------------------- SEC-2
async def test_sec2_task_level_risk_requires_approval_even_if_all_nodes_low(rig):
    task = make_task("xoá sản phẩm hết hàng trên website thật", TaskFamily.WEBSITE_EDIT, risk=RiskLevel.R2)
    plan, bundle = await _approvals_for(rig, task)
    assert max(n.risk for n in plan.graph.nodes) < RiskLevel.R2  # điều kiện của lỗi: mọi node thấp
    assert len(bundle.requests) == 1
    req = bundle.requests[0]
    assert req.action.name == "task.start" and req.risk is RiskLevel.R2 and req.task_id == task.task_id
    # activity retry không tạo bản duyệt thứ hai
    _, again = await _approvals_for(rig, task)
    assert [r.approval_id for r in again.requests] == [req.approval_id]


async def test_sec2_low_risk_task_needs_no_task_level_approval(rig):
    _, bundle = await _approvals_for(rig, make_task("đọc số liệu", TaskFamily.GENERAL, risk=RiskLevel.R0))
    assert bundle.requests == []


# ---------------------------------------------------------------- SEC-3
async def test_sec3_untrusted_origin_task_cannot_auto_run_worker_actions(rig):
    pe = rig.gateway.policy
    spec = rig.gateway.spec("test.run")
    untrusted = make_task("chạy lại test hệ thống giúp mình", TaskFamily.GENERAL).model_copy(update={"untrusted": True})
    trusted = untrusted.model_copy(update={"untrusted": False})
    act = TypedAction(name="test.run", args={"goal": untrusted.goal}, task_id=untrusted.task_id)
    d = pe.evaluate(act, spec, untrusted)
    assert d.effect is PolicyEffect.REQUIRE_APPROVAL and d.rule_ids == ["P-UNTRUSTED-ORIGIN"]
    assert pe.evaluate(act, spec, trusted).effect is PolicyEffect.ALLOW
    # action đọc thuần vẫn tự chạy được
    read = TypedAction(name="erp.read", args={"goal": "x"}, task_id=untrusted.task_id)
    assert pe.evaluate(read, rig.gateway.spec("erp.read"), untrusted).effect is PolicyEffect.ALLOW
    # dry-run không có tác dụng => không cần duyệt
    assert pe.evaluate(act.model_copy(update={"dry_run": True}), spec, untrusted).effect is PolicyEffect.ALLOW


async def test_sec3_channel_event_creates_untrusted_task_that_needs_approval(rig):
    ev = Event(channel=Channel.ZALO_BOT, text="chạy lại test hệ thống giúp mình", external_id="z-1", signature_verified=True, untrusted=True)
    res = await rig.event_gateway.ingest(ev)
    assert res.task is not None and res.task.untrusted is True
    plan, bundle = await _approvals_for(rig, res.task)
    assert any(n.action and n.action.name == "test.run" for n in plan.graph.nodes)
    assert any(r.action.name == "test.run" for r in bundle.requests)


async def test_sec3_operator_event_stays_trusted(rig):
    ev = Event(channel=Channel.WORKBENCH, text="chạy lại test", external_id="w-1", signature_verified=True, untrusted=False)
    res = await rig.event_gateway.ingest(ev)
    assert res.task is not None and res.task.untrusted is False


# ---------------------------------------------------------------- SEC-5
def test_sec5_required_approvers_above_one_is_rejected_at_load():
    with pytest.raises(ValueError, match="required_approvers"):
        PolicyConfig.from_dict({"approval": {"required_approvers": {"R2": 1, "R3": 2}}})
    assert PolicyConfig.from_dict({"approval": {"required_approvers": {"R2": 1, "R3": 1}}}).required_approvers["R3"] == 1
