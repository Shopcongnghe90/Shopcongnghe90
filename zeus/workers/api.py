"""Worker API router ``/worker/v1/*`` (server). Xác thực token theo worker (header X-Zeus-Worker-Token).

Gắn vào app: ``install(app, WorkerServices(...))`` hoặc ``create_app(routers=[router])`` + ``app.state.worker_services``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi import FastAPI

from zeus.contracts.api import (
    HEADER_WORKER_TOKEN,
    Ack,
    AssignmentResult,
    CancelAck,
    HeartbeatResponse,
    Paths,
    PollRequest,
    PollResponse,
    WorkerRegisterRequest,
    WorkerRegisterResponse,
)
from zeus.contracts.models import ArtifactRef, WorkerHeartbeat, WorkerStatus
from zeus.storage.db import aconnect
from zeus.workers.config import WorkersConfig
from zeus.workers.dispatch import ActivityCompleter, cancelled_result
from zeus.workers.queue import PgAssignmentQueue
from zeus.workers.registry import PgWorkerRegistry, TokenGrant

log = logging.getLogger("zeus.workers.api")
router = APIRouter()


@dataclass
class WorkerServices:
    registry: PgWorkerRegistry
    queue: PgAssignmentQueue
    config: WorkersConfig
    completer: ActivityCompleter | None
    artifact_dir: Path
    dsn: str


def install(app: FastAPI, services: WorkerServices) -> None:
    app.state.worker_services = services
    app.include_router(router)


def _svc(request: Request) -> WorkerServices:
    return request.app.state.worker_services


async def auth(request: Request, token: Annotated[str | None, Header(alias=HEADER_WORKER_TOKEN)] = None) -> TokenGrant:
    grant = await _svc(request).registry.verify_token(token)
    if grant is None:
        raise HTTPException(401, "token worker không hợp lệ")
    return grant


Grant = Annotated[TokenGrant, Depends(auth)]


@router.post(Paths.WORKER_REGISTER, response_model=WorkerRegisterResponse)
async def register(req: WorkerRegisterRequest, grant: Grant, request: Request) -> WorkerRegisterResponse:
    s = _svc(request)
    info = req.info
    if info.worker_id != grant.worker_id:
        raise HTTPException(403, "token không thuộc worker_id này")
    if not set(info.tenant_scope) <= set(grant.allowed_tenants):
        raise HTTPException(403, "tenant_scope vượt quyền token")
    await s.registry.register(info)
    return WorkerRegisterResponse(
        worker_id=info.worker_id, heartbeat_interval_s=s.config.heartbeat_interval_s, poll_wait_s=s.config.poll_wait_s
    )


@router.post(Paths.WORKER_HEARTBEAT, response_model=HeartbeatResponse)
async def heartbeat(hb: WorkerHeartbeat, grant: Grant, request: Request) -> HeartbeatResponse:
    s = _svc(request)
    if hb.worker_id != grant.worker_id:
        raise HTTPException(403, "token không thuộc worker_id này")
    try:
        await s.registry.heartbeat(hb)
    except KeyError:
        raise HTTPException(404, "worker chưa register") from None
    await s.queue.extend_leases(hb.worker_id, hb.running_assignment_ids)
    info = await s.registry.get(hb.worker_id)
    return HeartbeatResponse(
        drain=bool(info and info.status is WorkerStatus.DRAINING),
        cancel_assignment_ids=await s.queue.pending_cancellations(hb.worker_id),
    )


@router.post(Paths.WORKER_POLL, response_model=PollResponse)
async def poll(req: PollRequest, grant: Grant, request: Request) -> PollResponse:
    s = _svc(request)
    if req.worker_id != grant.worker_id:
        raise HTTPException(403, "token không thuộc worker_id này")
    info = await s.registry.get(req.worker_id)
    if info is None:
        raise HTTPException(404, "worker chưa register")
    if info.status is WorkerStatus.DRAINING:
        return PollResponse()
    deadline = time.monotonic() + req.wait_s
    while True:
        got = await s.queue.poll(req.worker_id, req.max_assignments)
        if got or time.monotonic() >= deadline:
            return PollResponse(assignments=got)
        await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


async def _finish_activity(s: WorkerServices, assignment_id: str, token: bytes | None, result) -> None:
    if token and s.completer:
        await s.completer.complete(token, result)
        await s.queue.mark_activity_completed(assignment_id)


@router.post(Paths.WORKER_RESULT, response_model=Ack)
async def post_result(assignment_id: str, res: AssignmentResult, grant: Grant, request: Request) -> Ack:
    s = _svc(request)
    if res.assignment_id != assignment_id:
        raise HTTPException(422, "assignment_id trong path và body khác nhau")
    if res.worker_id != grant.worker_id:
        raise HTTPException(403, "token không thuộc worker_id này")
    try:
        token = await s.queue.complete(res)
    except PermissionError:
        raise HTTPException(403, "assignment không thuộc worker này") from None
    if token is None:
        return Ack(detail="đã nhận trước đó")
    result = res.result.model_copy(update={"executed_by": res.worker_id})
    try:
        await _finish_activity(s, assignment_id, token, result)
    except Exception as exc:  # activity chưa hoàn thành được: worker gửi lại sẽ thử tiếp (token vẫn được trả)
        log.exception("không hoàn thành được activity cho %s", assignment_id)
        raise HTTPException(502, f"không hoàn thành activity: {exc}") from exc
    return Ack()


@router.post(Paths.WORKER_CANCEL_ACK, response_model=Ack)
async def cancel_ack(assignment_id: str, ack: CancelAck, grant: Grant, request: Request) -> Ack:
    s = _svc(request)
    if ack.assignment_id != assignment_id or ack.worker_id != grant.worker_id:
        raise HTTPException(403, "không khớp assignment/worker")
    try:
        token = await s.queue.ack_cancel(ack)
    except PermissionError:
        raise HTTPException(403, "assignment không thuộc worker này") from None
    await _finish_activity(s, assignment_id, token, cancelled_result(assignment_id, ack.worker_id, ack.detail or "cancelled"))
    return Ack()


@router.post(Paths.WORKER_ARTIFACTS, response_model=ArtifactRef)
async def upload_artifact(
    request: Request,
    grant: Grant,
    assignment_id: str,
    kind: Literal["log", "artifact", "evidence"] = "artifact",
    name: str | None = None,
    mime: str = "application/octet-stream",
) -> ArtifactRef:
    s = _svc(request)
    body = await request.body()
    if len(body) > s.config.artifact_max_bytes:
        raise HTTPException(413, "artifact quá lớn")
    st = await s.queue.status(assignment_id)
    async with await aconnect(s.dsn, autocommit=True) as conn:
        cur = await conn.execute("SELECT tenant_id, worker_id FROM assignments WHERE assignment_id=%s", (assignment_id,))
        row = await cur.fetchone()
        if st is None or row is None or row["worker_id"] != grant.worker_id:
            raise HTTPException(403, "assignment không thuộc worker này")
        sha = hashlib.sha256(body).hexdigest()
        tenant = row["tenant_id"]
        ref = f"artifact://{tenant}/{sha}"
        path = s.artifact_dir / tenant / sha
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(body)
        safe_name = Path(name).name if name else None
        await conn.execute(
            """INSERT INTO worker_artifacts (ref, tenant_id, sha256, size_bytes, mime, name, kind, worker_id, assignment_id, path)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ref) DO NOTHING""",
            (ref, tenant, sha, len(body), mime, safe_name, kind, grant.worker_id, assignment_id, str(path)),
        )
    return ArtifactRef(ref=ref, sha256=sha, size_bytes=len(body), mime=mime, name=safe_name)
