from __future__ import annotations

import json

import pytest

from tests.workers.conftest import make_info
from zeus.contracts.models import (
    Outcome,
    RiskLevel,
    Task,
    TaskFamily,
    TaskNode,
    TypedAction,
    WorkerHeartbeat,
    WorkerStatus,
)
from zeus.workers.config import load_config
from zeus.workers.scheduler import FEATURE_NAMES, DeterministicScheduler, LearnedScorer

CFG = load_config()


def sched(**kw) -> DeterministicScheduler:
    return DeterministicScheduler(CFG.scheduler, **kw)


def task(family=TaskFamily.BACKEND, **kw) -> Task:
    return Task(family=family, goal="g", **kw)


def node(caps=("python",), **kw) -> TaskNode:
    return TaskNode(node_id="n1", title="t", required_capabilities=list(caps), **kw)


def hb(wid, cpu=80.0, ram=4096, depth=0, err=0.0) -> WorkerHeartbeat:
    return WorkerHeartbeat(worker_id=wid, cpu_available_pct=cpu, ram_available_mb=ram, queue_depth=depth, recent_error_rate=err)


async def test_hard_filters_and_reasons():
    t = task(family=TaskFamily.ERP_BUG)
    ws = [
        make_info("w-nocap", caps=("git",)),
        make_info("w-other-tenant", tenant_scope=["acme"]),
        make_info("w-off", status=WorkerStatus.OFFLINE),
        make_info("w-drain", status=WorkerStatus.DRAINING),
        make_info("w-zone", network_zone="ops"),  # erp_bug chỉ agent/erp_test
        make_info("w-ok", network_zone="erp_test"),
    ]
    d = await sched().schedule(t, node(), ws, {w.worker_id: hb(w.worker_id) for w in ws})
    assert d.worker_id == "w-ok"
    by = {c.worker_id: c.rejected_reason for c in d.candidates}
    assert "capability" in by["w-nocap"]
    assert "tenant" in by["w-other-tenant"]
    assert "OFFLINE" in by["w-off"] and "DRAINING" in by["w-drain"]
    assert "network_zone" in by["w-zone"]
    assert by["w-ok"] is None
    assert set(d.weights) == set(FEATURE_NAMES) and d.features is not None and len(d.candidates) == 6


async def test_no_eligible_worker_means_queued():
    d = await sched().schedule(task(), node(caps=("gpu_llm",)), [make_info("w1")], {})
    assert d.worker_id is None and "xếp hàng" in d.reason and d.features is None
    d2 = await sched().schedule(task(), node(), [], {})
    assert d2.worker_id is None


async def test_scoring_prefers_idle_and_healthy_and_is_deterministic():
    ws = [make_info("w-busy"), make_info("w-idle"), make_info("w-flaky")]
    hbs = {"w-busy": hb("w-busy", cpu=10, depth=5), "w-idle": hb("w-idle", cpu=90, depth=0), "w-flaky": hb("w-flaky", cpu=90, err=0.9)}
    s = sched()
    d1 = await s.schedule(task(), node(), ws, hbs)
    d2 = await s.schedule(task(), node(), list(reversed(ws)), hbs)
    assert d1.worker_id == "w-idle"
    assert [c.model_dump(exclude={"features"}) for c in d1.candidates] == [c.model_dump(exclude={"features"}) for c in d2.candidates]
    assert d1.candidates[0].features.model_dump() == d2.candidates[0].features.model_dump()


async def test_tie_breaks_on_worker_id_and_data_locality_wins():
    ws = [make_info("w-b"), make_info("w-a")]
    hbs = {w.worker_id: hb(w.worker_id) for w in ws}
    assert (await sched().schedule(task(), node(), ws, hbs)).worker_id == "w-a"
    ws2 = [make_info("w-a"), make_info("w-b", data_localities=["repo:erp-addons"])]
    n = TaskNode(node_id="n", title="t", required_capabilities=["python"],
                 action=TypedAction(name="noop.echo", args={"data_locality": ["repo:erp-addons"]}))
    d = await sched().schedule(task(), n, ws2, {w.worker_id: hb(w.worker_id) for w in ws2})
    assert d.worker_id == "w-b"


async def test_risk_and_urgency_features_normalised():
    d = await sched().schedule(task(urgency=3), node(risk=RiskLevel.R2), [make_info("w1")], {"w1": hb("w1")})
    assert d.features.urgency == 1.0 and d.features.risk == pytest.approx(2 / 3)
    json.dumps(d.model_dump(mode="json"))  # mẫu ScheduleDecision JSON serialisable


async def test_learned_scorer_stub_uses_stats():
    ws = [make_info("w-a"), make_info("w-b")]
    hbs = {w.worker_id: hb(w.worker_id) for w in ws}
    s = sched(scorer=LearnedScorer({"w-a": (0, 10), "w-b": (10, 10)}))
    d = await s.schedule(task(), node(), ws, hbs)
    assert d.worker_id == "w-b" and d.policy_version.endswith("learned-stub")


@pytest.mark.pg
async def test_decision_persisted_and_stats_learned(stack):
    ws = [make_info("w-a"), make_info("w-b")]
    for w in ws:
        await stack.registry.register(w)
    t = task()
    d = await stack.scheduler.schedule(t, node(), ws, {})
    await stack.store.save(d)
    back = await stack.store.get(d.decision_id)
    assert back.worker_id == d.worker_id and back.features == d.features and back.candidates == d.candidates
    assert back.weights == d.weights
    assert await stack.store.success_stats(TaskFamily.BACKEND) == {}
    await stack.store.record_outcome(d.decision_id, Outcome.VERIFIED_SUCCESS)
    assert await stack.store.success_stats(TaskFamily.BACKEND) == {d.worker_id: (1, 1)}
    # thống kê đã học làm lệch lựa chọn lần sau (historical_success cao hơn cho worker đã thắng)
    other = "w-b" if d.worker_id == "w-a" else "w-a"
    d2 = await stack.scheduler.schedule(task(), node(), ws, {})
    chosen = next(c for c in d2.candidates if c.worker_id == d.worker_id)
    unchosen = next(c for c in d2.candidates if c.worker_id == other)
    assert chosen.features.historical_success > unchosen.features.historical_success
