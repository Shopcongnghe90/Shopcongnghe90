"""Workbench chạy với dữ liệu giả (fakes) — để xem giao diện / chụp màn hình / test, KHÔNG dùng cho production.

Chạy: ``ZEUS_WB_DEMO_PASSWORD=... python -m zeus.workbench.demo --port 8765`` (chỉ bind 127.0.0.1).
Không có mật khẩu mặc định: phải đặt ZEUS_WB_DEMO_PASSWORD.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import timedelta

from fastapi import FastAPI

from zeus.app.main import create_app
from zeus.config import Settings
from zeus.contracts.api import EventIngestResponse
from zeus.contracts.models import (
    ActionResult,
    ApprovalRequest,
    Event,
    EvidenceItem,
    EvidenceKind,
    EvidenceRecord,
    Inventory,
    MemoryItem,
    MemoryKind,
    Outcome,
    ProviderKind,
    RiskLevel,
    Task,
    TaskFamily,
    TaskGraph,
    TaskNode,
    TaskStatus,
    TypedAction,
    WorkerHeartbeat,
    WorkerInfo,
    WorkerKind,
    utcnow,
)
from zeus.testing.fakes import (
    FakeBrainRetriever,
    FakeWorkbenchDataSource,
    InMemoryApprovalStore,
    InMemoryEvidenceStore,
    InMemoryMemoryStore,
    InMemoryOutcomeRecorder,
    InMemoryWorkerRegistry,
)
from zeus.workbench.router import WorkbenchContext, router
from zeus.workbench.security import AuthConfig, hash_password

TRACE = "a" * 32


def build_demo_context(auth: AuthConfig | None = None) -> WorkbenchContext:
    approvals, registry, evidence, memory = InMemoryApprovalStore(), InMemoryWorkerRegistry(), InMemoryEvidenceStore(), InMemoryMemoryStore()
    ds = FakeWorkbenchDataSource(approvals=approvals, registry=registry, evidence=evidence)
    ingested: list[Event] = []

    async def ingest(ev: Event) -> EventIngestResponse:
        ingested.append(ev)
        return EventIngestResponse(event_id=ev.event_id, accepted=True, task_id="tsk_demo_new", workflow_id="wf-demo")

    graphs: dict[str, TaskGraph] = {}

    async def graph_provider(_tenant: str, task_id: str) -> TaskGraph | None:
        return graphs.get(task_id)

    async def audit(_tenant: str) -> list[dict[str, str]]:
        return [{"at": utcnow().strftime("%d/%m %H:%M"), "actor": "human:operator", "action": "approval.decide", "target": "apr_demo", "detail": "APPROVED"}]

    ctx = WorkbenchContext(
        data=ds, auth=auth or AuthConfig(), ingest=ingest, decide=approvals.decide, registry=registry,
        retriever=FakeBrainRetriever(memory), recorder=InMemoryOutcomeRecorder(), graph_provider=graph_provider, audit_provider=audit,
    )
    ctx.demo = {"ingested": ingested, "graphs": graphs, "memory": memory, "evidence": evidence, "approvals": approvals}  # type: ignore[attr-defined]
    return ctx


async def seed_demo(ctx: WorkbenchContext) -> dict[str, str]:
    """Nạp dữ liệu mẫu. Trả id các đối tượng chính để test tham chiếu."""
    ds: FakeWorkbenchDataSource = ctx.data  # type: ignore[assignment]
    d = ctx.demo  # type: ignore[attr-defined]
    now = utcnow()
    t_run = Task(family=TaskFamily.ERP_BUG, goal="Sửa lỗi in hoá đơn trên ERP staging", risk=RiskLevel.R1, status=TaskStatus.RUNNING, workflow_id="wf-1")
    t_ok = Task(family=TaskFamily.WEBSITE_EDIT, goal="Đổi banner trang chủ", status=TaskStatus.SUCCEEDED, created_at=now - timedelta(hours=2))
    t_fail = Task(family=TaskFamily.BACKEND, goal="Migrate bảng đơn hàng", risk=RiskLevel.R2, status=TaskStatus.FAILED, created_at=now - timedelta(hours=1))
    t_xss = Task(family=TaskFamily.GENERAL, goal="<script>alert('xss')</script> & \"quote\"", status=TaskStatus.PENDING)
    for t in (t_run, t_ok, t_fail, t_xss):
        ds.tasks[t.task_id] = t
    d["graphs"][t_run.task_id] = TaskGraph(
        task_id=t_run.task_id,
        nodes=[
            TaskNode(node_id="n1", title="Đọc code & tái hiện lỗi", status=TaskStatus.SUCCEEDED),
            TaskNode(node_id="n2", title="Viết bản sửa", depends_on=["n1"], status=TaskStatus.RUNNING, risk=RiskLevel.R1),
            TaskNode(node_id="n3", title="Chạy test", depends_on=["n2"]),
            TaskNode(node_id="n4", title="Soát xét chéo", depends_on=["n2"]),
            TaskNode(node_id="n5", title="Deploy staging", depends_on=["n3", "n4"], risk=RiskLevel.R2, status=TaskStatus.AWAITING_APPROVAL),
        ],
    )
    act = TypedAction(name="channel.zalo_bot.send_message", args={"to": "u1", "text": "<b>Xin chào</b>"}, task_id=t_run.task_id)
    apr = await d["approvals"].request(ApprovalRequest(task_id=t_run.task_id, action=act, risk=RiskLevel.R2, summary_vi="Gửi tin Zalo cho khách <img src=x onerror=alert(1)>"))
    for wid, kind, cpu, q in (("w-code-1", WorkerKind.LINUX_VM, 62.0, 1), ("w-code-2", WorkerKind.LINUX_VM, 15.0, 4), ("gpu-llm", WorkerKind.GPU_CONTAINER, 80.0, 0)):
        await ctx.registry.register(WorkerInfo(worker_id=wid, kind=kind, inventory=Inventory(hostname=wid, os="ubuntu-24.04", cpu_count=4, ram_mb=8192)))  # type: ignore[union-attr]
        await ctx.registry.heartbeat(WorkerHeartbeat(worker_id=wid, cpu_available_pct=cpu, ram_available_mb=4096, queue_depth=q))  # type: ignore[union-attr]
    await ctx.registry.register(WorkerInfo(worker_id="w-stale", kind=WorkerKind.WINDOWS_VM, inventory=Inventory(hostname="w-stale", os="win11", cpu_count=2, ram_mb=4096)))  # type: ignore[union-attr]
    await ctx.registry.heartbeat(WorkerHeartbeat(worker_id="w-stale", at=now - timedelta(minutes=10)))  # type: ignore[union-attr]

    def rec(task: Task, model: str, prov: ProviderKind, outcome: Outcome, cost: float, lat: int, **kw) -> EvidenceRecord:
        ev = [EvidenceItem(kind=EvidenceKind.TEST_RESULT, passed=True, summary="pytest 12 passed")] if outcome is Outcome.VERIFIED_SUCCESS else []
        return EvidenceRecord(
            trace_id=TRACE, tenant_id=task.tenant_id, task_id=task.task_id, task_family=task.family, goal=task.goal, model_provider=prov, model_name=model,
            cost_usd=cost, latency_ms=lat, final_outcome=outcome, evidence=ev, verified_by="judge:fake", **kw,
        )

    records = [
        rec(t_ok, "sonnet-5-5", ProviderKind.ANTHROPIC, Outcome.VERIFIED_SUCCESS, 0.12, 4200),
        rec(t_run, "sonnet-5-5", ProviderKind.ANTHROPIC, Outcome.VERIFIED_SUCCESS, 0.30, 9000, retries=1),
        rec(t_fail, "haiku-4-5", ProviderKind.ANTHROPIC, Outcome.VERIFIED_FAILURE, 0.02, 1500, rollback_performed=True, rollback_ref="rb_001"),
        rec(t_fail, "local-qwen", ProviderKind.LOCAL, Outcome.UNVERIFIED, 0.0, 800),
    ]
    for r in records:
        await d["evidence"].put(r)
        await ctx.recorder.record(r)  # type: ignore[union-attr]
    await d["memory"].put(MemoryItem(kind=MemoryKind.DECISION, title="ADR-006 Workbench", content="Workbench không có build step, tiếng Việt, <b>offline</b>.", tags=["workbench"]))
    return {"running": t_run.task_id, "failed": t_fail.task_id, "xss": t_xss.task_id, "approval": apr.approval_id, "ok": t_ok.task_id}


def build_demo_app(password: str, autologin: bool = False) -> FastAPI:
    """autologin=True CHỈ để chụp màn hình bằng trình duyệt headless (tiêm cookie phiên hợp lệ); không dùng ngoài demo."""
    auth = AuthConfig(password_hash=hash_password(password, iterations=1000))
    ctx = build_demo_context(auth)
    asyncio.run(seed_demo(ctx))
    app = create_app(Settings(env="dev"), routers=[router])
    app.state.workbench = ctx
    if autologin:
        sid = "D" * 43
        cookie = f"zwb_sid={sid}; zwb_auth={auth.make_auth_cookie(sid, auth.username)}"

        @app.middleware("http")
        async def _inject(request, call_next):  # type: ignore[no-untyped-def]
            request.scope["headers"] = [(k, v) for k, v in request.scope["headers"] if k != b"cookie"] + [(b"cookie", cookie.encode())]
            return await call_next(request)

    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--autologin", action="store_true", help="chỉ để chụp màn hình")
    a = ap.parse_args()
    pw = os.environ.get("ZEUS_WB_DEMO_PASSWORD")
    if not pw:
        raise SystemExit("đặt ZEUS_WB_DEMO_PASSWORD")
    uvicorn.run(build_demo_app(pw, a.autologin), host="127.0.0.1", port=a.port, log_level="warning")
