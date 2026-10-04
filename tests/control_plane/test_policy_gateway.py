from __future__ import annotations

import pytest

from tests.control_plane.conftest import make_rig, make_task
from zeus.contracts.interfaces import ApprovalRequired, PolicyDenied
from zeus.contracts.models import (
    ActionSpec,
    ApprovalDecision,
    ApprovalStatus,
    PolicyEffect,
    RiskLevel,
    TypedAction,
)
from zeus.policy.budget import BudgetExceeded, BudgetPolicy, InMemoryBudgetLedger, LedgerEntry
from zeus.policy.engine import DefaultPolicyEngine
from zeus.policy.jsonschema import validate
from zeus.policy.redact import detect_pii, redact


def spec(name="erp.read", risk=RiskLevel.R0, **kw) -> ActionSpec:
    return ActionSpec(name=name, risk=risk, **kw)


def act(name="erp.read", tenant="zeusvn", **args) -> TypedAction:
    return TypedAction(name=name, tenant_id=tenant, args=args)


def test_policy_r0_r1_allow_r2_r3_require_approval_with_rule_ids(policy_cfg):
    p = DefaultPolicyEngine(policy_cfg)
    d0 = p.evaluate(act(), spec())
    d1 = p.evaluate(act("code.apply_patch"), spec("code.apply_patch", RiskLevel.R1))
    d2 = p.evaluate(act("db.migrate"), spec("db.migrate", RiskLevel.R2))
    d3 = p.evaluate(act("fin.close_books"), spec("fin.close_books", RiskLevel.R3))
    assert [d.effect for d in (d0, d1)] == [PolicyEffect.ALLOW] * 2
    assert [d.effect for d in (d2, d3)] == [PolicyEffect.REQUIRE_APPROVAL] * 2
    assert all(d.rule_ids and d.reasons for d in (d0, d1, d2, d3))
    assert d2.required_approvers == 1 and d2.rule_ids == ["P-APPROVAL-R2"]


def test_policy_external_is_at_least_r2_and_dry_run_skips_approval(policy_cfg):
    p = DefaultPolicyEngine(policy_cfg)
    s = spec("channel.send_message", RiskLevel.R1, external=True)
    assert p.evaluate(act("channel.send_message"), s).effect is PolicyEffect.REQUIRE_APPROVAL
    dry = act("channel.send_message").model_copy(update={"dry_run": True})
    assert p.evaluate(dry, s).effect is PolicyEffect.ALLOW


def test_policy_denylist_tenant_and_dangerous_args(policy_cfg):
    p = DefaultPolicyEngine(policy_cfg)
    d = p.evaluate(act("shell.run", cmd="ls"), spec("shell.run"))
    assert d.effect is PolicyEffect.DENY and d.rule_ids == ["P-DENYLIST"]
    # tenant mặc định chỉ được action đọc
    d = p.evaluate(act("code.apply_patch", tenant="khach-a"), spec("code.apply_patch", RiskLevel.R1))
    assert d.effect is PolicyEffect.DENY and d.rule_ids == ["P-TENANT-ALLOWLIST"] and d.allow_cloud is False
    assert p.evaluate(act("erp.read", tenant="khach-a"), spec()).effect is PolicyEffect.ALLOW
    d = p.evaluate(act("erp.read", query="DROP DATABASE shop"), spec())
    assert d.effect is PolicyEffect.DENY and d.rule_ids == ["P-DANGEROUS-ARGS"]
    t = make_task(tenant_id="khach-a")
    assert p.evaluate(act("erp.read"), spec(), t).rule_ids == ["P-TENANT-MISMATCH"]


def test_policy_task_budget(policy_cfg):
    spent = {"x": 3.0}
    p = DefaultPolicyEngine(policy_cfg, lambda tid: spent["x"])
    t = make_task(budget_usd=2.0)
    d = p.evaluate(act(), spec(), t)
    assert d.effect is PolicyEffect.DENY and d.rule_ids == ["P-BUDGET-TASK"]
    spent["x"] = 0.5
    d = p.evaluate(act(), spec(), t)
    assert d.effect is PolicyEffect.ALLOW and d.budget_remaining_usd == pytest.approx(1.5)


# ----------------------------------------------------------------------- tool gateway
async def test_gateway_schema_validation_and_unknown_action(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    with pytest.raises(PolicyDenied, match="missing required 'goal'"):
        await rig.gateway.execute(act("erp.read"))
    with pytest.raises(PolicyDenied, match="unknown action"):
        await rig.gateway.execute(act("nope.nope"))
    with pytest.raises(PolicyDenied, match="unexpected property"):
        await rig.gateway.execute(act("channel.send_message", text="hi", extra=1))
    r = await rig.gateway.execute(act("erp.read", goal="x"))
    assert r.ok and rig.control_tool.executed
    acts = [row["action"] for row in rig.audit.rows]
    assert acts.count("policy.denied") == 3 and "tool.executed" in acts


async def test_gateway_approval_flow_and_no_r2_without_approval(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    a = act("channel.send_message", text="Chào anh")
    with pytest.raises(ApprovalRequired) as ei:
        await rig.gateway.execute(a)
    req = ei.value.request
    assert req.status is ApprovalStatus.PENDING and req.risk is RiskLevel.R2 and req.expires_at
    assert rig.control_tool.executed == []  # chưa chạy
    with pytest.raises(ApprovalRequired):  # gọi lại: tái dùng request đang chờ, không tạo thêm
        await rig.gateway.execute(a)
    assert len(await rig.approvals.list("zeusvn")) == 1
    await rig.approvals.decide(ApprovalDecision(approval_id=req.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:huyen"))
    # bị đổi tham số sau khi duyệt => không được dùng lại approval
    tampered = a.model_copy(update={"args": {"text": "Nội dung khác"}})
    with pytest.raises(ApprovalRequired):
        await rig.gateway.execute(tampered)
    r = await rig.gateway.execute(a)
    assert r.ok and len(rig.control_tool.executed) == 1
    assert any(row["action"] == "approval.requested" for row in rig.audit.rows)


async def test_gateway_rejected_approval_never_runs(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    a = act("channel.send_message", text="x")
    with pytest.raises(ApprovalRequired) as ei:
        await rig.gateway.execute(a)
    await rig.approvals.decide(ApprovalDecision(approval_id=ei.value.request.approval_id, status=ApprovalStatus.REJECTED, decided_by="human:x"))
    with pytest.raises(ApprovalRequired):
        await rig.gateway.execute(a)
    assert rig.control_tool.executed == []


async def test_gateway_external_idempotency_replay(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    a1 = act("channel.send_message", text="Chào")
    with pytest.raises(ApprovalRequired) as ei:
        await rig.gateway.execute(a1)
    await rig.approvals.decide(ApprovalDecision(approval_id=ei.value.request.approval_id, status=ApprovalStatus.APPROVED, decided_by="h"))
    r1 = await rig.gateway.execute(a1)
    # cùng nội dung, action_id khác (gửi lại): gateway dẫn xuất idempotency key => cần duyệt riêng, nhưng cùng key thì replay
    a2 = a1.model_copy(update={"idempotency_key": rig.audit.rows[-1]["details"]["key"]})
    r2 = await rig.gateway.execute(a2)
    assert r2 is r1 and len(rig.control_tool.executed) == 1
    assert any(row["action"] == "tool.idempotent_replay" for row in rig.audit.rows)


async def test_gateway_denylist_blocks_even_registered_action(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    with pytest.raises(PolicyDenied):
        await rig.gateway.execute(act("shell.run", cmd="id"))
    with pytest.raises(PolicyDenied, match="tenant"):
        await rig.gateway.execute(act("erp.read", goal="x"), make_task(tenant_id="khach-a"))


async def test_remote_actions_cannot_run_in_control(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    with pytest.raises(PolicyDenied, match="worker"):
        await rig.gateway.execute(act("test.run"))


# ----------------------------------------------------------------------- budget / redact / schema
async def test_budget_policy_task_and_day_caps():
    ledger = InMemoryBudgetLedger()
    b = BudgetPolicy(ledger, per_task_usd=1.0, per_day_usd=2.0)
    await b.check("zeusvn", "t1", 0.5)
    await ledger.record(LedgerEntry(tenant_id="zeusvn", task_id="t1", provider="x", model="m", usd=0.8))
    with pytest.raises(BudgetExceeded, match="task"):
        await b.check("zeusvn", "t1", 0.5)
    await ledger.record(LedgerEntry(tenant_id="zeusvn", task_id="t2", provider="x", model="m", usd=1.0))
    with pytest.raises(BudgetExceeded, match="ngày"):
        await b.check("zeusvn", "t3", 0.5)
    await b.check("zeusvn", "t3", 0.0)  # miễn phí (local) luôn qua


def test_redact_pii():
    text = "Gọi 0912 345 678 hoặc +84 912345678, mail an@shop.vn, CCCD 012345678901, thẻ 4111 1111 1111 1111"
    out, classes = redact(text)
    assert "0912" not in out and "an@shop.vn" not in out and "012345678901" not in out and "4111" not in out
    assert classes == ["pii.card", "pii.email", "pii.national_id", "pii.phone"]
    assert detect_pii("không có gì") == []


def test_jsonschema_subset():
    s = {"type": "object", "required": ["n"], "additionalProperties": False,
         "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}, "t": {"type": "array", "items": {"type": "string"}}, "e": {"enum": ["a", "b"]}}}
    assert validate({"n": 3, "t": ["x"], "e": "a"}, s) == []
    errs = validate({"n": 9, "t": [1], "e": "z", "q": 1}, s)
    assert len(errs) == 4
    assert validate({"n": True}, s)  # bool không phải integer
    assert validate({}, s) == ["$: missing required 'n'"]
