"""Client HTTP tới Worker API (chỉ httpx + zeus.contracts)."""

from __future__ import annotations

import httpx

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
from zeus.contracts.models import ArtifactRef, WorkerHeartbeat, WorkerInfo


class WorkerApiClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 90.0) -> None:
        self._c = httpx.AsyncClient(base_url=base_url, headers={HEADER_WORKER_TOKEN: token}, transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self._c.aclose()

    async def register(self, info: WorkerInfo) -> WorkerRegisterResponse:
        r = await self._c.post(Paths.WORKER_REGISTER, json=WorkerRegisterRequest(info=info).model_dump(mode="json"))
        r.raise_for_status()
        return WorkerRegisterResponse.model_validate(r.json())

    async def heartbeat(self, hb: WorkerHeartbeat) -> HeartbeatResponse:
        r = await self._c.post(Paths.WORKER_HEARTBEAT, json=hb.model_dump(mode="json"))
        r.raise_for_status()
        return HeartbeatResponse.model_validate(r.json())

    async def poll(self, req: PollRequest) -> PollResponse:
        r = await self._c.post(Paths.WORKER_POLL, json=req.model_dump(mode="json"))
        r.raise_for_status()
        return PollResponse.model_validate(r.json())

    async def post_result(self, res: AssignmentResult) -> Ack:
        r = await self._c.post(Paths.WORKER_RESULT.format(assignment_id=res.assignment_id), json=res.model_dump(mode="json"))
        r.raise_for_status()
        return Ack.model_validate(r.json())

    async def cancel_ack(self, ack: CancelAck) -> Ack:
        r = await self._c.post(Paths.WORKER_CANCEL_ACK.format(assignment_id=ack.assignment_id), json=ack.model_dump(mode="json"))
        r.raise_for_status()
        return Ack.model_validate(r.json())

    async def upload(self, assignment_id: str, data: bytes, *, kind: str, name: str, mime: str = "application/octet-stream") -> ArtifactRef:
        r = await self._c.post(
            Paths.WORKER_ARTIFACTS, params={"assignment_id": assignment_id, "kind": kind, "name": name, "mime": mime},
            content=data, headers={"Content-Type": "application/octet-stream"},
        )
        r.raise_for_status()
        return ArtifactRef.model_validate(r.json())
