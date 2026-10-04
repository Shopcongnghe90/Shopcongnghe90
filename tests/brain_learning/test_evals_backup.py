from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.brain_learning.conftest import MIGRATIONS, make_evidence
from zeus.brain import BackupError, BrainBackup, HashingEmbedding, PgBrainRetriever, PgMemoryStore
from zeus.contracts.models import EVAL_TASK_FAMILIES, MemoryItem, MemoryKind, Outcome, RetrievalQuery, TrustLevel
from zeus.evals import PgEvalStore, add_regression_case, grade, load_cases, oracle_candidate, run_eval, write_report
from zeus.evals.runner import DEFAULT_ROOT
from zeus.evidence import ArtifactStore, PgEvidenceStore
from zeus.learning import PgOutcomeRecorder
from zeus.storage import apply_migrations


def test_graders():
    assert grade("Thuế bị tính HAI LẦN", {"type": "contains", "values": ["thue", "hai lan"]})[0]
    assert not grade("x", {"type": "contains", "values": ["y"]})[0]
    assert grade(" ok ", {"type": "exact", "value": "ok"})[0] and not grade("ok.", {"type": "exact", "value": "ok"})[0]
    schema = {"type": "object", "required": ["a"], "additionalProperties": False,
              "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"enum": ["x", "y"]}}}
    spec = {"type": "json_schema", "schema": schema}
    assert grade('{"a": 2, "b": "x"}', spec)[0]
    for bad in ('{"a": true}', '{"a": 0}', '{}', '{"a": 1, "b": "z"}', '{"a": 1, "c": 1}', "not json"):
        assert not grade(bad, spec)[0], bad
    assert not grade("x", {"type": "nope"})[0]


def test_corpus_covers_18_families_with_real_cases():
    cases = load_cases()
    by = {}
    for c in cases:
        by.setdefault(c.family, []).append(c)
    assert set(by) == set(EVAL_TASK_FAMILIES) and len(EVAL_TASK_FAMILIES) == 18
    assert all(len(v) >= 5 for v in by.values())
    assert all((DEFAULT_ROOT / f.value / "v1.jsonl").exists() for f in EVAL_TASK_FAMILIES)
    assert {c.grader["type"] for c in cases} == {"contains", "json_schema"}
    assert all(len(c.input["prompt"]) > 20 for c in cases)


async def test_eval_runner_oracle_and_bad_candidate_report_json(tmp_path):
    cases = load_cases()
    rep = await run_eval(cases, oracle_candidate, "oracle")
    assert rep.n_cases >= 90 and rep.pass_rate == 1.0 and set(rep.by_family) == {f.value for f in EVAL_TASK_FAMILIES}
    out = tmp_path / "report.json"
    write_report(rep, out)
    assert json.loads(out.read_text())["n_passed"] == rep.n_cases

    async def dumb(case):
        if case.family.value == "erp_bug":
            raise RuntimeError("provider chết")
        return ("không biết", 0.002)
    bad = await run_eval(cases, dumb, "dumb")
    assert bad.pass_rate < 0.05 and bad.cost_usd == pytest.approx(0.002 * (len(cases) - 5))
    assert any("lỗi candidate" in r.detail for r in bad.results)


async def test_regression_case_from_verified_failure_and_store(bdsn, tmp_path):
    root = tmp_path / "datasets"
    shutil.copytree(DEFAULT_ROOT, root)
    fail = make_evidence(outcome=Outcome.VERIFIED_FAILURE, goal="Tính thuế đơn DH9 sai khi có voucher")
    case = add_regression_case(fail, "Thuế tính trên giá sau voucher", {"type": "contains", "values": ["sau voucher"]}, root)
    assert case and case.source == "regression"
    assert add_regression_case(fail, "x", {"type": "contains", "values": ["x"]}, root) is None  # idempotent
    with pytest.raises(ValueError):
        add_regression_case(make_evidence(outcome=Outcome.UNVERIFIED), "x", {"type": "exact", "value": "x"}, root)
    with pytest.raises(ValueError):
        add_regression_case(make_evidence(outcome=Outcome.VERIFIED_SUCCESS), "x", {"type": "exact", "value": "x"}, root)
    cases = load_cases(root)
    assert sum(c.source == "regression" for c in cases) == 1
    async def stale(case):  # candidate cũ không biết bug mới
        return await oracle_candidate(case) if case.source != "regression" else "sai"
    rep = await run_eval(cases, stale, "stale")
    assert rep.regression_failures == 1 and rep.n_passed == rep.n_cases - 1
    es = PgEvalStore(bdsn)
    assert await es.register_datasets("zeusvn", root) == 18
    run_id = await es.save_report("zeusvn", rep)
    import psycopg
    with psycopg.connect(bdsn) as c:
        assert c.execute("SELECT count(*) FROM eval_results WHERE run_id=%s AND NOT passed", (run_id,)).fetchone()[0] == 1


@pytest.mark.pg
async def test_backup_restore_match_and_corruption_detected(bdsn, pg_cluster, tmp_path):
    import psycopg
    import uuid
    emb = HashingEmbedding()
    mem = PgMemoryStore(bdsn, emb)
    arts = ArtifactStore(bdsn, tmp_path / "art")
    ev = PgEvidenceStore(bdsn)
    rec = PgOutcomeRecorder(bdsn, ev)
    await mem.put(MemoryItem(kind=MemoryKind.SEMANTIC, content="Quy trình đổi trả laptop lỗi", trust=TrustLevel.VERIFIED, tags=["a"]))
    await mem.set_canonical("zeusvn", "phase", {"n": 1})
    await mem.record_decision("zeusvn", "ADR", "quyết định")
    ref = await arts.put("zeusvn", b"log build")
    await rec.record(make_evidence(task_id="bk").model_copy(update={"code_diff_ref": ref.ref}))
    await rec.record(make_evidence(task_id="bk2", outcome=Outcome.VERIFIED_FAILURE, cost=0.1234567891))
    bk = BrainBackup(bdsn)
    path = tmp_path / "brain.jsonl"
    header = await bk.export("zeusvn", path)
    assert header["tables"]["memory_items"]["count"] == 1 and header["tables"]["outcomes"]["count"] == 2

    name = f"zeus_restore_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_cluster.dsn(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        target = pg_cluster.dsn(name)
        with psycopg.connect(target, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        apply_migrations(target, MIGRATIONS)
        item = await BrainBackup(target).restore(path)
        assert item.kind.value == "data_match" and item.passed is True, item.summary
        got = await PgMemoryStore(target, emb).list("zeusvn")
        assert got[0].content.startswith("Quy trình")
        # retrieval vector + lexical hoạt động trên DB đã khôi phục
        hits = await PgBrainRetriever(PgMemoryStore(target, emb)).retrieve(RetrievalQuery(text="đổi trả laptop"))
        assert hits and hits[0].vector_score is not None
        assert len(await PgEvidenceStore(target).list_for_task("zeusvn", "bk")) == 1
        # tệp hỏng bị phát hiện trước khi nhập
        text = path.read_text()
        path.write_text(text.replace("Quy trình", "Quy TRINH", 1))
        with pytest.raises(BackupError, match="checksum"):
            BrainBackup.read_backup(path)
    finally:
        with psycopg.connect(pg_cluster.dsn(), autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def test_activities_registered_names(bdsn):
    from temporalio.testing import ActivityEnvironment

    from zeus.brain.activities import BrainActivities

    store = PgMemoryStore(bdsn)
    await store.put(MemoryItem(kind=MemoryKind.SEMANTIC, content="chính sách đổi trả"))
    rec = PgOutcomeRecorder(bdsn)
    acts = BrainActivities(PgBrainRetriever(store), rec.evidence, rec)
    names = {a.__temporal_activity_definition.name for a in acts.all()}
    assert names == {"zeus.brain.retrieve", "zeus.brain.build_context", "zeus.evidence.put", "zeus.learning.record_outcome"}
    env = ActivityEnvironment()
    hits = await env.run(acts.retrieve, RetrievalQuery(text="đổi trả"))
    assert len(hits) == 1
    ds = await env.run(acts.record_outcome, make_evidence(task_id="act"))
    assert ds.stage.value == "VERIFIED"
