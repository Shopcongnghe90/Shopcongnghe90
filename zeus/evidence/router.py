"""Router FastAPI tuỳ chọn cho Evidence (integrator mount). Tenant luôn là tham số bắt buộc."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from zeus.contracts.models import EvidenceRecord
from zeus.evidence.rules import EvidenceRuleError
from zeus.evidence.store import ArtifactMissing, EvidenceImmutable, PgEvidenceStore


def build_router(store: PgEvidenceStore) -> APIRouter:
    router = APIRouter(prefix="/evidence", tags=["evidence"])

    @router.post("", status_code=201)
    async def put_evidence(record: EvidenceRecord) -> dict[str, str]:
        try:
            return {"record_id": await store.put(record)}
        except EvidenceImmutable as exc:
            raise HTTPException(409, str(exc)) from exc
        except (EvidenceRuleError, ArtifactMissing, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/task/{task_id}")
    async def list_task(task_id: str, tenant_id: str) -> list[EvidenceRecord]:
        return await store.list_for_task(tenant_id, task_id)

    @router.get("/task/{task_id}/trace")
    async def trace(task_id: str, tenant_id: str) -> list[dict[str, Any]]:
        return await store.trace(tenant_id, task_id)

    @router.get("/{record_id}")
    async def get_evidence(record_id: str, tenant_id: str) -> EvidenceRecord:
        rec = await store.get(record_id, tenant_id)
        if rec is None:
            raise HTTPException(404, "không thấy evidence")
        return rec

    return router
