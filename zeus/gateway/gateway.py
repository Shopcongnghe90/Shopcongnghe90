"""Event Gateway: ingest -> chuẩn hoá (untrusted mặc định) -> idempotency -> intent -> risk -> Task."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from zeus.contracts.interfaces import IntentEngine, RiskEngine
from zeus.contracts.models import (
    Channel,
    Event,
    EventStage,
    Intent,
    RiskAssessment,
    Task,
    TaskStatus,
    TraceContext,
    utcnow,
)
from zeus.gateway.store import ControlStore
from zeus.obs import new_trace_id

# Chỉ các nguồn nội bộ đã xác thực mới được giữ untrusted=False.
_TRUSTED_CHANNELS = frozenset({Channel.WORKBENCH, Channel.INTERNAL, Channel.SCHEDULER})
_MAX_TEXT = 20_000


@dataclass
class IngestResult:
    event: Event
    duplicate: bool
    task_id: str | None = None
    task: Task | None = None
    intent: Intent | None = None
    risk: RiskAssessment | None = None
    resume_task: Task | None = None  # duplicate nhưng task còn PENDING (workflow có thể chưa start): caller start lại (idempotent)


def normalize_event(event: Event, idempotency_key: str | None = None) -> Event:
    """Event chuẩn: NORMALIZED, trace có sẵn, untrusted trừ khi nguồn nội bộ đã xác thực, text bị chặn độ dài."""
    trusted = event.channel in _TRUSTED_CHANNELS and event.signature_verified and not event.untrusted
    upd: dict[str, object] = {"stage": EventStage.NORMALIZED, "untrusted": not trusted}
    if idempotency_key and not event.external_id:
        upd["external_id"] = f"idem:{idempotency_key}"
    if event.text and len(event.text) > _MAX_TEXT:
        upd["text"] = event.text[:_MAX_TEXT]
    if event.trace is None:
        upd["trace"] = TraceContext(trace_id=new_trace_id(), tenant_id=event.tenant_id)
    return event.model_copy(update=upd)


class EventGateway:
    def __init__(self, store: ControlStore, intent: IntentEngine, risk: RiskEngine, resume_after_s: float = 30.0) -> None:
        self.store = store
        self.intent = intent
        self.risk = risk
        # ingest trùng mà task còn PENDING lâu hơn ngưỡng này => caller start lại workflow (lần start trước có thể đã thất bại)
        self.resume_after_s = resume_after_s

    async def ingest(self, event: Event, idempotency_key: str | None = None) -> IngestResult:
        ev = normalize_event(event, idempotency_key)
        saved, is_new = await self.store.put_event(ev)
        if not is_new:
            tid = await self.store.event_task_id(saved.tenant_id, saved.event_id)
            if tid is not None:
                t = await self.store.get_task(saved.tenant_id, tid)
                resume = t if t is not None and t.status is TaskStatus.PENDING and utcnow() - t.created_at > timedelta(seconds=self.resume_after_s) else None
                return IngestResult(event=saved, duplicate=True, task_id=tid, resume_task=resume)
            # Event đã lưu nhưng bước sau (intent/risk/put_task) lỗi ở lần trước: xử lý tiếp thay vì nuốt event (R3)
        intent = await self.intent.classify(saved)
        risk = await self.risk.assess(saved, intent)
        task_id_trace = saved.trace.model_copy() if saved.trace else None
        task = Task(
            task_id="tsk_" + hashlib.sha256(f"{saved.tenant_id}/{saved.event_id}".encode()).hexdigest()[:32],  # xác định theo event => resume idempotent
            tenant_id=saved.tenant_id,
            family=intent.family,
            goal=(saved.text or intent.summary)[:500],
            risk=risk.level,
            intent_id=intent.intent_id,
            event_id=saved.event_id,
            trace=task_id_trace,
            untrusted=saved.untrusted,
        )
        task = task.model_copy(update={"workflow_id": task.task_id, "trace": task.trace.model_copy(update={"task_id": task.task_id, "workflow_id": task.task_id}) if task.trace else None})
        await self.store.put_task(task)
        return IngestResult(event=saved, duplicate=False, task_id=task.task_id, task=task, intent=intent, risk=risk)
