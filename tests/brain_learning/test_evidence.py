from __future__ import annotations

import hashlib

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.brain_learning.conftest import make_evidence
from zeus.contracts.interfaces import EvidenceStore
from zeus.contracts.models import EvidenceItem, EvidenceKind, EvidenceRecord, Outcome, TestRun
from zeus.evidence import (
    ArtifactMissing,
    ArtifactStore,
    EvidenceImmutable,
    EvidenceRuleError,
    PgEvidenceStore,
    build_router,
    check_record,
    strength_of,
)

pytestmark = [pytest.mark.pg, pytest.mark.cloud_exit_evidence]


def _rec(**kw) -> EvidenceRecord:
    base = dict(trace_id="b" * 32, tenant_id="zeusvn", task_id="t1", goal="g")
    return EvidenceRecord(**{**base, **kw})


def test_strength_rules_claims_are_not_evidence():
    assert strength_of(_rec()) == "none"
    weak = _rec(evidence=[EvidenceItem(kind=EvidenceKind.MODEL_JUDGEMENT, passed=True)])
    assert strength_of(weak) == "weak"
    with pytest.raises(ValueError):  # validator của contract
        _rec(final_outcome=Outcome.VERIFIED_SUCCESS, evidence=[EvidenceItem(kind=EvidenceKind.MODEL_JUDGEMENT, passed=True)])
    # test đạt + một test khác fail => không được VERIFIED_SUCCESS (luật của B)
    mixed = _rec(final_outcome=Outcome.VERIFIED_SUCCESS,
                 tests=[TestRun(name="a", command="x", passed=3, exit_code=0), TestRun(name="b", command="y", passed=1, failed=1, exit_code=1)])
    with pytest.raises(EvidenceRuleError):
        check_record(mixed)
    with pytest.raises(EvidenceRuleError):  # VERIFIED_FAILURE trống = chỉ là lời khẳng định
        check_record(_rec(final_outcome=Outcome.VERIFIED_FAILURE))


async def test_put_get_list_and_protocol(bdsn):
    store = PgEvidenceStore(bdsn)
    assert isinstance(store, EvidenceStore)
    r1 = make_evidence(task_id="tsk_a")
    r2 = make_evidence(task_id="tsk_a", outcome=Outcome.UNVERIFIED)
    r3 = make_evidence(task_id="tsk_b", tenant="acme")
    for r in (r1, r2, r3):
        await store.put(r)
    assert await store.put(r1) == r1.record_id  # idempotent
    got = await store.get(r1.record_id, "zeusvn")
    assert got == r1
    assert await store.get(r3.record_id, "zeusvn") is None  # cô lập tenant
    assert {x.record_id for x in await store.list_for_task("zeusvn", "tsk_a")} == {r1.record_id, r2.record_id}
    assert await store.list_for_task("zeusvn", "tsk_b") == []


async def test_verified_is_immutable_unverified_can_upgrade(bdsn):
    store = PgEvidenceStore(bdsn)
    weak = make_evidence(outcome=Outcome.UNVERIFIED)
    await store.put(weak)
    strong = weak.model_copy(update={"final_outcome": Outcome.VERIFIED_SUCCESS, "tests": [TestRun(name="u", command="pytest", passed=2, exit_code=0)]})
    await store.put(strong)  # nâng cấp UNVERIFIED -> VERIFIED
    assert (await store.get(weak.record_id, "zeusvn")).final_outcome is Outcome.VERIFIED_SUCCESS
    downgrade = strong.model_copy(update={"final_outcome": Outcome.VERIFIED_FAILURE, "tests": [TestRun(name="u", command="p", passed=1, failed=1, exit_code=1)]})
    with pytest.raises(EvidenceImmutable):
        await store.put(downgrade)
    with psycopg.connect(bdsn, autocommit=True) as c:
        for sql in ("UPDATE evidence_records SET strength='weak'",
                    "DELETE FROM evidence_records"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)


async def test_artifacts_content_addressed_immutable_and_trace(bdsn, tmp_path):
    arts = ArtifactStore(bdsn, tmp_path / "art")
    store = PgEvidenceStore(bdsn)
    data = b"diff --git a/erp.py b/erp.py\n+fix tax"
    ref = await arts.put("zeusvn", data, "text/x-diff", "fix.diff")
    assert ref.sha256 == hashlib.sha256(data).hexdigest() and ref.ref == f"artifact://zeusvn/{ref.sha256}"
    assert (await arts.put("zeusvn", data)).ref == ref.ref  # trùng nội dung => cùng ref
    assert await arts.get("zeusvn", ref.sha256) == data
    assert not await arts.exists("acme", ref.sha256)  # cô lập tenant
    rec = make_evidence(task_id="tsk_trace").model_copy(update={"code_diff_ref": ref.ref})
    await store.put(rec)
    tr = await store.trace("zeusvn", "tsk_trace")
    assert tr[0]["record_id"] == rec.record_id and tr[0]["artifacts"][0]["sha256"] == ref.sha256
    # tham chiếu artifact không tồn tại / của tenant khác bị chặn
    with pytest.raises(ArtifactMissing):
        await store.put(make_evidence(task_id="x").model_copy(update={"code_diff_ref": f"artifact://zeusvn/{'0' * 64}"}))
    with pytest.raises(ValueError):
        await store.put(make_evidence(task_id="y").model_copy(update={"code_diff_ref": f"artifact://acme/{ref.sha256}"}))
    # artifacts bất biến trong DB; file bị sửa => phát hiện
    with psycopg.connect(bdsn, autocommit=True) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE artifacts SET name='x'")
    (tmp_path / "art" / "zeusvn" / ref.sha256[:2] / ref.sha256).write_bytes(b"tampered")
    assert not await arts.verify("zeusvn", ref.sha256)


async def test_evidence_http_router(bdsn):
    store = PgEvidenceStore(bdsn)
    app = FastAPI()
    app.include_router(build_router(store))
    rec = make_evidence(task_id="tsk_http")
    with TestClient(app) as cl:
        assert cl.post("/evidence", json=rec.model_dump(mode="json")).status_code == 201
        assert cl.get(f"/evidence/{rec.record_id}", params={"tenant_id": "zeusvn"}).json()["record_id"] == rec.record_id
        assert cl.get(f"/evidence/{rec.record_id}", params={"tenant_id": "acme"}).status_code == 404
        assert len(cl.get("/evidence/task/tsk_http", params={"tenant_id": "zeusvn"}).json()) == 1
        bad = rec.model_dump(mode="json") | {"record_id": "evr_bad", "final_outcome": "VERIFIED_SUCCESS", "tests": [], "evidence": []}
        assert cl.post("/evidence", json=bad).status_code == 422
        mixed = make_evidence(task_id="m").model_dump(mode="json")
        mixed["tests"].append({"name": "b", "command": "x", "passed": 1, "failed": 1, "exit_code": 1})
        assert cl.post("/evidence", json=mixed).status_code == 422
        flip = rec.model_dump(mode="json") | {"final_outcome": "VERIFIED_FAILURE", "tests": [{"name": "u", "command": "p", "passed": 0, "failed": 1, "exit_code": 1}]}
        assert cl.post("/evidence", json=flip).status_code == 409
