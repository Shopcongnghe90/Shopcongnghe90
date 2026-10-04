"""Fake in-memory deterministic cho MỌI Protocol trong zeus.contracts.interfaces.

Mục đích: 4 workstream test độc lập, không cần mạng/GPU/API key/Claude Cloud. Fake KHÔNG phải
implementation production; hành vi giữ tối giản nhưng tôn trọng bất biến của contract
(policy theo RiskLevel, approval trước side-effect R2+, evidence quyết định outcome...).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Awaitable

from zeus.contracts.api import AssignmentResult, TaskAssignment
from zeus.contracts.interfaces import ApprovalRequired, PolicyDenied, ProviderUnavailable
from zeus.contracts.models import (
    ActionResult,
    ActionSpec,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Channel,
    ChannelIdentity,
    ContextPacket,
    Cost,
    Critique,
    DatasetRecord,
    DatasetStage,
    Event,
    EventKind,
    EvidenceRecord,
    EvidenceStrength,
    Intent,
    MemoryItem,
    MemoryKind,
    ModelRequest,
    ModelResponse,
    Outcome,
    Plan,
    PolicyDecision,
    PolicyEffect,
    ProviderCapability,
    ProviderKind,
    RetrievalHit,
    RetrievalQuery,
    RiskAssessment,
    RiskLevel,
    RouteCandidate,
    RouteDecision,
    RouterStat,
    RouteStrategy,
    ScheduleCandidate,
    ScheduleDecision,
    ScheduleFeatures,
    Task,
    TaskFamily,
    TaskGraph,
    TaskNode,
    TaskStatus,
    TrustLevel,
    TypedAction,
    Usage,
    Verdict,
    VerdictDecision,
    WorkerHeartbeat,
    WorkerInfo,
    WorkerStatus,
    utcnow,
)

# ============================================================== A


class FakeModelProvider:
    """Provider giả: trả lời theo ``script`` (callable) hoặc echo. ``fail=True`` mô phỏng mất provider."""

    def __init__(
        self,
        kind: ProviderKind = ProviderKind.FAKE,
        model: str = "fake-1",
        *,
        fail: bool = False,
        local: bool = False,
        price_in: float = 0.0,
        price_out: float = 0.0,
        script: Callable[[ModelRequest], str] | None = None,
    ) -> None:
        self.kind = kind
        self.model = model
        self.fail = fail
        self.local = local
        self.price_in = price_in
        self.price_out = price_out
        self.script = script
        self.calls: list[ModelRequest] = []

    def capabilities(self) -> list[ProviderCapability]:
        return [
            ProviderCapability(
                provider=self.kind,
                model=self.model,
                local=self.local,
                price_in_per_mtok=self.price_in,
                price_out_per_mtok=self.price_out,
                available=not self.fail,
            )
        ]

    async def complete(self, request: ModelRequest, model: str) -> ModelResponse:
        self.calls.append(request)
        if self.fail:
            raise ProviderUnavailable(f"{self.kind.value}:{model} unavailable (fake)")
        text = self.script(request) if self.script else f"[{self.kind.value}:{model}] " + request.messages[-1].content
        usage = Usage(
            input_tokens=sum(len(m.content.split()) for m in request.messages),
            output_tokens=len(text.split()),
        )
        return ModelResponse(
            request_id=request.request_id,
            provider=self.kind,
            model=model,
            text=text,
            usage=usage,
            cost=Cost.from_usage(usage, self.price_in, self.price_out),
            latency_ms=1,
            finish_reason="stop",
        )

    async def health(self) -> bool:
        return not self.fail


class FakeModelBroker:
    """Broker giả: thứ tự provider cố định; bỏ cloud khi request.allow_cloud=False/contains_pii;
    fallback khi ProviderUnavailable; hết provider => ProviderUnavailable (caller xếp hàng)."""

    def __init__(self, providers: Sequence[FakeModelProvider]) -> None:
        self.providers = list(providers)

    def _eligible(self, request: ModelRequest) -> list[FakeModelProvider]:
        out = []
        for p in self.providers:
            if (not request.allow_cloud or request.contains_pii) and not p.local:
                continue
            out.append(p)
        if request.preferred:
            out.sort(key=lambda p: (p.kind not in request.preferred,))
        return out

    def route(self, request: ModelRequest) -> RouteDecision:
        eligible = self._eligible(request)
        if not eligible:
            raise ProviderUnavailable("no eligible provider")
        cands = [RouteCandidate(provider=p.kind, model=p.model, score=1.0 - i * 0.1, prior=1.0 - i * 0.1) for i, p in enumerate(eligible)]
        return RouteDecision(
            task_family=request.task_family,
            role=request.role,
            provider=cands[0].provider,
            model=cands[0].model,
            strategy=RouteStrategy.BOOTSTRAP_PRIOR,
            candidates=cands,
            fallbacks=cands[1:],
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        route = self.route(request)
        tried: list[str] = []
        for p in self._eligible(request):
            try:
                resp = await p.complete(request, p.model)
            except ProviderUnavailable:
                tried.append(f"{p.kind.value}:{p.model}")
                continue
            return resp.model_copy(update={"fallback_chain": tried, "route_id": route.route_id})
        raise ProviderUnavailable(f"all providers failed: {tried}")


_FAMILY_KEYWORDS: list[tuple[TaskFamily, tuple[str, ...]]] = [
    (TaskFamily.ERP_BUG, ("lỗi erp", "erp lỗi", "odoo lỗi", "erp bug")),
    (TaskFamily.ERP_FEATURE, ("erp", "odoo")),
    (TaskFamily.SHOPEE_ISSUE, ("shopee",)),
    (TaskFamily.ZALO_ISSUE, ("zalo",)),
    (TaskFamily.FACEBOOK_ISSUE, ("facebook", "fanpage", "messenger")),
    (TaskFamily.DOMAIN_PROVISIONING, ("tên miền", "domain")),
    (TaskFamily.WEBSITE_EDIT, ("website", "trang web")),
    (TaskFamily.DEPLOYMENT, ("deploy", "triển khai")),
    (TaskFamily.SECURITY, ("bảo mật", "security", "mật khẩu")),
    (TaskFamily.CUSTOMER_SUPPORT, ("đơn hàng", "giá", "còn hàng", "ship")),
]


class FakeIntentEngine:
    async def classify(self, event: Event) -> Intent:
        text = (event.text or "").lower()
        family = TaskFamily.GENERAL
        for fam, kws in _FAMILY_KEYWORDS:
            if any(k in text for k in kws):
                family = fam
                break
        return Intent(
            event_id=event.event_id,
            tenant_id=event.tenant_id,
            family=family,
            summary=(event.text or "")[:200],
            confidence=0.9 if family is not TaskFamily.GENERAL else 0.4,
            needs_clarification=family is TaskFamily.GENERAL,
            classifier="rules",
        )


_PHONE_RE = re.compile(r"(?:\+?84|0)(?:\d[\s.-]?){8,10}\d")
_INJECTION_RE = re.compile(r"(ignore (all|previous) instructions|bỏ qua (mọi|các) chỉ dẫn|system prompt)", re.I)


class FakeRiskEngine:
    _FAMILY_RISK = {
        TaskFamily.DOMAIN_PROVISIONING: RiskLevel.R3,
        TaskFamily.DEPLOYMENT: RiskLevel.R2,
        TaskFamily.SECURITY: RiskLevel.R2,
        TaskFamily.ERP_BUG: RiskLevel.R1,
        TaskFamily.ERP_FEATURE: RiskLevel.R1,
    }

    async def assess(self, event: Event, intent: Intent) -> RiskAssessment:
        level = self._FAMILY_RISK.get(intent.family, RiskLevel.R0)
        text = event.text or ""
        pii = bool(_PHONE_RE.search(text))
        inj = bool(_INJECTION_RE.search(text))
        reasons = [f"family={intent.family.value}"]
        if pii:
            reasons.append("pii.phone")
        if inj:
            reasons.append("prompt_injection_pattern")
        return RiskAssessment(
            level=level,
            reasons=reasons,
            requires_approval=level >= RiskLevel.R2,
            pii_detected=pii,
            data_classes=["pii.phone"] if pii else [],
            injection_suspected=inj,
        )

    def assess_action(self, action: TypedAction, spec: ActionSpec) -> RiskAssessment:
        level = spec.risk
        if spec.external and level < RiskLevel.R2:
            level = RiskLevel.R2
        return RiskAssessment(level=level, reasons=[f"spec={spec.name}"], requires_approval=level >= RiskLevel.R2)


class FakePlanner:
    def __init__(self, name: str = "fake-planner", provider: ProviderKind = ProviderKind.FAKE) -> None:
        self.name = name
        self.provider = provider

    async def plan(self, task: Task, context: ContextPacket) -> Plan:
        graph = TaskGraph(
            task_id=task.task_id,
            nodes=[
                TaskNode(node_id="analyze", title="Phân tích", family=task.family, risk=RiskLevel.R0),
                TaskNode(node_id="execute", title="Thực hiện", family=task.family, risk=task.risk, depends_on=["analyze"]),
                TaskNode(node_id="verify", title="Kiểm chứng", family=task.family, risk=RiskLevel.R0, depends_on=["execute"]),
            ],
        )
        return Plan(task_id=task.task_id, graph=graph, rationale="fake 3-step plan", planner=f"{self.provider.value}:{self.name}")


class FakeCritic:
    def __init__(self, reviewer: str = "fake-critic", provider: ProviderKind = ProviderKind.FAKE, approve: bool = True) -> None:
        self.reviewer = reviewer
        self.provider = provider
        self._approve = approve

    async def critique(self, plan: Plan, context: ContextPacket) -> Critique:
        return Critique(plan_id=plan.plan_id, reviewer=self.reviewer, reviewer_provider=self.provider, approve=self._approve)


class FakeJudge:
    """Judge deterministic: VERIFIED_SUCCESS chỉ khi có EvidenceRecord đã VERIFIED_SUCCESS (đã qua validator)."""

    async def judge(self, task: Task, evidence: Sequence[EvidenceRecord]) -> Verdict:
        ids = [e.record_id for e in evidence]
        if any(e.final_outcome is Outcome.VERIFIED_SUCCESS for e in evidence):
            return Verdict(task_id=task.task_id, decision=VerdictDecision.PASS, outcome=Outcome.VERIFIED_SUCCESS, judge="fake-judge", evidence_ids=ids)
        if any(e.final_outcome is Outcome.VERIFIED_FAILURE for e in evidence):
            return Verdict(task_id=task.task_id, decision=VerdictDecision.FAIL, outcome=Outcome.VERIFIED_FAILURE, judge="fake-judge", evidence_ids=ids)
        return Verdict(task_id=task.task_id, decision=VerdictDecision.NEEDS_HUMAN, outcome=Outcome.UNVERIFIED, judge="fake-judge", evidence_ids=ids, reasons=["no strong evidence"])


class FakePolicyEngine:
    """R0/R1 ALLOW; R2/R3 REQUIRE_APPROVAL; tên action trong ``deny`` => DENY; external => tối thiểu R2."""

    def __init__(self, deny: Sequence[str] = ()) -> None:
        self.deny = set(deny)

    def evaluate(self, action: TypedAction, spec: ActionSpec, task: Task | None = None) -> PolicyDecision:
        if action.name in self.deny:
            return PolicyDecision(effect=PolicyEffect.DENY, reasons=["denylisted"], rule_ids=["fake.deny"])
        level = spec.risk if not spec.external else RiskLevel.max(spec.risk, RiskLevel.R2)
        if level >= RiskLevel.R2 and not action.dry_run:
            return PolicyDecision(effect=PolicyEffect.REQUIRE_APPROVAL, reasons=[f"risk {level.value}"], rule_ids=["fake.r2plus"], required_approvers=1)
        return PolicyDecision(effect=PolicyEffect.ALLOW, reasons=[f"risk {level.value}"], rule_ids=["fake.allow"])


class FakeToolProvider:
    def __init__(self, specs: Sequence[ActionSpec], handler: Callable[[TypedAction], Awaitable[dict[str, Any]]] | None = None) -> None:
        self._specs = list(specs)
        self._handler = handler
        self.executed: list[TypedAction] = []

    def specs(self) -> list[ActionSpec]:
        return list(self._specs)

    async def execute(self, action: TypedAction, spec: ActionSpec) -> ActionResult:
        self.executed.append(action)
        output = await self._handler(action) if self._handler else {"echo": action.args}
        return ActionResult(action_id=action.action_id, ok=True, output=output, executed_by="fake")


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self.items: dict[str, ApprovalRequest] = {}

    async def request(self, req: ApprovalRequest) -> ApprovalRequest:
        self.items[req.approval_id] = req
        return req

    async def decide(self, decision: ApprovalDecision) -> ApprovalRequest:
        req = self.items.get(decision.approval_id)
        if req is None:
            raise KeyError(decision.approval_id)
        if req.status is not ApprovalStatus.PENDING:
            raise ValueError(f"approval {req.approval_id} already {req.status.value}")
        if req.expires_at and decision.decided_at > req.expires_at:
            updated = req.model_copy(update={"status": ApprovalStatus.EXPIRED})
        else:
            updated = req.model_copy(
                update={"status": ApprovalStatus(decision.status), "decided_by": decision.decided_by, "decided_at": decision.decided_at, "comment": decision.comment}
            )
        self.items[req.approval_id] = updated
        return updated

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        return self.items.get(approval_id)

    async def list(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        return [a for a in self.items.values() if a.tenant_id == tenant_id and (status is None or a.status is status)]


class FakeToolGateway:
    """validate (tên/tenant) -> policy -> approval -> execute. Approval đã APPROVED cho đúng action_id thì chạy."""

    def __init__(self, providers: Sequence[FakeToolProvider], policy: FakePolicyEngine, approvals: InMemoryApprovalStore) -> None:
        self.policy = policy
        self.approvals = approvals
        self._routes: dict[str, tuple[FakeToolProvider, ActionSpec]] = {}
        for p in providers:
            for s in p.specs():
                if s.name in self._routes:
                    raise ValueError(f"duplicate action spec {s.name}")
                self._routes[s.name] = (p, s)

    def list_actions(self) -> list[ActionSpec]:
        return [s for _, s in self._routes.values()]

    async def execute(self, action: TypedAction, task: Task | None = None) -> ActionResult:
        if action.name not in self._routes:
            raise PolicyDenied(f"unknown action {action.name}")
        provider, spec = self._routes[action.name]
        if task is not None and task.tenant_id != action.tenant_id:
            raise PolicyDenied("tenant mismatch between task and action")
        decision = self.policy.evaluate(action, spec, task)
        if decision.effect is PolicyEffect.DENY:
            raise PolicyDenied("; ".join(decision.reasons))
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            approved = [
                a
                for a in await self.approvals.list(action.tenant_id, ApprovalStatus.APPROVED)
                if a.action.action_id == action.action_id
            ]
            if not approved:
                req = await self.approvals.request(
                    ApprovalRequest(tenant_id=action.tenant_id, task_id=action.task_id, action=action, risk=spec.risk, summary_vi=f"Duyệt thao tác {spec.name}")
                )
                raise ApprovalRequired(req)
        return await provider.execute(action, spec)


# ============================================================== B


class InMemoryEvidenceStore:
    def __init__(self) -> None:
        self.records: dict[str, EvidenceRecord] = {}

    async def put(self, record: EvidenceRecord) -> str:
        if record.record_id in self.records:
            raise ValueError("evidence records are append-only")
        self.records[record.record_id] = record
        return record.record_id

    async def get(self, record_id: str, tenant_id: str | None = None) -> EvidenceRecord | None:
        rec = self.records.get(record_id)
        return rec if rec is not None and (tenant_id is None or rec.tenant_id == tenant_id) else None

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[EvidenceRecord]:
        return [r for r in self.records.values() if r.tenant_id == tenant_id and r.task_id == task_id]


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, MemoryItem] = {}

    async def put(self, item: MemoryItem) -> str:
        self.items[item.memory_id] = item
        return item.memory_id

    async def get(self, tenant_id: str, memory_id: str) -> MemoryItem | None:
        item = self.items.get(memory_id)
        return item if item and item.tenant_id == tenant_id else None

    async def list(self, tenant_id: str, kind: MemoryKind | None = None, limit: int = 100) -> list[MemoryItem]:
        out = [m for m in self.items.values() if m.tenant_id == tenant_id and (kind is None or m.kind is kind)]
        return out[:limit]


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


class FakeEmbeddingProvider:
    """Embedding băm deterministic (bag-of-words hashing, chuẩn hoá L2)."""

    def __init__(self, dim: int = 64, model_name: str = "fake-hash-embed") -> None:
        self.dim = dim
        self.model_name = model_name

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in _tokens(t):
                h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class FakeReranker:
    async def rerank(self, query: str, hits: Sequence[RetrievalHit], top_k: int) -> list[RetrievalHit]:
        q = set(_tokens(query))
        scored = []
        for h in hits:
            overlap = len(q & set(_tokens(h.memory.content))) / (len(q) or 1)
            scored.append(h.model_copy(update={"rerank_score": overlap, "score": overlap}))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]


class FakeBrainRetriever:
    """Lexical overlap trên MemoryStore; lọc tenant/kind/trust/as_of; budget theo ~token (từ)."""

    def __init__(self, store: InMemoryMemoryStore) -> None:
        self.store = store

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        q = set(_tokens(query.text))
        hits = []
        for m in await self.store.list(query.tenant_id, limit=10_000):
            if query.kinds and m.kind not in query.kinds:
                continue
            if query.require_verified and m.trust is not TrustLevel.VERIFIED:
                continue
            if query.tags and not set(query.tags) <= set(m.tags):
                continue
            if query.as_of and (m.valid_from > query.as_of or (m.valid_to and m.valid_to <= query.as_of)):
                continue
            s = len(q & set(_tokens(m.content))) / (len(q) or 1)
            if s > 0:
                hits.append(RetrievalHit(memory=m, score=s, lexical_score=s, why="lexical overlap"))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: query.top_k]

    async def build_context(self, query: RetrievalQuery, task: Task | None = None) -> ContextPacket:
        hits = await self.retrieve(query)
        kept, used = [], 0
        for h in hits:
            cost = len(_tokens(h.memory.content))
            if query.token_budget and used + cost > query.token_budget:
                continue
            kept.append(h)
            used += cost
        return ContextPacket(
            tenant_id=query.tenant_id,
            task_id=task.task_id if task else None,
            goal=task.goal if task else query.text,
            hits=kept,
            token_estimate=used,
            token_budget=query.token_budget,
        )


class InMemoryOutcomeRecorder:
    def __init__(self) -> None:
        self.dataset: list[DatasetRecord] = []
        self._stats: dict[tuple[TaskFamily, ProviderKind, str, str], RouterStat] = {}

    async def record(self, evidence: EvidenceRecord) -> DatasetRecord:
        verified = evidence.final_outcome is not Outcome.UNVERIFIED
        rec = DatasetRecord(
            tenant_id=evidence.tenant_id,
            stage=DatasetStage.VERIFIED if verified else DatasetStage.NORMALIZED,
            task_family=evidence.task_family,
            input={"goal": evidence.goal},
            output={"actions": [a.action_id for a in evidence.actions]},
            evidence_record_id=evidence.record_id,
            outcome=evidence.final_outcome,
        )
        self.dataset.append(rec)
        if evidence.model_provider and evidence.model_name:
            key = (evidence.task_family, evidence.model_provider, evidence.model_name, evidence.tenant_id)
            st = self._stats.get(key) or RouterStat(task_family=key[0], provider=key[1], model=key[2])
            upd: dict[str, Any] = {
                "n": st.n + 1,
                "total_cost_usd": st.total_cost_usd + evidence.cost_usd,
                "total_latency_ms": st.total_latency_ms + evidence.latency_ms,
            }
            field = {
                Outcome.VERIFIED_SUCCESS: "verified_success",
                Outcome.VERIFIED_FAILURE: "verified_failure",
                Outcome.UNVERIFIED: "unverified",
            }[evidence.final_outcome]
            upd[field] = getattr(st, field) + 1
            self._stats[key] = st.model_copy(update=upd)
        return rec

    async def stats(self, task_family: TaskFamily | None = None, tenant_id: str | None = None) -> list[RouterStat]:
        return [
            s for k, s in self._stats.items()
            if (task_family is None or k[0] is task_family) and (tenant_id is None or k[3] == tenant_id)
        ]


# ============================================================== C


class InMemoryWorkerRegistry:
    def __init__(self) -> None:
        self.workers: dict[str, WorkerInfo] = {}
        self.heartbeats: dict[str, WorkerHeartbeat] = {}

    async def register(self, info: WorkerInfo) -> WorkerInfo:
        self.workers[info.worker_id] = info
        return info

    async def heartbeat(self, hb: WorkerHeartbeat) -> None:
        if hb.worker_id not in self.workers:
            raise KeyError(f"unknown worker {hb.worker_id}")
        self.heartbeats[hb.worker_id] = hb
        w = self.workers[hb.worker_id]
        if w.status is not hb.status:
            self.workers[hb.worker_id] = w.model_copy(update={"status": hb.status})

    async def get(self, worker_id: str) -> WorkerInfo | None:
        return self.workers.get(worker_id)

    async def list(self, status: WorkerStatus | None = None) -> list[WorkerInfo]:
        return [w for w in self.workers.values() if status is None or w.status is status]

    async def last_heartbeat(self, worker_id: str) -> WorkerHeartbeat | None:
        return self.heartbeats.get(worker_id)

    async def mark_stale(self, now: datetime, ttl_s: int) -> list[str]:
        stale = []
        for wid, w in self.workers.items():
            hb = self.heartbeats.get(wid)
            last = hb.at if hb else w.registered_at
            if now - last > timedelta(seconds=ttl_s) and w.status is not WorkerStatus.OFFLINE:
                self.workers[wid] = w.model_copy(update={"status": WorkerStatus.OFFLINE})
                stale.append(wid)
        return stale


DEFAULT_SCHEDULE_WEIGHTS: dict[str, float] = {
    "capability_match": 3.0,
    "worker_health": 2.0,
    "cpu_available": 1.0,
    "ram_available": 1.0,
    "queue_load": 1.0,
    "historical_success": 2.0,
    "data_locality": 1.0,
    "urgency": 0.5,
    "risk": -1.0,
    "failure_penalty": -2.0,
}


class FakeScheduler:
    """Scorer tuyến tính deterministic trên 10 yếu tố; loại worker thiếu capability/OFFLINE/DRAINING.
    Hoà điểm => chọn worker_id nhỏ nhất (ổn định)."""

    def __init__(self, weights: Mapping[str, float] | None = None, history: Mapping[str, float] | None = None) -> None:
        self.weights = dict(weights or DEFAULT_SCHEDULE_WEIGHTS)
        self.history = dict(history or {})

    async def schedule(
        self, task: Task, node: TaskNode, workers: Sequence[WorkerInfo], heartbeats: Mapping[str, WorkerHeartbeat]
    ) -> ScheduleDecision:
        cands: list[ScheduleCandidate] = []
        for w in sorted(workers, key=lambda x: x.worker_id):
            hb = heartbeats.get(w.worker_id)
            feats = ScheduleFeatures(
                capability_match=1.0 if w.has_capabilities(node.required_capabilities) else 0.0,
                worker_health=1.0 if w.status is WorkerStatus.ONLINE else 0.3 if w.status is WorkerStatus.DEGRADED else 0.0,
                cpu_available=(hb.cpu_available_pct / 100.0) if hb else 0.5,
                ram_available=min(1.0, (hb.ram_available_mb / max(1, w.inventory.ram_mb))) if hb else 0.5,
                queue_load=1.0 / (1 + hb.queue_depth) if hb else 0.5,
                historical_success=self.history.get(w.worker_id, 0.5),
                data_locality=1.0 if task.family.value in " ".join(w.data_localities) else 0.0,
                urgency=task.urgency / 3,
                risk=node.risk.rank / 3,
                failure_penalty=hb.recent_error_rate if hb else 0.0,
            )
            reject = None
            if feats.capability_match < 1.0:
                reject = "missing capability"
            elif w.status in (WorkerStatus.OFFLINE, WorkerStatus.DRAINING):
                reject = f"status {w.status.value}"
            elif task.tenant_id not in w.tenant_scope:
                reject = "tenant not in worker scope"
            score = sum(self.weights[k] * v for k, v in feats.model_dump().items())
            cands.append(ScheduleCandidate(worker_id=w.worker_id, score=round(score, 6), features=feats, rejected_reason=reject))
        ok = [c for c in cands if c.rejected_reason is None]
        best = min(ok, key=lambda c: (-c.score, c.worker_id), default=None)
        return ScheduleDecision(
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            node_id=node.node_id,
            task_family=task.family,
            worker_id=best.worker_id if best else None,
            score=best.score if best else 0.0,
            features=best.features if best else None,
            weights=self.weights,
            candidates=cands,
            reason="best score" if best else "no eligible worker; queued",
        )


class InMemoryAssignmentQueue:
    """Hàng đợi assignment theo worker; lease/hết hạn do C hiện thực thật ở bảng 3xx."""

    def __init__(self) -> None:
        self.queues: dict[str, list[TaskAssignment]] = {}
        self.tokens: dict[str, bytes | None] = {}
        self.owner: dict[str, str] = {}
        self.results: dict[str, AssignmentResult] = {}
        self.cancelled: set[str] = set()

    async def enqueue(self, assignment: TaskAssignment, worker_id: str, task_token: bytes | None = None) -> None:
        if assignment.assignment_id in self.owner:
            raise ValueError(f"duplicate assignment {assignment.assignment_id}")
        self.queues.setdefault(worker_id, []).append(assignment)
        self.tokens[assignment.assignment_id] = task_token
        self.owner[assignment.assignment_id] = worker_id

    async def poll(self, worker_id: str, max_assignments: int = 1) -> list[TaskAssignment]:
        q = self.queues.get(worker_id, [])
        out = [a for a in q if a.assignment_id not in self.cancelled][:max_assignments]
        self.queues[worker_id] = [a for a in q if a not in out and a.assignment_id not in self.cancelled]
        return out

    async def complete(self, result: AssignmentResult) -> bytes | None:
        aid = result.assignment_id
        if self.owner.get(aid) != result.worker_id:
            raise PermissionError("assignment does not belong to this worker")
        if aid in self.results:
            return None  # idempotent: kết quả lặp không hoàn thành activity lần 2
        self.results[aid] = result
        return self.tokens.get(aid)

    async def cancel(self, assignment_id: str) -> None:
        self.cancelled.add(assignment_id)

    async def pending_cancellations(self, worker_id: str) -> list[str]:
        return sorted(a for a in self.cancelled if self.owner.get(a) == worker_id and a not in self.results)


# ============================================================== D


class FakeChannelAdapter:
    """Webhook giả ký HMAC-SHA256(secret, body) ở header ``X-Fake-Signature`` (hex)."""

    def __init__(self, secret: str, channel: Channel = Channel.API) -> None:
        self.channel = channel
        self._secret = secret.encode()

    def sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        got = {k.lower(): v for k, v in headers.items()}.get("x-fake-signature", "")
        return hmac.compare_digest(got, self.sign(body))

    def normalize(self, headers: Mapping[str, str], body: bytes) -> list[Event]:
        if not self.verify(headers, body):
            raise PermissionError("invalid signature")
        payload = json.loads(body)
        return [
            Event(
                channel=self.channel,
                kind=EventKind.MESSAGE,
                external_id=str(payload.get("id")),
                sender=ChannelIdentity(channel_user_id=str(payload.get("from", "unknown"))),
                text=payload.get("text"),
                signature_verified=True,
                untrusted=True,
            )
        ]

    def outbound_specs(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                name=f"channel.{self.channel.value}.send_message",
                risk=RiskLevel.R2,
                external=True,
                reversible=False,
                owner="D",
                input_schema={"type": "object", "required": ["to", "text"]},
            )
        ]


class FakeWorkbenchDataSource:
    def __init__(
        self,
        tasks: Sequence[Task] = (),
        approvals: InMemoryApprovalStore | None = None,
        registry: InMemoryWorkerRegistry | None = None,
        evidence: InMemoryEvidenceStore | None = None,
    ) -> None:
        self.tasks = {t.task_id: t for t in tasks}
        self.approvals = approvals or InMemoryApprovalStore()
        self.registry = registry or InMemoryWorkerRegistry()
        self.evidence = evidence or InMemoryEvidenceStore()

    async def list_tasks(self, tenant_id: str, status: TaskStatus | None = None, limit: int = 50) -> list[Task]:
        out = [t for t in self.tasks.values() if t.tenant_id == tenant_id and (status is None or t.status is status)]
        return sorted(out, key=lambda t: t.created_at, reverse=True)[:limit]

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None:
        t = self.tasks.get(task_id)
        return t if t and t.tenant_id == tenant_id else None

    async def list_approvals(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        return await self.approvals.list(tenant_id, status)

    async def list_workers(self) -> list[WorkerInfo]:
        return await self.registry.list()

    async def list_evidence(self, tenant_id: str, task_id: str) -> list[EvidenceRecord]:
        return await self.evidence.list_for_task(tenant_id, task_id)


def strong_evidence_count(record: EvidenceRecord) -> int:
    return sum(1 for e in record.evidence if e.strength is EvidenceStrength.STRONG and e.passed)


__all__ = [n for n in dir() if n.startswith(("Fake", "InMemory"))] + ["DEFAULT_SCHEDULE_WEIGHTS", "strong_evidence_count", "utcnow"]
