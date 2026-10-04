"""OutcomeRecorder: EvidenceRecord -> outcomes + router_stats (+ DatasetRecord đúng stage) + thống kê worker."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from zeus.brain.pg import PgBase
from zeus.contracts.models import (
    DEFAULT_TENANT,
    DatasetRecord,
    DatasetStage,
    EvidenceRecord,
    Outcome,
    ProviderKind,
    RouterStat,
    TaskFamily,
)
from zeus.evidence.rules import check_record
from zeus.evidence.store import PgEvidenceStore
from zeus.learning.pipeline import DatasetPipeline, derived_id


class WorkerStat(BaseModel):
    worker_id: str
    n: int
    verified_success: int
    verified_failure: int
    avg_latency_ms: float
    avg_cost_usd: float

    @property
    def success_rate(self) -> float:
        v = self.verified_success + self.verified_failure
        return self.verified_success / v if v else 0.0


class PgOutcomeRecorder(PgBase):
    """Implements ``OutcomeRecorder``. ``stats`` nhận thêm ``tenant_id`` (xem CONTRACT_CHANGE_REQUEST)."""

    def __init__(self, dsn: str, evidence: PgEvidenceStore | None = None, default_tenant: str = DEFAULT_TENANT) -> None:
        super().__init__(dsn)
        self.evidence = evidence or PgEvidenceStore(dsn)
        self.pipeline = DatasetPipeline(dsn, self.evidence)
        self.default_tenant = default_tenant

    async def record(self, evidence: EvidenceRecord) -> DatasetRecord:
        check_record(evidence)
        await self.evidence.put(evidence)  # idempotent; bảo đảm FK + bất biến
        provider = evidence.model_provider.value if evidence.model_provider else "unknown"
        model = evidence.model_name or "unknown"
        pv = evidence.prompt_version or ""
        o = evidence.final_outcome
        async with self.conn(autocommit=False) as c:
            cur = await c.execute(
                "INSERT INTO outcomes (outcome_id, tenant_id, evidence_record_id, task_id, task_family, provider, model, "
                "prompt_version, worker_id, outcome, cost_usd, latency_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (evidence_record_id) DO NOTHING",
                (f"out_{uuid.uuid4().hex}", evidence.tenant_id, evidence.record_id, evidence.task_id,
                 evidence.task_family.value, provider, model, pv, evidence.worker_id, o.value, evidence.cost_usd,
                 evidence.latency_ms),
            )
            if cur.rowcount == 1:  # chỉ cộng thống kê lần đầu (idempotent theo evidence)
                await c.execute(
                    "INSERT INTO router_stats (tenant_id, task_family, provider, model, prompt_version, n, verified_success, "
                    "verified_failure, unverified, total_cost_usd, total_latency_ms) VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (tenant_id, task_family, provider, model, prompt_version) DO UPDATE SET "
                    "n=router_stats.n+1, verified_success=router_stats.verified_success+EXCLUDED.verified_success, "
                    "verified_failure=router_stats.verified_failure+EXCLUDED.verified_failure, "
                    "unverified=router_stats.unverified+EXCLUDED.unverified, "
                    "total_cost_usd=router_stats.total_cost_usd+EXCLUDED.total_cost_usd, "
                    "total_latency_ms=router_stats.total_latency_ms+EXCLUDED.total_latency_ms, updated_at=now()",
                    (evidence.tenant_id, evidence.task_family.value, provider, model, pv,
                     int(o is Outcome.VERIFIED_SUCCESS), int(o is Outcome.VERIFIED_FAILURE), int(o is Outcome.UNVERIFIED),
                     evidence.cost_usd, evidence.latency_ms),
                )
            await c.commit()
        stage = DatasetStage.RAW if o is Outcome.UNVERIFIED else DatasetStage.VERIFIED
        rec = DatasetRecord(
            record_id=derived_id(evidence.record_id, stage),
            tenant_id=evidence.tenant_id,
            stage=stage,
            task_family=evidence.task_family,
            input={"goal": evidence.goal, "task_id": evidence.task_id, "prompt_version": pv},
            output={"outcome": o.value, "actions": [a.output for a in evidence.actions if a.ok],
                    "evidence": [e.summary for e in evidence.evidence if e.summary]},
            evidence_record_id=evidence.record_id if stage is DatasetStage.VERIFIED else None,
            outcome=o,
            source="runtime",
        )
        return await self.pipeline.insert(rec)

    async def _stats_rows(self, tenant_id: str, family: TaskFamily | None, by_prompt: bool) -> list[tuple[str, RouterStat]]:
        pv = "prompt_version" if by_prompt else "(''::text)"
        sql = (
            f"SELECT task_family, provider, model, {pv} AS pv, sum(n)::int n, sum(verified_success)::int vs, "
            "sum(verified_failure)::int vf, sum(unverified)::int u, sum(total_cost_usd) cost, sum(total_latency_ms)::bigint lat "
            "FROM router_stats WHERE tenant_id=%s AND provider <> 'unknown'"
        )
        args: list[object] = [tenant_id]
        if family:
            sql += " AND task_family=%s"
            args.append(family.value)
        sql += f" GROUP BY task_family, provider, model, 4 ORDER BY task_family, provider, model, 4"
        async with self.conn() as c:
            rows = await (await c.execute(sql, args)).fetchall()
        return [
            (r["pv"], RouterStat(task_family=TaskFamily(r["task_family"]), provider=ProviderKind(r["provider"]),
                                 model=r["model"], n=r["n"], verified_success=r["vs"], verified_failure=r["vf"],
                                 unverified=r["u"], total_cost_usd=r["cost"], total_latency_ms=r["lat"]))
            for r in rows
        ]

    async def stats(self, task_family: TaskFamily | None = None, tenant_id: str | None = None) -> list[RouterStat]:
        return [s for _, s in await self._stats_rows(tenant_id or self.default_tenant, task_family, False)]

    async def stats_by_prompt(self, tenant_id: str, task_family: TaskFamily | None = None) -> list[tuple[str, RouterStat]]:
        return await self._stats_rows(tenant_id, task_family, True)

    async def worker_stats(self, tenant_id: str, task_family: TaskFamily | None = None) -> dict[str, WorkerStat]:
        """Thống kê theo worker cho scheduler (chỉ outcome có worker_id)."""
        sql = (
            "SELECT worker_id, count(*)::int n, count(*) FILTER (WHERE outcome='VERIFIED_SUCCESS')::int vs, "
            "count(*) FILTER (WHERE outcome='VERIFIED_FAILURE')::int vf, avg(latency_ms)::float lat, avg(cost_usd)::float cost "
            "FROM outcomes WHERE tenant_id=%s AND worker_id IS NOT NULL"
        )
        args: list[object] = [tenant_id]
        if task_family:
            sql += " AND task_family=%s"
            args.append(task_family.value)
        async with self.conn() as c:
            rows = await (await c.execute(sql + " GROUP BY worker_id", args)).fetchall()
        return {
            r["worker_id"]: WorkerStat(worker_id=r["worker_id"], n=r["n"], verified_success=r["vs"],
                                       verified_failure=r["vf"], avg_latency_ms=r["lat"], avg_cost_usd=r["cost"])
            for r in rows
        }
