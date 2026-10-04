"""Contract tests: bất biến của zeus.contracts (FROZEN v1.0)."""

from __future__ import annotations

import inspect
import json

import pydantic
import pytest

from zeus.contracts import api, interfaces, models
from zeus.contracts.models import (
    EVAL_TASK_FAMILIES,
    ActionSpec,
    ApprovalDecision,
    ApprovalStatus,
    Channel,
    DatasetRecord,
    DatasetStage,
    Event,
    EvidenceItem,
    EvidenceKind,
    EvidenceRecord,
    EvidenceStrength,
    Outcome,
    RiskLevel,
    RouterStat,
    ProviderKind,
    TaskFamily,
    TaskGraph,
    TaskGraphError,
    TaskNode,
    TestRun,
    TraceContext,
    Usage,
    Cost,
    Verdict,
    VerdictDecision,
)

TRACE = "a" * 32


def test_contracts_version_frozen():
    assert models.CONTRACTS_VERSION == "1.1.0"


def test_task_families_cover_eval_set():
    expected = {
        "erp_bug", "erp_feature", "website_edit", "website_build", "frontend", "backend", "database",
        "customer_support", "zalo_issue", "facebook_issue", "shopee_issue", "domain_provisioning",
        "deployment", "security", "visual_qa", "worker_scheduling", "model_routing", "tool_selection",
    }
    assert {f.value for f in EVAL_TASK_FAMILIES} == expected
    assert len(EVAL_TASK_FAMILIES) == 18
    assert TaskFamily.GENERAL.value == "general"


def test_risk_levels_ordered():
    assert RiskLevel.R0 < RiskLevel.R1 < RiskLevel.R2 < RiskLevel.R3
    assert RiskLevel.R3 >= RiskLevel.R2
    assert RiskLevel.max(RiskLevel.R1, RiskLevel.R3, RiskLevel.R0) is RiskLevel.R3
    assert RiskLevel.max() is RiskLevel.R0


def test_event_untrusted_by_default_and_extra_forbidden():
    e = Event(channel=Channel.ZALO_BOT, text="xin chào", external_id="m1")
    assert e.untrusted is True
    assert e.signature_verified is False
    assert e.dedupe_key() == "zeusvn:zalo_bot:m1"
    with pytest.raises(pydantic.ValidationError):
        Event(channel=Channel.ZALO_BOT, bogus=1)  # type: ignore[call-arg]


def test_tenant_id_validated():
    with pytest.raises(pydantic.ValidationError):
        Event(channel=Channel.API, tenant_id="Bad Tenant!")
    TraceContext(trace_id=TRACE, tenant_id="khach-01")
    with pytest.raises(pydantic.ValidationError):
        TraceContext(trace_id="xyz")


def test_task_graph_dag_validation():
    g = TaskGraph(
        task_id="t1",
        nodes=[
            TaskNode(node_id="a", title="A"),
            TaskNode(node_id="b", title="B", depends_on=["a"], risk=RiskLevel.R2),
            TaskNode(node_id="c", title="C", depends_on=["a"]),
            TaskNode(node_id="d", title="D", depends_on=["b", "c"]),
        ],
    )
    order = g.topological_order()
    assert order.index("a") < order.index("b") < order.index("d")
    assert g.ready_nodes(set()) == ["a"]
    assert sorted(g.ready_nodes({"a"})) == ["b", "c"]
    assert g.max_risk is RiskLevel.R2
    with pytest.raises((TaskGraphError, pydantic.ValidationError)):
        TaskGraph(task_id="t", nodes=[TaskNode(node_id="x", title="x", depends_on=["y"]), TaskNode(node_id="y", title="y", depends_on=["x"])])
    with pytest.raises((TaskGraphError, pydantic.ValidationError)):
        TaskGraph(task_id="t", nodes=[TaskNode(node_id="x", title="x", depends_on=["missing"])])
    with pytest.raises((TaskGraphError, pydantic.ValidationError)):
        TaskGraph(task_id="t", nodes=[TaskNode(node_id="x", title="x"), TaskNode(node_id="x", title="x2")])


def _record(**kw):
    base = dict(trace_id=TRACE, tenant_id="zeusvn", task_id="t1", goal="g")
    base.update(kw)
    return EvidenceRecord(**base)


def test_claims_are_not_evidence():
    with pytest.raises(pydantic.ValidationError):
        _record(final_outcome=Outcome.VERIFIED_SUCCESS)
    with pytest.raises(pydantic.ValidationError):
        _record(
            final_outcome=Outcome.VERIFIED_SUCCESS,
            evidence=[EvidenceItem(kind=EvidenceKind.MODEL_JUDGEMENT, summary="LGTM", passed=True)],
        )
    ok = _record(
        final_outcome=Outcome.VERIFIED_SUCCESS,
        evidence=[EvidenceItem(kind=EvidenceKind.TEST_RESULT, passed=True)],
    )
    assert ok.evidence[0].strength is EvidenceStrength.STRONG
    ok2 = _record(final_outcome=Outcome.VERIFIED_SUCCESS, tests=[TestRun(name="u", command="pytest", passed=3, exit_code=0)])
    assert ok2.tests[0].ok
    assert _record().final_outcome is Outcome.UNVERIFIED


def test_evidence_record_has_required_fields():
    required = {
        "trace_id", "tenant_id", "workflow_id", "task_id", "goal", "input_snapshot_ref", "context_snapshot_ref",
        "model_provider", "model_name", "model_version", "prompt_version", "playbook_version", "tools", "worker_id",
        "actions", "code_diff_ref", "tests", "evidence", "latency_ms", "tokens", "cost_usd", "retries",
        "human_intervention", "rollback_performed", "final_outcome",
    }
    assert required <= set(EvidenceRecord.model_fields)


def test_verdict_consistency():
    with pytest.raises(pydantic.ValidationError):
        Verdict(task_id="t", decision=VerdictDecision.PASS, outcome=Outcome.UNVERIFIED, judge="j")
    with pytest.raises(pydantic.ValidationError):
        Verdict(task_id="t", decision=VerdictDecision.PASS, outcome=Outcome.VERIFIED_SUCCESS, judge="j")
    Verdict(task_id="t", decision=VerdictDecision.PASS, outcome=Outcome.VERIFIED_SUCCESS, judge="j", evidence_ids=["e"])


def test_dataset_stage_gates():
    DatasetRecord(stage=DatasetStage.RAW)
    with pytest.raises(pydantic.ValidationError):
        DatasetRecord(stage=DatasetStage.VERIFIED)
    DatasetRecord(stage=DatasetStage.VERIFIED, outcome=Outcome.VERIFIED_FAILURE, evidence_record_id="evr_1")
    with pytest.raises(pydantic.ValidationError):
        DatasetRecord(stage=DatasetStage.CURATED, outcome=Outcome.VERIFIED_SUCCESS, evidence_record_id="evr_1")
    DatasetRecord(stage=DatasetStage.CURATED, outcome=Outcome.VERIFIED_SUCCESS, evidence_record_id="evr_1", pii_redacted=True)


def test_cost_and_router_stat_metric():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000)
    c = Cost.from_usage(u, 2.0, 10.0)  # claude-sonnet-5-5 giá 2/10
    assert c.usd == pytest.approx(3.0)
    assert Cost.from_usage(u, 2.0, 10.0, batch=True).usd == pytest.approx(1.5)
    s = RouterStat(task_family=TaskFamily.BACKEND, provider=ProviderKind.ANTHROPIC, model="m", n=4,
                   verified_success=2, verified_failure=1, unverified=1, total_cost_usd=1.0)
    assert s.cost_per_verified_success == pytest.approx(0.5)
    assert s.success_rate == pytest.approx(2 / 3)
    assert RouterStat(task_family=TaskFamily.BACKEND, provider=ProviderKind.LOCAL, model="m").cost_per_verified_success is None
    assert (u + u).input_tokens == 2_000_000


def test_action_spec_name_and_approval_decision():
    ActionSpec(name="erp.sale_order.confirm", risk=RiskLevel.R2)
    with pytest.raises(pydantic.ValidationError):
        ActionSpec(name="NoNamespace", risk=RiskLevel.R0)
    d = ApprovalDecision(approval_id="apr_1", status="APPROVED", decided_by="human:owner")
    assert d.status == ApprovalStatus.APPROVED
    with pytest.raises(pydantic.ValidationError):
        ApprovalDecision(approval_id="apr_1", status="PENDING", decided_by="x")


def _all_models():
    for mod in (models, api):
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, pydantic.BaseModel) and obj.__module__ == mod.__name__ and obj is not models.ZeusModel:
                yield obj


def test_all_models_have_json_schema():
    names = []
    for m in _all_models():
        schema = m.model_json_schema()
        json.dumps(schema)
        names.append(m.__name__)
    assert len(names) >= 50


def test_json_roundtrip_complex():
    rec = _record(
        final_outcome=Outcome.VERIFIED_SUCCESS,
        model_provider=ProviderKind.ANTHROPIC,
        model_name="claude-sonnet-5-5",
        tests=[TestRun(name="u", command="pytest", passed=1, exit_code=0)],
    )
    again = EvidenceRecord.model_validate_json(rec.model_dump_json())
    assert again == rec


def test_protocols_are_runtime_checkable():
    assert len(interfaces.ALL_PROTOCOLS) == 22
    for p in interfaces.ALL_PROTOCOLS:
        assert getattr(p, "_is_runtime_protocol", False), p


def test_api_paths_unique_and_versioned():
    paths = [v for k, v in vars(api.Paths).items() if k.isupper()]
    assert len(paths) == len(set(paths))
    assert all(p.startswith(("/api/v1/", "/worker/v1/", "/hooks/", "/healthz", "/readyz", "/wb")) for p in paths)
