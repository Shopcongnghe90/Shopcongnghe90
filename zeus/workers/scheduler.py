"""Resource Scheduler: ràng buộc cứng + chấm điểm tuyến tính 10 yếu tố (ADR-013), scorer thay được.

Deterministic: cùng đầu vào => cùng ScheduleDecision (hoà điểm chọn worker_id nhỏ nhất).
Mọi quyết định mang feature vector + weights + candidates để lưu và học P(success | task, worker, state).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from psycopg.types.json import Jsonb

from zeus.contracts.models import (
    Outcome,
    ScheduleCandidate,
    ScheduleDecision,
    ScheduleFeatures,
    Task,
    TaskFamily,
    TaskNode,
    WorkerHeartbeat,
    WorkerInfo,
    WorkerStatus,
    utcnow,
)
from zeus.storage.db import aconnect
from zeus.workers.config import SchedulerConfig

FEATURE_NAMES = tuple(ScheduleFeatures.model_fields)

# (n_success, n_total) theo worker cho một family
Stats = Mapping[str, tuple[int, int]]
StatsProvider = Callable[[TaskFamily], Awaitable[Stats]]


class Scorer(Protocol):
    name: str

    def score(self, features: ScheduleFeatures, weights: Mapping[str, float], *, worker_id: str, family: TaskFamily) -> float: ...


class LinearScorer:
    name = "linear"

    def score(self, features: ScheduleFeatures, weights: Mapping[str, float], *, worker_id: str, family: TaskFamily) -> float:
        return sum(weights.get(k, 0.0) * getattr(features, k) for k in FEATURE_NAMES)


def success_rate(stats: Stats, worker_id: str, prior: float = 0.5) -> float:
    """Ước lượng Laplace (s+1)/(n+2); chưa có dữ liệu => 0.5."""
    s, n = stats.get(worker_id, (0, 0))
    return (s + 1) / (n + 2) if n or s else prior


class LearnedScorer:
    """STUB học: dùng thống kê P(success|family,worker) thay cho feature historical_success.

    Chưa phải mô hình học thật; chỉ chứng minh scorer thay được và dữ liệu schedule_decisions đủ để học.
    """

    name = "learned-stub"

    def __init__(self, stats: Stats | None = None) -> None:
        self.stats: Stats = stats or {}

    def score(self, features: ScheduleFeatures, weights: Mapping[str, float], *, worker_id: str, family: TaskFamily) -> float:
        total = 0.0
        for k in FEATURE_NAMES:
            v = success_rate(self.stats, worker_id) if k == "historical_success" else getattr(features, k)
            total += weights.get(k, 0.0) * v
        return total


def _wanted_localities(node: TaskNode) -> set[str]:
    raw = node.action.args.get("data_locality") if node.action else None
    if raw is None:
        return set()
    return {raw} if isinstance(raw, str) else set(raw)


class DeterministicScheduler:
    """Implements ``zeus.contracts.interfaces.Scheduler``."""

    def __init__(
        self,
        config: SchedulerConfig,
        *,
        scorer: Scorer | None = None,
        stats_provider: StatsProvider | None = None,
        stale_ttl_s: int = 60,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.scorer: Scorer = scorer or LinearScorer()
        self.stats_provider = stats_provider
        self.stale_ttl_s = stale_ttl_s
        self._now = now

    def _reject_reason(self, task: Task, node: TaskNode, w: WorkerInfo, hb: WorkerHeartbeat | None) -> str | None:
        if w.status in (WorkerStatus.OFFLINE, WorkerStatus.DRAINING):
            return f"status {w.status.value}"
        if task.tenant_id not in w.tenant_scope:
            return f"tenant {task.tenant_id} ngoài tenant_scope"
        missing = sorted(set(node.required_capabilities) - {c.name for c in w.capabilities})
        if missing:
            return f"thiếu capability {missing}"
        zones = self.config.zone_rules.get(task.family.value, self.config.default_zones)
        if w.network_zone not in zones:
            return f"network_zone {w.network_zone} không được phép cho {task.family.value}"
        if hb is not None and hb.at < self._now() - timedelta(seconds=self.stale_ttl_s):
            return "heartbeat quá hạn"
        return None

    def _features(self, task: Task, node: TaskNode, w: WorkerInfo, hb: WorkerHeartbeat | None, stats: Stats) -> ScheduleFeatures:
        req = set(node.required_capabilities)
        have = {c.name for c in w.capabilities}
        wanted = _wanted_localities(node)
        health = {WorkerStatus.ONLINE: 1.0, WorkerStatus.DEGRADED: 0.4}.get(w.status, 0.0)
        return ScheduleFeatures(
            capability_match=(len(req & have) / len(req)) if req else 1.0,
            worker_health=health,
            cpu_available=hb.cpu_available_pct / 100.0 if hb else 0.5,
            ram_available=min(1.0, hb.ram_available_mb / max(1, w.inventory.ram_mb)) if hb else 0.5,
            queue_load=1.0 / (1 + hb.queue_depth) if hb else 0.5,
            historical_success=success_rate(stats, w.worker_id),
            data_locality=(len(wanted & set(w.data_localities)) / len(wanted)) if wanted else 0.5,
            urgency=task.urgency / 3.0,
            risk=node.risk.rank / 3.0,
            failure_penalty=hb.recent_error_rate if hb else 0.0,
        )

    async def schedule(
        self,
        task: Task,
        node: TaskNode,
        workers: Sequence[WorkerInfo],
        heartbeats: Mapping[str, WorkerHeartbeat],
    ) -> ScheduleDecision:
        stats: Stats = await self.stats_provider(task.family) if self.stats_provider else {}
        weights = dict(self.config.weights)
        cands: list[ScheduleCandidate] = []
        for w in sorted(workers, key=lambda x: x.worker_id):
            hb = heartbeats.get(w.worker_id)
            feats = self._features(task, node, w, hb, stats)
            reason = self._reject_reason(task, node, w, hb)
            score = self.scorer.score(feats, weights, worker_id=w.worker_id, family=task.family)
            cands.append(ScheduleCandidate(worker_id=w.worker_id, score=round(score, 6), features=feats, rejected_reason=reason))
        ok = [c for c in cands if c.rejected_reason is None]
        base = dict(
            tenant_id=task.tenant_id, task_id=task.task_id, node_id=node.node_id, task_family=task.family,
            weights=weights, candidates=cands, policy_version=f"{self.config.policy_version}/{self.scorer.name}",
            decided_at=self._now(),
        )
        if not ok:
            why = "; ".join(f"{c.worker_id}: {c.rejected_reason}" for c in cands) or "không có worker nào đăng ký"
            return ScheduleDecision(worker_id=None, reason=f"không worker đủ điều kiện => xếp hàng ({why})", **base)
        best = max(ok, key=lambda c: (c.score, [-ord(ch) for ch in c.worker_id]))
        return ScheduleDecision(
            worker_id=best.worker_id, score=best.score, features=best.features,
            reason=f"chọn {best.worker_id} trong {len(ok)}/{len(cands)} ứng viên hợp lệ", **base,
        )


class PgScheduleStore:
    """Lưu ScheduleDecision và thống kê học."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def save(self, d: ScheduleDecision) -> None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute(
                """INSERT INTO schedule_decisions (decision_id, tenant_id, task_id, node_id, task_family, worker_id, score,
                       features, weights, candidates, policy_version, reason, decided_at, outcome)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (decision_id) DO NOTHING""",
                (d.decision_id, d.tenant_id, d.task_id, d.node_id, d.task_family.value, d.worker_id, d.score,
                 Jsonb(d.features.model_dump(mode="json")) if d.features else None, Jsonb(d.weights),
                 Jsonb([c.model_dump(mode="json") for c in d.candidates]), d.policy_version, d.reason, d.decided_at,
                 d.outcome.value if d.outcome else None),
            )

    async def get(self, decision_id: str) -> ScheduleDecision | None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute("SELECT * FROM schedule_decisions WHERE decision_id = %s", (decision_id,))
            r = await cur.fetchone()
        if not r:
            return None
        return ScheduleDecision.model_validate(
            {**r, "task_family": r["task_family"], "candidates": r["candidates"], "features": r["features"]}
        )

    async def record_outcome(self, decision_id: str, outcome: Outcome) -> None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute("UPDATE schedule_decisions SET outcome = %s WHERE decision_id = %s", (outcome.value, decision_id))

    async def success_stats(self, family: TaskFamily) -> dict[str, tuple[int, int]]:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                """SELECT worker_id, count(*) FILTER (WHERE outcome = 'VERIFIED_SUCCESS') AS s,
                          count(*) FILTER (WHERE outcome IN ('VERIFIED_SUCCESS','VERIFIED_FAILURE')) AS n
                   FROM schedule_decisions WHERE task_family = %s AND worker_id IS NOT NULL AND outcome IS NOT NULL
                   GROUP BY worker_id""",
                (family.value,),
            )
            rows = await cur.fetchall()
        return {r["worker_id"]: (int(r["s"]), int(r["n"])) for r in rows}
