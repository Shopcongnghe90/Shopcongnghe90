"""Fixture/harness cho test control plane (chỉ dùng fakes của B/C/D, không mạng, không key)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from zeus.broker.broker import DefaultModelBroker
from zeus.broker.config import ModelsConfig
from zeus.broker.providers import FakeProvider
from zeus.contracts.api import AssignmentResult
from zeus.contracts.models import (
    ActionResult,
    ActionSpec,
    EvidenceItem,
    EvidenceKind,
    Inventory,
    ProviderKind,
    RiskLevel,
    Task,
    TaskFamily,
    TypedAction,
    WorkerCapability,
    WorkerHeartbeat,
    WorkerInfo,
    WorkerKind,
)
from zeus.gateway.gateway import EventGateway
from zeus.gateway.store import InMemoryControlStore
from zeus.intent.engine import DefaultIntentEngine
from zeus.orchestration.activities import ControlDeps
from zeus.orchestration.client import complete_assignment
from zeus.planning.critic import DefaultCritic
from zeus.planning.judge import DeterministicJudge
from zeus.planning.planner import DefaultPlanner
from zeus.policy.approvals import InMemoryApprovals
from zeus.policy.budget import BudgetPolicy, InMemoryBudgetLedger
from zeus.policy.engine import DefaultPolicyEngine, PolicyConfig
from zeus.policy.gateway import DefaultToolGateway, InMemoryAudit, RemoteToolProvider
from zeus.risk.engine import DefaultRiskEngine
from zeus.testing.fakes import (
    FakeBrainRetriever,
    FakeScheduler,
    FakeToolProvider,
    InMemoryAssignmentQueue,
    InMemoryEvidenceStore,
    InMemoryMemoryStore,
    InMemoryOutcomeRecorder,
    InMemoryWorkerRegistry,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def models_cfg() -> ModelsConfig:
    return ModelsConfig.load(ROOT / "config" / "models.yaml")


@pytest.fixture(scope="session")
def policy_cfg() -> PolicyConfig:
    return PolicyConfig.load(ROOT / "config" / "policy.yaml")


def worker_specs() -> list[ActionSpec]:
    return [
        ActionSpec(name="code.apply_patch", risk=RiskLevel.R1, required_capabilities=["git", "python"], rollback_action="code.revert_patch",
                   input_schema={"type": "object", "properties": {"goal": {"type": "string"}}}),
        ActionSpec(name="code.revert_patch", risk=RiskLevel.R1, required_capabilities=["git"]),
        ActionSpec(name="test.run", risk=RiskLevel.R0, required_capabilities=["python"], idempotent=True),
        ActionSpec(name="db.migrate", risk=RiskLevel.R2, required_capabilities=["python"]),
        ActionSpec(name="shell.run", risk=RiskLevel.R0, required_capabilities=["python"]),
    ]


def control_specs() -> list[ActionSpec]:
    return [
        ActionSpec(name="erp.read", risk=RiskLevel.R0, input_schema={"type": "object", "required": ["goal"], "properties": {"goal": {"type": "string"}}}),
        ActionSpec(name="channel.send_message", risk=RiskLevel.R1, external=True, idempotent=True,
                   input_schema={"type": "object", "required": ["text"], "additionalProperties": False, "properties": {"text": {"type": "string", "maxLength": 200}}}),
    ]


def make_worker(wid: str = "w1", caps: tuple[str, ...] = ("git", "python")) -> WorkerInfo:
    return WorkerInfo(
        worker_id=wid, kind=WorkerKind.LINUX_VM, capabilities=[WorkerCapability(name=c) for c in caps],
        inventory=Inventory(hostname=wid, os="linux", cpu_count=4, ram_mb=8192),
    )


@dataclass
class Rig:
    deps: ControlDeps
    store: InMemoryControlStore
    approvals: InMemoryApprovals
    evidence: InMemoryEvidenceStore
    outcomes: InMemoryOutcomeRecorder
    queue: InMemoryAssignmentQueue
    workers: InMemoryWorkerRegistry
    gateway: DefaultToolGateway
    audit: InMemoryAudit
    control_tool: FakeToolProvider
    ledger: InMemoryBudgetLedger
    event_gateway: EventGateway
    broker: Any = None
    fakes: dict[ProviderKind, FakeProvider] = field(default_factory=dict)


async def make_rig(models_cfg: ModelsConfig, policy_cfg: PolicyConfig, *, broker: Any = None, require_model: bool = False, register_worker: bool = True,
                   judge_broker: Any = None, store: Any = None, approvals: Any = None, audit: Any = None, spans: Any = None) -> Rig:
    store, approvals, ev, oc = store or InMemoryControlStore(), approvals or InMemoryApprovals(), InMemoryEvidenceStore(), InMemoryOutcomeRecorder()
    queue, workers, audit, ledger = InMemoryAssignmentQueue(), InMemoryWorkerRegistry(), audit or InMemoryAudit(), InMemoryBudgetLedger()

    async def erp_read(action: TypedAction) -> dict[str, Any]:
        return {"rows": 3, "evidence": [{"kind": "data_match", "summary": "so khớp 3 dòng", "passed": True}]}

    control_tool = FakeToolProvider(control_specs(), erp_read)
    # FakeToolProvider.execute chạy được cho cả channel.send_message (echo)
    gw = DefaultToolGateway([control_tool, RemoteToolProvider(worker_specs())], DefaultPolicyEngine(policy_cfg, lambda tid: sum(e.usd for e in ledger.entries if e.task_id == tid)), approvals, audit)
    if register_worker:
        w = await workers.register(make_worker())
        await workers.heartbeat(WorkerHeartbeat(worker_id=w.worker_id, ram_available_mb=4096))
    mem = InMemoryMemoryStore()
    deps = ControlDeps(
        intent=DefaultIntentEngine(broker),
        risk=DefaultRiskEngine(),
        planner=DefaultPlanner(broker, gw.list_actions, require_model=require_model),
        critic=DefaultCritic(broker, gw.list_actions),
        judge=DeterministicJudge(judge_broker),
        gateway=gw, approvals=approvals, store=store, evidence=ev, outcomes=oc,
        retriever=FakeBrainRetriever(mem), scheduler=FakeScheduler(), workers=workers, queue=queue, spans=spans,
    )
    return Rig(deps, store, approvals, ev, oc, queue, workers, gw, audit, control_tool, ledger, EventGateway(store, deps.intent, deps.risk), broker)


def make_task(goal: str = "Sửa lỗi API trả về sai số liệu", family: TaskFamily = TaskFamily.BACKEND, **kw: Any) -> Task:
    t = Task(family=family, goal=goal, **kw)
    return t.model_copy(update={"workflow_id": t.task_id})


class FakeWorkerLoop:
    """Mô phỏng thin worker (C): poll AssignmentQueue, 'chạy' action, rồi hoàn thành activity bất đồng bộ qua task_token."""

    def __init__(self, client: Any, rig: Rig, worker_id: str = "w1", *, fail_actions: set[str] = frozenset(), hold: asyncio.Event | None = None, delay: float = 0.0) -> None:  # type: ignore[assignment]
        self.client, self.rig, self.worker_id = client, rig, worker_id
        self.fail_actions, self.hold, self.delay = set(fail_actions), hold, delay
        self.seen: list[str] = []
        self.executed: list[str] = []
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            for a in await self.rig.queue.poll(self.worker_id, 4):
                self.seen.append(a.action.name)
                asyncio.ensure_future(self._handle(a))
            await asyncio.sleep(0.05)

    async def _handle(self, a: Any) -> None:
        if self.hold is not None:
            await self.hold.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        ok = a.action.name not in self.fail_actions
        self.executed.append(a.action.name)
        ev = [EvidenceItem(kind=EvidenceKind.TEST_RESULT, summary=f"{a.action.name} {'pass' if ok else 'fail'}", passed=ok)] if a.action.name in {"test.run", "db.migrate"} or not ok else []
        res = AssignmentResult(
            assignment_id=a.assignment_id, worker_id=self.worker_id,
            result=ActionResult(action_id=a.action.action_id, ok=ok, error=None if ok else "boom", rollback_ref=f"rb_{a.action.action_id}", executed_by=self.worker_id),
            evidence=ev, metrics={"schedule_decision_id": a.schedule_decision_id or ""},
        )
        token = await self.rig.queue.complete(res)
        if token:
            await complete_assignment(self.client, token, res)

    def start(self) -> "FakeWorkerLoop":
        self._task = asyncio.ensure_future(self._run())
        return self

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def llm_script(plan_nodes: list[dict[str, Any]] | None = None) -> Any:
    """Script cho FakeProvider: trả JSON hợp lệ cho intent / planner / critic / judge theo prompt_version."""

    def script(req: Any) -> str:
        pv = req.prompt_version
        if pv == "intent-1":
            return json.dumps({"family": "backend", "summary": "yêu cầu backend", "confidence": 0.8})
        if pv == "planner-1":
            return json.dumps({"rationale": "plan từ model", "nodes": plan_nodes or [
                {"node_id": "impl", "title": "Sửa", "depends_on": [], "action": "code.apply_patch", "risk": "R1", "acceptance": ["diff sạch"]},
                {"node_id": "verify", "title": "Test", "depends_on": ["impl"], "action": "test.run", "risk": "R0", "acceptance": ["test pass"]},
            ]})
        if pv == "critic-1":
            return json.dumps({"approve": True, "issues": []})
        if pv == "judge-1":
            return json.dumps({"contradiction": False, "reason": ""})
        return "{}"

    return script


def mock_openai_transport(script: Any, *, status: int = 200, calls: list[Any] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if status != 200:
            return httpx.Response(status, json={"error": "x"})
        from zeus.contracts.models import ChatMessage, ModelRequest

        sys_msgs = [m["content"] for m in body["messages"] if m["role"] == "system"]
        msgs = [ChatMessage(role=m["role"], content=m["content"]) for m in body["messages"] if m["role"] != "system"]
        text = script(ModelRequest(system="\n".join(sys_msgs) or None, messages=msgs, prompt_version=_guess_version(sys_msgs)))
        return httpx.Response(200, json={"id": "c1", "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

    return httpx.MockTransport(handler)


def _guess_version(sys_msgs: list[str]) -> str | None:
    s = "\n".join(sys_msgs)
    for key, pv in (("bộ phân loại ý định", "intent-1"), ("Planner của hệ thống", "planner-1"), ("Critic độc lập", "critic-1"), ("giám khảo độc lập", "judge-1")):
        if key in s:
            return pv
    return None
