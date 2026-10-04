"""Client HTTP tới Control API của A (``Paths``): WorkbenchDataSource thật + hàm ingest/decide cho Workbench.

Hình dạng phản hồi của Control API do A hiện thực; ở đây giả định theo schema trong contracts/api.py
(TaskListResponse/ApprovalListResponse/EventIngestResponse; danh sách worker/evidence là mảng JSON). Cần đối chiếu khi tích hợp.
"""

from __future__ import annotations

from typing import Any

import httpx

from zeus.contracts.api import ApprovalListResponse, EventIngestRequest, EventIngestResponse, Paths, TaskListResponse
from zeus.contracts.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Event,
    EvidenceRecord,
    RouterStat,
    Task,
    TaskFamily,
    TaskStatus,
    WorkerInfo,
)


class ControlApiClient:
    def __init__(self, http: httpx.AsyncClient, tenant_header: str = "X-Zeus-Tenant") -> None:
        self._http, self._th = http, tenant_header

    async def _get(self, path: str, tenant_id: str | None = None, **params: Any) -> Any:
        r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None}, headers={self._th: tenant_id} if tenant_id else None)
        r.raise_for_status()
        return r.json()

    # --- WorkbenchDataSource
    async def list_tasks(self, tenant_id: str, status: TaskStatus | None = None, limit: int = 50) -> list[Task]:
        data = TaskListResponse.model_validate(await self._get(Paths.TASKS, tenant_id, status=status.value if status else None, limit=limit))
        return [Task(task_id=s.task_id, tenant_id=s.tenant_id, family=TaskFamily(s.family), goal=s.goal, risk=s.risk, status=s.status, created_at=s.created_at, workflow_id=s.workflow_id) for s in data.items]  # type: ignore[arg-type]

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None:
        r = await self._http.get(Paths.TASK.format(task_id=task_id), headers={self._th: tenant_id})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return Task.model_validate(r.json())

    async def list_approvals(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        return ApprovalListResponse.model_validate(await self._get(Paths.APPROVALS, tenant_id, status=status.value if status else None)).items

    async def list_workers(self) -> list[WorkerInfo]:
        return [WorkerInfo.model_validate(w) for w in await self._get(Paths.WORKERS)]

    async def list_evidence(self, tenant_id: str, task_id: str) -> list[EvidenceRecord]:
        return [EvidenceRecord.model_validate(e) for e in await self._get(Paths.TASK_EVIDENCE.format(task_id=task_id), tenant_id)]

    # --- hành động
    async def decide(self, decision: ApprovalDecision) -> ApprovalRequest:
        r = await self._http.post(Paths.APPROVAL_DECISION.format(approval_id=decision.approval_id), json=decision.model_dump(mode="json"))
        r.raise_for_status()
        return ApprovalRequest.model_validate(r.json())

    async def ingest(self, event: Event) -> EventIngestResponse:
        r = await self._http.post(Paths.EVENTS, json=EventIngestRequest(event=event).model_dump(mode="json"), headers={self._th: event.tenant_id})
        r.raise_for_status()
        return EventIngestResponse.model_validate(r.json())

    async def router_stats(self) -> list[RouterStat]:
        return [RouterStat.model_validate(s) for s in await self._get(Paths.ROUTER_STATS)]
