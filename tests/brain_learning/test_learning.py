from __future__ import annotations

import pytest

from tests.brain_learning.conftest import make_evidence
from zeus.brain import PgBrainRetriever, PgMemoryStore
from zeus.contracts.interfaces import OutcomeRecorder
from zeus.contracts.models import (
    DatasetRecord,
    DatasetStage,
    MemoryKind,
    Outcome,
    ProviderKind,
    RetrievalQuery,
    RiskLevel,
    RouterStat,
    TaskFamily,
)
from zeus.learning import (
    CCState,
    ChampionChallenger,
    DatasetPipeline,
    PgOutcomeRecorder,
    PromotionBlocked,
    PromotionError,
    Thresholds,
    decide_promotion,
    redact_pii,
    wilson_lower,
)

pytestmark = [pytest.mark.pg]


async def test_recorder_stage_and_router_stats_match_total_cost(bdsn):
    rec = PgOutcomeRecorder(bdsn)
    assert isinstance(rec, OutcomeRecorder)
    evs = [
        make_evidence(task_id="1", cost=0.10, worker_id="w1"),
        make_evidence(task_id="2", cost=0.20, worker_id="w1"),
        make_evidence(task_id="3", cost=0.30, outcome=Outcome.VERIFIED_FAILURE, worker_id="w2"),
        make_evidence(task_id="4", cost=0.05, outcome=Outcome.UNVERIFIED, worker_id="w2"),
        make_evidence(task_id="5", cost=0.07, model="gpt-x", provider=ProviderKind.OPENAI),
        make_evidence(task_id="6", cost=0.02, prompt_version="p2"),
    ]
    ds = [await rec.record(e) for e in evs]
    assert [d.stage for d in ds] == [DatasetStage.VERIFIED] * 3 + [DatasetStage.RAW] + [DatasetStage.VERIFIED] * 2
    assert ds[3].evidence_record_id is None and ds[2].outcome is Outcome.VERIFIED_FAILURE
    await rec.record(evs[0])  # idempotent: không cộng đôi
    stats = {(s.provider, s.model): s for s in await rec.stats(TaskFamily.ERP_BUG)}
    son = stats[(ProviderKind.ANTHROPIC, "claude-sonnet-5-5")]
    assert (son.n, son.verified_success, son.verified_failure, son.unverified) == (5, 3, 1, 1)
    assert son.total_cost_usd == pytest.approx(0.10 + 0.20 + 0.30 + 0.05 + 0.02)
    assert son.cost_per_verified_success == pytest.approx(son.total_cost_usd / 3)
    assert sum(s.total_cost_usd for s in stats.values()) == pytest.approx(sum(e.cost_usd for e in evs))
    assert await rec.stats(TaskFamily.SECURITY) == []
    byp = {pv: s for pv, s in await rec.stats_by_prompt("zeusvn", TaskFamily.ERP_BUG) if s.model.startswith("claude")}
    assert byp["p2"].n == 1 and byp["p1"].n == 4
    ws = await rec.worker_stats("zeusvn")
    assert ws["w1"].success_rate == 1.0 and ws["w2"].verified_failure == 1 and ws["w2"].n == 2


async def test_stats_are_tenant_scoped(bdsn):
    rec = PgOutcomeRecorder(bdsn)
    await rec.record(make_evidence(tenant="acme", task_id="a"))
    assert await rec.stats() == []
    assert len(await rec.stats(tenant_id="acme")) == 1


async def test_pipeline_gates_no_unverified_promotion_or_training(bdsn):
    rec = PgOutcomeRecorder(bdsn)
    pipe = rec.pipeline
    raw = await rec.record(make_evidence(task_id="u", outcome=Outcome.UNVERIFIED))
    assert raw.stage is DatasetStage.RAW
    with pytest.raises(PromotionError, match="nhảy cấp"):
        await pipe.verify("zeusvn", raw.record_id)  # RAW không được nhảy thẳng
    norm = await pipe.normalize("zeusvn", raw.record_id)
    assert norm.stage is DatasetStage.NORMALIZED and norm.promoted_from == raw.record_id
    with pytest.raises(PromotionError):  # không evidence
        await pipe.verify("zeusvn", norm.record_id)
    unv = make_evidence(task_id="u2", outcome=Outcome.UNVERIFIED)
    await rec.evidence.put(unv)
    with pytest.raises(PromotionError, match="chưa verified"):
        await pipe.verify("zeusvn", norm.record_id, unv.record_id)
    with pytest.raises(PromotionError, match="evidence không tồn tại"):  # evidence tenant khác
        other = make_evidence(tenant="acme", task_id="o")
        await rec.evidence.put(other)
        await pipe.verify("zeusvn", norm.record_id, other.record_id)
    with pytest.raises(ValueError):  # validator contract + ràng buộc DB
        DatasetRecord(stage=DatasetStage.VERIFIED)
    assert await pipe.export_training("zeusvn") == []

    # đường hợp lệ: evidence verified + PII được che trước CURATED
    good = make_evidence(task_id="g", goal="Khách 0909123456 email a.b@c.vn hỏi đổi trả")
    await rec.evidence.put(good)
    n2 = await pipe.normalize("zeusvn", (await pipe.insert(DatasetRecord(
        tenant_id="zeusvn", task_family=TaskFamily.CUSTOMER_SUPPORT, input={"q": "Khách 0909123456 email a.b@c.vn"},
        output={"a": "ok"}))).record_id)
    v = await pipe.verify("zeusvn", n2.record_id, good.record_id)
    assert v.stage is DatasetStage.VERIFIED and v.outcome is Outcome.VERIFIED_SUCCESS
    cur = await pipe.curate("zeusvn", v.record_id)
    assert cur.pii_redacted and "[PHONE]" in cur.input["q"] and "[EMAIL]" in cur.input["q"] and "0909" not in str(cur.input)
    exp = await pipe.export_training("zeusvn")
    assert [e["record_id"] for e in exp] == [cur.record_id]
    # VERIFIED_FAILURE không bao giờ thành dữ liệu huấn luyện
    fail = await rec.record(make_evidence(task_id="f", outcome=Outcome.VERIFIED_FAILURE))
    with pytest.raises(PromotionError):
        await pipe.curate("zeusvn", fail.record_id)
    assert len(await pipe.export_training("zeusvn")) == 1
    assert redact_pii("gọi +84 912 345 678") == "gọi [PHONE]"


def test_wilson_and_decide_promotion():
    assert wilson_lower(0, 0) == 0 and 0.8 < wilson_lower(95, 100) < 0.95

    def st(vs, vf, cost):
        return RouterStat(task_family=TaskFamily.ERP_BUG, provider=ProviderKind.ANTHROPIC, model="m", n=vs + vf,
                          verified_success=vs, verified_failure=vf, total_cost_usd=cost)
    th = Thresholds(min_canary_n=30)
    champ = st(60, 40, 10.0)  # 0.60, cost/vs 0.1667
    assert decide_promotion(champ, st(95, 5, 8.0), th).promote
    d = decide_promotion(champ, st(9, 1, 1.0), th)  # quá ít mẫu
    assert not d.promote and "mẫu" in d.reasons[0]
    assert not decide_promotion(champ, st(55, 45, 5.0), th).promote  # kém hơn về success
    d = decide_promotion(champ, st(95, 5, 30.0), th)  # thắng nhưng đắt hơn
    assert not d.promote and any("cost" in r for r in d.reasons) and d.report["challenger"]["n_verified"] == 100


def _good_eval(): return {"n_cases": 90, "pass_rate": 0.95, "regression_failures": 0, "eval_run_id": "evr_1"}


async def _walk_to_compare(cc: ChampionChallenger, subject="route:erp_bug", **kw):
    await cc.register("zeusvn", subject, "route", "haiku", "erp_bug", **kw)
    await cc.propose("zeusvn", subject, "sonnet")
    await cc.advance("zeusvn", subject)  # CHALLENGER -> OFFLINE_EVAL
    await cc.advance("zeusvn", subject, _good_eval())
    await cc.advance("zeusvn", subject, {"n": 25, "no_regressions": True})
    await cc.advance("zeusvn", subject, {"n": 25, "side_effects": 0})
    await cc.advance("zeusvn", subject, {"canary_ok": True})


def _stat(vs, vf, cost):
    return RouterStat(task_family=TaskFamily.ERP_BUG, provider=ProviderKind.ANTHROPIC, model="m", n=vs + vf,
                      verified_success=vs, verified_failure=vf, total_cost_usd=cost)


async def test_champion_challenger_full_path_promote_writes_ledger_and_brain(bdsn):
    mem = PgMemoryStore(bdsn)
    cc = ChampionChallenger(bdsn, mem, Thresholds(min_canary_n=30))
    await _walk_to_compare(cc)
    s = await cc.get("zeusvn", "route:erp_bug")
    assert s.state is CCState.COMPARE and [h["to"] for h in s.history][-1] == "COMPARE"
    d = await cc.promote("zeusvn", "route:erp_bug", _stat(60, 40, 10.0), _stat(95, 5, 8.0))
    assert d.promote
    s = await cc.get("zeusvn", "route:erp_bug")
    assert s.state is CCState.CHAMPION and s.champion == "sonnet" and s.challenger is None
    led = await mem.list_decisions("zeusvn")
    assert "Promote route:erp_bug" in led[0]["title"]
    decs = await mem.list("zeusvn", MemoryKind.DECISION)
    assert decs and "sonnet" in decs[0].content
    hits = await PgBrainRetriever(mem).retrieve(RetrievalQuery(text="champion route erp_bug"))
    assert hits and hits[0].memory.kind is MemoryKind.DECISION


async def test_champion_challenger_blocks_shortcuts_and_auto_promote(bdsn):
    cc = ChampionChallenger(bdsn, PgMemoryStore(bdsn), Thresholds(min_canary_n=30))
    await cc.register("zeusvn", "route:a", "route", "haiku", "erp_bug")
    # quan sát production chỉ tạo đề xuất; promote ngay bị chặn
    await cc.suggest("zeusvn", "route:a", "sonnet", origin="production_observation")
    with pytest.raises(PromotionBlocked, match="COMPARE"):
        await cc.promote("zeusvn", "route:a", _stat(60, 40, 10.0), _stat(100, 0, 1.0))
    await cc.advance("zeusvn", "route:a")
    for bad in ({}, {"n_cases": 90, "pass_rate": 0.8, "regression_failures": 0, "eval_run_id": "e"},
                {"n_cases": 90, "pass_rate": 0.99, "regression_failures": 1, "eval_run_id": "e"}):
        with pytest.raises(PromotionBlocked, match="offline eval"):
            await cc.advance("zeusvn", "route:a", bad)
    await cc.advance("zeusvn", "route:a", _good_eval())
    with pytest.raises(PromotionBlocked):
        await cc.advance("zeusvn", "route:a", {"n": 5, "no_regressions": True})
    await cc.advance("zeusvn", "route:a", {"n": 25, "no_regressions": True})
    with pytest.raises(PromotionBlocked, match="shadow"):
        await cc.advance("zeusvn", "route:a", {"n": 25, "side_effects": 2})
    # rollback hợp lệ ở bất kỳ bước nào
    s = await cc.rollback("zeusvn", "route:a", "shadow lệch")
    assert s.state is CCState.CHAMPION and s.champion == "haiku" and s.history[-1]["action"] == "rollback"
    with pytest.raises(PromotionBlocked):
        await cc.rollback("zeusvn", "route:a", "x")


async def test_r2_never_auto_promotes_and_bad_stats_blocked(bdsn):
    cc = ChampionChallenger(bdsn, PgMemoryStore(bdsn), Thresholds(min_canary_n=30))
    await _walk_to_compare(cc, "route:r2", risk=RiskLevel.R2)
    win = (_stat(60, 40, 10.0), _stat(95, 5, 8.0))
    with pytest.raises(PromotionBlocked, match="phê duyệt người"):
        await cc.promote("zeusvn", "route:r2", *win)
    with pytest.raises(PromotionBlocked, match="phê duyệt người"):
        await cc.promote("zeusvn", "route:r2", *win, approved_by="agent:planner")
    with pytest.raises(PromotionBlocked, match="mẫu"):
        await cc.promote("zeusvn", "route:r2", _stat(60, 40, 10.0), _stat(5, 0, 0.1), approved_by="human:owner")
    assert (await cc.get("zeusvn", "route:r2")).state is CCState.COMPARE
    assert (await cc.promote("zeusvn", "route:r2", *win, approved_by="human:owner")).promote
