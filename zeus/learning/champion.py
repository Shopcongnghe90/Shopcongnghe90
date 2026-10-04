"""Champion/Challenger state machine.

CHAMPION -> CHALLENGER -> OFFLINE_EVAL -> REPLAY -> SHADOW -> CANARY -> COMPARE -> promote | rollback

Bất biến an toàn:
- Không có đường tắt: mỗi bước cần báo cáo đạt ngưỡng của đúng bước đó.
- Quan sát production KHÔNG BAO GIỜ tự promote: ``suggest`` chỉ tạo đề xuất (CHALLENGER); promote đòi eval/replay/shadow
  đã đạt + ``decide_promotion`` thắng + (rủi ro >= R2) người duyệt (gate G10).
- Promote ghi Decision Ledger + Brain memory ``decision``.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from zeus.brain.pg import PgBase
from zeus.brain.store import PgMemoryStore
from zeus.contracts.models import MemoryItem, MemoryKind, RiskLevel, RouterStat, TrustLevel, utcnow


class CCState(str, Enum):
    CHAMPION = "CHAMPION"
    CHALLENGER = "CHALLENGER"
    OFFLINE_EVAL = "OFFLINE_EVAL"
    REPLAY = "REPLAY"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    COMPARE = "COMPARE"


_NEXT = {
    CCState.CHALLENGER: CCState.OFFLINE_EVAL,
    CCState.OFFLINE_EVAL: CCState.REPLAY,
    CCState.REPLAY: CCState.SHADOW,
    CCState.SHADOW: CCState.CANARY,
    CCState.CANARY: CCState.COMPARE,
}


class PromotionBlocked(RuntimeError):
    pass


class Thresholds(BaseModel):
    min_eval_cases: int = 18
    min_eval_pass_rate: float = 0.9
    min_replay_n: int = 20
    min_shadow_n: int = 20
    min_canary_n: int = 30
    z: float = 1.64  # ~95% một phía
    max_cost_ratio: float = 1.0  # cost/verified success của challenger <= champion * ratio


class PromotionDecision(BaseModel):
    promote: bool
    reasons: list[str] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)


def wilson_lower(successes: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - margin) / denom


def decide_promotion(champion: RouterStat, challenger: RouterStat, th: Thresholds | None = None) -> PromotionDecision:
    """Hàm thuần: promote khi challenger đủ mẫu, cận dưới Wilson của tỉ lệ thành công >= tỉ lệ champion, và
    cost/verified success không tệ hơn. Luôn trả report giải thích."""
    th = th or Thresholds()
    cv = challenger.verified_success + challenger.verified_failure
    hv = champion.verified_success + champion.verified_failure
    lower = wilson_lower(challenger.verified_success, cv, th.z)
    c_cost, h_cost = challenger.cost_per_verified_success, champion.cost_per_verified_success
    reasons: list[str] = []
    if cv < th.min_canary_n:
        reasons.append(f"challenger mới có {cv} mẫu verified < {th.min_canary_n}")
    if hv < th.min_canary_n:
        reasons.append(f"champion mới có {hv} mẫu verified < {th.min_canary_n}")
    if lower < champion.success_rate:
        reasons.append(f"cận dưới success challenger {lower:.3f} < success champion {champion.success_rate:.3f}")
    if c_cost is None:
        reasons.append("challenger chưa có verified success để tính cost/verified success")
    elif h_cost is not None and c_cost > h_cost * th.max_cost_ratio:
        reasons.append(f"cost/verified success {c_cost:.4f} > {h_cost:.4f} x {th.max_cost_ratio}")
    return PromotionDecision(
        promote=not reasons,
        reasons=reasons or ["challenger thắng có khoảng tin cậy và chi phí không tệ hơn"],
        report={
            "champion": {"n_verified": hv, "success_rate": champion.success_rate, "cost_per_verified_success": h_cost},
            "challenger": {"n_verified": cv, "success_rate": challenger.success_rate, "wilson_lower": lower,
                           "cost_per_verified_success": c_cost},
            "thresholds": th.model_dump(),
        },
    )


class Subject(BaseModel):
    tenant_id: str
    subject_id: str
    subject_kind: str
    task_family: str | None = None
    risk: RiskLevel = RiskLevel.R0
    champion: str
    challenger: str | None = None
    state: CCState = CCState.CHAMPION
    history: list[dict[str, Any]] = Field(default_factory=list)


class ChampionChallenger(PgBase):
    def __init__(self, dsn: str, memory: PgMemoryStore | None = None, thresholds: Thresholds | None = None) -> None:
        super().__init__(dsn)
        self.memory = memory or PgMemoryStore(dsn)
        self.th = thresholds or Thresholds()

    async def _get(self, c: Any, tenant_id: str, subject_id: str, lock: bool = False) -> Subject:
        r = await (await c.execute(
            "SELECT * FROM champion_challenger WHERE tenant_id=%s AND subject_id=%s" + (" FOR UPDATE" if lock else ""),
            (tenant_id, subject_id))).fetchone()
        if r is None:
            raise KeyError(subject_id)
        return Subject(tenant_id=r["tenant_id"], subject_id=r["subject_id"], subject_kind=r["subject_kind"],
                       task_family=r["task_family"], risk=RiskLevel(r["risk"]), champion=r["champion"],
                       challenger=r["challenger"], state=CCState(r["state"]), history=r["history"])

    async def _save(self, c: Any, s: Subject) -> None:
        await c.execute(
            "UPDATE champion_challenger SET champion=%s, challenger=%s, state=%s, history=%s, updated_at=now() "
            "WHERE tenant_id=%s AND subject_id=%s",
            (s.champion, s.challenger, s.state.value, Jsonb(s.history), s.tenant_id, s.subject_id))

    async def get(self, tenant_id: str, subject_id: str) -> Subject:
        async with self.conn() as c:
            return await self._get(c, tenant_id, subject_id)

    async def register(self, tenant_id: str, subject_id: str, subject_kind: str, champion: str,
                       task_family: str | None = None, risk: RiskLevel = RiskLevel.R0) -> Subject:
        async with self.conn() as c:
            await c.execute(
                "INSERT INTO champion_challenger (subject_id, tenant_id, subject_kind, task_family, risk, champion, state) "
                "VALUES (%s,%s,%s,%s,%s,%s,'CHAMPION') ON CONFLICT DO NOTHING",
                (subject_id, tenant_id, subject_kind, task_family, risk.value, champion))
            return await self._get(c, tenant_id, subject_id)

    async def _transition(self, tenant_id: str, subject_id: str, expect: CCState, to: CCState, note: dict[str, Any],
                          challenger: str | None = None) -> Subject:
        async with self.conn(autocommit=False) as c:
            s = await self._get(c, tenant_id, subject_id, lock=True)
            if s.state is not expect:
                raise PromotionBlocked(f"{subject_id}: cần ở {expect.value}, đang {s.state.value}")
            s.history.append({"at": utcnow().isoformat(), "from": s.state.value, "to": to.value, **note})
            s.state = to
            if challenger is not None:
                s.challenger = challenger
            await self._save(c, s)
            await c.commit()
            return s

    async def propose(self, tenant_id: str, subject_id: str, challenger: str, origin: str = "human") -> Subject:
        """CHAMPION -> CHALLENGER. ``origin='production_observation'`` vẫn chỉ là đề xuất, không bao giờ là promote."""
        return await self._transition(tenant_id, subject_id, CCState.CHAMPION, CCState.CHALLENGER,
                                      {"challenger": challenger, "origin": origin}, challenger=challenger)

    suggest = propose

    async def advance(self, tenant_id: str, subject_id: str, report: dict[str, Any] | None = None) -> Subject:
        """Đi 1 bước tới trạng thái kế, kiểm báo cáo của bước vừa hoàn thành."""
        s = await self.get(tenant_id, subject_id)
        if s.state not in _NEXT:
            raise PromotionBlocked(f"không thể advance từ {s.state.value} (COMPARE dùng promote/rollback)")
        report = report or {}
        th = self.th
        if s.state is CCState.OFFLINE_EVAL:  # rời OFFLINE_EVAL: cần eval report đạt
            n, rate, reg = report.get("n_cases", 0), report.get("pass_rate", 0.0), report.get("regression_failures", 1)
            if n < th.min_eval_cases or rate < th.min_eval_pass_rate or reg != 0 or not report.get("eval_run_id"):
                raise PromotionBlocked(
                    f"offline eval chưa đạt: n={n} (>={th.min_eval_cases}), pass_rate={rate} (>={th.min_eval_pass_rate}), "
                    f"regression_failures={reg} (=0), cần eval_run_id")
        elif s.state is CCState.REPLAY:
            if report.get("n", 0) < th.min_replay_n or not report.get("no_regressions"):
                raise PromotionBlocked("replay chưa đạt (đủ mẫu và no_regressions)")
        elif s.state is CCState.SHADOW:
            if report.get("n", 0) < th.min_shadow_n or report.get("side_effects", 1) != 0:
                raise PromotionBlocked("shadow chưa đạt (đủ mẫu, side_effects=0)")
        elif s.state is CCState.CANARY and not report.get("canary_ok"):
            raise PromotionBlocked("canary chưa đạt (canary_ok)")
        return await self._transition(tenant_id, subject_id, s.state, _NEXT[s.state],
                                      {"report": {k: v for k, v in report.items() if k != "raw"}})

    async def promote(self, tenant_id: str, subject_id: str, champion_stat: RouterStat, challenger_stat: RouterStat,
                      approved_by: str | None = None) -> PromotionDecision:
        """COMPARE -> CHAMPION(mới) nếu decide_promotion thắng. R2+ bắt buộc ``approved_by`` (người)."""
        s = await self.get(tenant_id, subject_id)
        if s.state is not CCState.COMPARE:
            raise PromotionBlocked(f"chỉ promote từ COMPARE (đang {s.state.value}); không promote từ quan sát production")
        if s.risk in (RiskLevel.R2, RiskLevel.R3) and not (approved_by and approved_by.startswith("human:")):
            raise PromotionBlocked(f"rủi ro {s.risk.value}: cần phê duyệt người (approved_by='human:<id>'), gate G10")
        decision = decide_promotion(champion_stat, challenger_stat, self.th)
        if not decision.promote:
            raise PromotionBlocked("; ".join(decision.reasons))
        old = s.champion
        await self._finish(s, to_champion=s.challenger, note={"action": "promote", "approved_by": approved_by,
                                                              "decision": decision.model_dump()})
        did = await self.memory.record_decision(
            tenant_id, f"Promote {subject_id}: {old} -> {s.challenger}",
            f"{s.challenger} thay {old} làm champion cho {subject_id}", "; ".join(decision.reasons), evidence_ref=subject_id)
        await self.memory.put(MemoryItem(
            tenant_id=tenant_id, kind=MemoryKind.DECISION, title=f"Promote {subject_id}", trust=TrustLevel.VERIFIED,
            content=f"{s.challenger} là champion của {subject_id} (trước đó {old}). Ledger {did}.", source_ref=did,
            tags=["src:decision", f"topic:champion:{subject_id}"]))
        return decision

    async def rollback(self, tenant_id: str, subject_id: str, reason: str) -> Subject:
        s = await self.get(tenant_id, subject_id)
        if s.state is CCState.CHAMPION:
            raise PromotionBlocked("không có challenger để rollback")
        return await self._finish(s, to_champion=None, note={"action": "rollback", "reason": reason})

    async def _finish(self, s: Subject, to_champion: str | None, note: dict[str, Any]) -> Subject:
        async with self.conn(autocommit=False) as c:
            cur = await self._get(c, s.tenant_id, s.subject_id, lock=True)
            if cur.state is not s.state:
                raise PromotionBlocked("trạng thái đã đổi (race)")
            cur.history.append({"at": utcnow().isoformat(), "from": cur.state.value, "to": "CHAMPION", **note})
            if to_champion:
                cur.champion = to_champion
            cur.challenger, cur.state = None, CCState.CHAMPION
            await self._save(c, cur)
            await c.commit()
            return cur
