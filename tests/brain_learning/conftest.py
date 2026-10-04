from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from zeus.contracts.models import (
    EvidenceItem,
    EvidenceKind,
    EvidenceRecord,
    Outcome,
    ProviderKind,
    TaskFamily,
    TestRun,
)
from zeus.storage import apply_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
TRACE = "a" * 32


@pytest.fixture()
def bdsn(pg_dsn: str) -> str:
    """DB đã áp toàn bộ migration + tenant 'acme' thứ hai để test cô lập."""
    apply_migrations(pg_dsn, MIGRATIONS)
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("INSERT INTO tenants (tenant_id, display_name, kind) VALUES ('acme','Acme','customer')")
    return pg_dsn


def make_evidence(
    *,
    tenant: str = "zeusvn",
    task_id: str = "tsk_1",
    outcome: Outcome = Outcome.VERIFIED_SUCCESS,
    family: TaskFamily = TaskFamily.ERP_BUG,
    model: str = "claude-sonnet-5-5",
    provider: ProviderKind = ProviderKind.ANTHROPIC,
    cost: float = 0.01,
    prompt_version: str = "p1",
    worker_id: str | None = None,
    extra_items: list[EvidenceItem] | None = None,
    goal: str = "Sửa lỗi tính thuế đơn hàng",
) -> EvidenceRecord:
    ev: list[EvidenceItem] = list(extra_items or [])
    tests: list[TestRun] = []
    if outcome is Outcome.VERIFIED_SUCCESS:
        tests = [TestRun(name="unit", command="pytest -q", passed=5, failed=0, exit_code=0)]
    elif outcome is Outcome.VERIFIED_FAILURE:
        tests = [TestRun(name="unit", command="pytest -q", passed=3, failed=2, exit_code=1)]
    else:
        ev.append(EvidenceItem(kind=EvidenceKind.MODEL_JUDGEMENT, summary="model nói là xong"))
    return EvidenceRecord(
        trace_id=TRACE, tenant_id=tenant, task_id=task_id, task_family=family, goal=goal, model_provider=provider,
        model_name=model, prompt_version=prompt_version, cost_usd=cost, latency_ms=100, worker_id=worker_id,
        tests=tests, evidence=ev, final_outcome=outcome, verified_by="judge:openai" if outcome is not Outcome.UNVERIFIED else None,
    )
