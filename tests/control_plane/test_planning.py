from __future__ import annotations

import json

import pytest

from tests.control_plane.conftest import llm_script, make_rig, make_task
from zeus.broker.broker import DefaultModelBroker
from zeus.broker.providers import FakeProvider
from zeus.contracts.interfaces import Critic, Judge, Planner
from zeus.contracts.models import (
    ActionResult,
    ContextPacket,
    EVAL_TASK_FAMILIES,
    EvidenceItem,
    EvidenceKind,
    EvidenceRecord,
    Outcome,
    ProviderKind,
    RiskLevel,
    Severity,
    TaskFamily,
    TaskGraph,
    TaskNode,
    TestRun,
    VerdictDecision,
    Plan,
)
from zeus.planning.critic import DefaultCritic
from zeus.planning.judge import DeterministicJudge, derive_outcome
from zeus.planning.planner import DefaultPlanner

A, L = ProviderKind.ANTHROPIC, ProviderKind.LOCAL


def ctx(task) -> ContextPacket:
    return ContextPacket(tenant_id=task.tenant_id, task_id=task.task_id, goal=task.goal)


def broker_for(cfg, policy, providers) -> DefaultModelBroker:
    return DefaultModelBroker(cfg, providers, policy=policy)


@pytest.mark.parametrize("family", list(TaskFamily))
async def test_playbook_plan_is_valid_dag_for_every_family_without_model(family, models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    task = make_task(family=family)
    plan = await rig.deps.planner.plan(task, ctx(task))
    assert isinstance(rig.deps.planner, Planner) and plan.planner == "playbook-1"
    TaskGraph.model_validate(plan.graph.model_dump())  # DAG hợp lệ
    assert plan.graph.nodes and all(n.acceptance for n in plan.graph.nodes) and plan.graph.ready_nodes(set())
    reg = {s.name for s in rig.gateway.list_actions()}
    assert all(n.action is None or n.action.name in reg for n in plan.graph.nodes)
    assert all(n.risk >= rig.gateway.spec(n.action.name).risk for n in plan.graph.nodes if n.action)


async def test_playbook_without_registry_degrades_to_actionless_nodes_with_assumption():
    plan = DefaultPlanner().playbook_plan(make_task(family=TaskFamily.BACKEND))
    assert all(n.action is None for n in plan.graph.nodes) and plan.assumptions


async def test_model_plan_is_validated_and_clamped_by_registry_risk(models_cfg, policy_cfg):
    nodes = [
        {"node_id": "m", "title": "Migrate", "depends_on": [], "action": "db.migrate", "risk": "R0", "acceptance": ["ok"]},  # model nói R0 nhưng spec là R2
        {"node_id": "v", "title": "Verify", "depends_on": ["m"], "action": "test.run", "risk": "R0", "acceptance": ["pass"]},
    ]
    f = FakeProvider(L, models=models_cfg.models, script=llm_script(nodes))
    rig = await make_rig(models_cfg, policy_cfg, broker=broker_for(models_cfg, policy_cfg, {L: f}))
    task = make_task(tenant_id="zeusvn")
    plan = await rig.deps.planner.plan(task, ctx(task))
    assert plan.planner == "local:local-default" and plan.rationale == "plan từ model"
    assert plan.graph.node("m").risk is RiskLevel.R2 and plan.route and plan.route.features["input_tokens"] > 0
    assert "<untrusted_data>" in f.calls[0].messages[0].content and task.goal not in (f.calls[0].system or "")


@pytest.mark.parametrize(
    "bad",
    [
        "không phải json",
        json.dumps({"nodes": []}),
        json.dumps({"nodes": [{"node_id": "a", "depends_on": ["a"], "action": None}]}),  # tự phụ thuộc
        json.dumps({"nodes": [{"node_id": "a", "depends_on": ["b"]}, {"node_id": "b", "depends_on": ["a"]}]}),  # chu trình
        json.dumps({"nodes": [{"node_id": "a", "action": "shell.exec_anything", "risk": "R0"}]}),  # ngoài registry
        json.dumps({"nodes": [{"node_id": "a", "risk": "R9"}]}),
    ],
)
async def test_bad_model_plan_falls_back_to_playbook(bad, models_cfg, policy_cfg):
    f = FakeProvider(L, models=models_cfg.models, script=lambda r: bad)
    rig = await make_rig(models_cfg, policy_cfg, broker=broker_for(models_cfg, policy_cfg, {L: f}))
    task = make_task()
    plan = await rig.deps.planner.plan(task, ctx(task))
    assert plan.planner == "playbook-1" and plan.graph.nodes


async def test_provider_down_planner_playbook_vs_require_model(models_cfg, policy_cfg):
    down = {L: FakeProvider(L, models=models_cfg.models, fail=True)}
    rig = await make_rig(models_cfg, policy_cfg, broker=broker_for(models_cfg, policy_cfg, down))
    task = make_task()
    assert (await rig.deps.planner.plan(task, ctx(task))).planner == "playbook-1"
    strict = DefaultPlanner(rig.deps.planner.broker, rig.gateway.list_actions, require_model=True)
    from zeus.contracts.interfaces import ProviderUnavailable

    with pytest.raises(ProviderUnavailable):
        await strict.plan(task, ctx(task))


# ----------------------------------------------------------------------- critic
async def test_critic_structural_issues(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    task = make_task()
    bad = Plan(task_id=task.task_id, planner="playbook-1", graph=TaskGraph(task_id=task.task_id, nodes=[
        TaskNode(node_id="x", title="Làm gì đó nguy hiểm", risk=RiskLevel.R2),  # R2 không action, không acceptance, không verify
    ]))
    c = await rig.deps.critic.critique(bad, ctx(task))
    assert isinstance(rig.deps.critic, Critic) and c.approve  # không blocker
    msgs = " | ".join(i.message for i in c.issues)
    assert "tiêu chí nghiệm thu" in msgs and "không gắn typed action" in msgs and "Thiếu bước kiểm chứng" in msgs
    unknown = bad.model_copy(update={"graph": TaskGraph(task_id=task.task_id, nodes=[TaskNode(node_id="y", title="t", action=__import__("zeus.contracts.models", fromlist=["TypedAction"]).TypedAction(name="ghost.act"), acceptance=["a"])])})
    c2 = await rig.deps.critic.critique(unknown, ctx(task))
    assert not c2.approve and c2.has_blocker


async def test_critic_is_cross_vendor_when_possible(models_cfg, policy_cfg):
    fa, fl = FakeProvider(A, models=models_cfg.models, script=llm_script()), FakeProvider(L, models=models_cfg.models, script=llm_script())
    broker = broker_for(models_cfg, policy_cfg, {A: fa, L: fl})
    rig = await make_rig(models_cfg, policy_cfg, broker=broker)
    task = make_task()
    plan = await DefaultPlanner(broker, rig.gateway.list_actions, preferred=[A]).plan(task, ctx(task))
    assert plan.planner.startswith("anthropic:")  # planner = Claude
    crit = await rig.deps.critic.critique(plan, ctx(task))
    assert crit.reviewer_provider is L and crit.reviewer.startswith("local:")
    assert not any("cùng hãng" in i.message for i in crit.issues)


async def test_critic_same_vendor_only_is_flagged_but_allowed(models_cfg, policy_cfg):
    fa = FakeProvider(A, models=models_cfg.models, script=llm_script())
    rig = await make_rig(models_cfg, policy_cfg, broker=broker_for(models_cfg, policy_cfg, {A: fa}))
    task = make_task()
    plan = await rig.deps.planner.plan(task, ctx(task))
    assert plan.planner.startswith("anthropic:")
    crit = await rig.deps.critic.critique(plan, ctx(task))
    assert crit.reviewer_provider is A and any("cùng hãng" in i.message for i in crit.issues)


async def test_critic_model_blocker_is_reported(models_cfg, policy_cfg):
    def script(r):
        if r.prompt_version == "critic-1":
            return json.dumps({"approve": False, "issues": [{"severity": "blocker", "message": "Thiếu backup", "node_id": "impl"}]})
        return llm_script()(r)

    fa, fl = FakeProvider(A, models=models_cfg.models, script=script), FakeProvider(L, models=models_cfg.models, script=script)
    rig = await make_rig(models_cfg, policy_cfg, broker=broker_for(models_cfg, policy_cfg, {A: fa, L: fl}))
    task = make_task()
    plan = await rig.deps.planner.plan(task, ctx(task))
    crit = await rig.deps.critic.critique(plan, ctx(task))
    assert crit.has_blocker and not crit.approve and crit.issues[-1].node_id == "impl"


# ----------------------------------------------------------------------- judge
def rec(task, *, items=(), tests=(), actions_ok=True, outcome=Outcome.UNVERIFIED) -> EvidenceRecord:
    return EvidenceRecord(
        trace_id="a" * 32, tenant_id=task.tenant_id, task_id=task.task_id, goal=task.goal, evidence=list(items), tests=list(tests),
        actions=[ActionResult(action_id="a1", ok=actions_ok)], final_outcome=outcome,
    )


def ev(kind, passed):
    return EvidenceItem(kind=kind, summary=str(kind), passed=passed)


async def test_judge_requires_strong_evidence_not_model_claims():
    task, j = make_task(), DeterministicJudge()
    assert isinstance(j, Judge)
    weak = rec(task, items=[ev(EvidenceKind.MODEL_JUDGEMENT, True), ev(EvidenceKind.DIFF, True), ev(EvidenceKind.LOG, True), ev(EvidenceKind.SCREENSHOT, True)])
    v = await j.judge(task, [weak])
    assert v.decision is VerdictDecision.NEEDS_HUMAN and v.outcome is Outcome.UNVERIFIED and "không đủ" in v.reasons[0]
    assert (await j.judge(task, [])).decision is VerdictDecision.NEEDS_HUMAN
    strong = rec(task, items=[ev(EvidenceKind.TEST_RESULT, True)], outcome=Outcome.VERIFIED_SUCCESS)
    v = await j.judge(task, [strong])
    assert v.decision is VerdictDecision.PASS and v.outcome is Outcome.VERIFIED_SUCCESS and v.evidence_ids == [strong.record_id]


async def test_judge_ignores_claimed_outcome_and_fails_on_strong_failure():
    task, j = make_task(), DeterministicJudge()
    # record "khẳng định" UNVERIFIED nhưng có test pass => judge tự suy ra từ bằng chứng
    t = rec(task, tests=[TestRun(name="t", command="pytest", passed=3, exit_code=0)])
    assert (await j.judge(task, [t])).decision is VerdictDecision.PASS
    mixed = rec(task, items=[ev(EvidenceKind.TEST_RESULT, True), ev(EvidenceKind.BUILD_LOG, False)])
    v = await j.judge(task, [mixed])
    assert v.decision is VerdictDecision.FAIL and v.outcome is Outcome.VERIFIED_FAILURE
    failed_action = rec(task, items=[ev(EvidenceKind.TEST_RESULT, True)], actions_ok=False)
    assert (await j.judge(task, [failed_action])).decision is not VerdictDecision.PASS
    assert derive_outcome([ev(EvidenceKind.HUMAN_CONFIRMATION, True)], []) is Outcome.VERIFIED_SUCCESS
    assert derive_outcome([ev(EvidenceKind.HTTP_CHECK, None)], []) is Outcome.UNVERIFIED


async def test_judge_model_second_opinion_can_only_downgrade(models_cfg, policy_cfg):
    task = make_task()
    strong = rec(task, items=[ev(EvidenceKind.TEST_RESULT, True)], outcome=Outcome.VERIFIED_SUCCESS)
    yes = FakeProvider(L, models=models_cfg.models, script=lambda r: json.dumps({"contradiction": True, "reason": "log lạ"}))
    v = await DeterministicJudge(broker_for(models_cfg, policy_cfg, {L: yes})).judge(task, [strong])
    assert v.decision is VerdictDecision.NEEDS_HUMAN and "log lạ" in v.reasons[0] and v.outcome is Outcome.UNVERIFIED
    no = FakeProvider(L, models=models_cfg.models, script=lambda r: json.dumps({"contradiction": False}))
    v = await DeterministicJudge(broker_for(models_cfg, policy_cfg, {L: no})).judge(task, [strong])
    assert v.decision is VerdictDecision.PASS and v.judge == "deterministic+local:local-default"
    # model nói "ổn" không thể nâng bằng chứng yếu
    weak = rec(task, items=[ev(EvidenceKind.MODEL_JUDGEMENT, True)])
    v = await DeterministicJudge(broker_for(models_cfg, policy_cfg, {L: no})).judge(task, [weak])
    assert v.decision is VerdictDecision.NEEDS_HUMAN and not no.calls[1:]  # không gọi model khi không PASS


async def test_judge_is_cross_vendor_from_makers(models_cfg, policy_cfg):
    fa = FakeProvider(A, models=models_cfg.models, script=llm_script())
    fl = FakeProvider(L, models=models_cfg.models, script=llm_script())
    task = make_task()
    strong = rec(task, items=[ev(EvidenceKind.TEST_RESULT, True)], outcome=Outcome.VERIFIED_SUCCESS)
    j = DeterministicJudge(broker_for(models_cfg, policy_cfg, {A: fa, L: fl})).for_makers({A})
    v = await j.judge(task, [strong])
    assert v.judge.endswith("local:local-default") and fl.calls and not fa.calls


def test_all_eval_families_have_distinct_enum():
    assert len(EVAL_TASK_FAMILIES) == 18
