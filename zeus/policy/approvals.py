"""Approval store: in-memory (dev/test) và Postgres (bảng approvals). Quyết định có người duyệt, hết hạn => EXPIRED."""

from __future__ import annotations

from zeus.contracts.models import ApprovalDecision, ApprovalRequest, ApprovalStatus, utcnow


def _apply_decision(req: ApprovalRequest, decision: ApprovalDecision) -> ApprovalRequest:
    if req.status is not ApprovalStatus.PENDING:
        raise ValueError(f"approval {req.approval_id} already {req.status.value}")
    if req.expires_at and decision.decided_at > req.expires_at:
        return req.model_copy(update={"status": ApprovalStatus.EXPIRED})
    return req.model_copy(
        update={
            "status": ApprovalStatus(decision.status),
            "decided_by": decision.decided_by,
            "decided_at": decision.decided_at,
            "comment": decision.comment,
        }
    )


class InMemoryApprovals:
    def __init__(self) -> None:
        self.items: dict[str, ApprovalRequest] = {}

    async def request(self, req: ApprovalRequest) -> ApprovalRequest:
        self.items[req.approval_id] = req
        return req

    async def decide(self, decision: ApprovalDecision) -> ApprovalRequest:
        req = self.items.get(decision.approval_id)
        if req is None:
            raise KeyError(decision.approval_id)
        self.items[req.approval_id] = updated = _apply_decision(req, decision)
        return updated

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        return self.items.get(approval_id)

    async def list(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        return [a for a in self.items.values() if a.tenant_id == tenant_id and (status is None or a.status is status)]

    async def expire(self, approval_id: str) -> ApprovalRequest | None:
        req = self.items.get(approval_id)
        if req and req.status is ApprovalStatus.PENDING:
            self.items[approval_id] = req = req.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return req


class PgApprovalStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def _conn(self):  # noqa: ANN202
        from zeus.storage.db import aconnect

        return await aconnect(self.dsn, autocommit=True)

    @staticmethod
    def _row(r: dict) -> ApprovalRequest:
        return ApprovalRequest.model_validate(r["data"])

    async def request(self, req: ApprovalRequest) -> ApprovalRequest:
        async with await self._conn() as conn:
            await conn.execute(
                "INSERT INTO approvals (approval_id, tenant_id, task_id, action_id, risk, status, requested_at, expires_at, data)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (approval_id) DO NOTHING",
                (req.approval_id, req.tenant_id, req.task_id, req.action.action_id, req.risk.value, req.status.value, req.requested_at, req.expires_at, req.model_dump_json()),
            )
        return req

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        async with await self._conn() as conn:
            cur = await conn.execute("SELECT data, status, decided_by, decided_at, comment FROM approvals WHERE approval_id=%s", (approval_id,))
            r = await cur.fetchone()
        return self._merge(r) if r else None

    @classmethod
    def _merge(cls, r: dict) -> ApprovalRequest:
        req = cls._row(r)
        return req.model_copy(update={"status": ApprovalStatus(r["status"]), "decided_by": r["decided_by"], "decided_at": r["decided_at"], "comment": r["comment"]})

    async def decide(self, decision: ApprovalDecision) -> ApprovalRequest:
        req = await self.get(decision.approval_id)
        if req is None:
            raise KeyError(decision.approval_id)
        updated = _apply_decision(req, decision)
        async with await self._conn() as conn:
            cur = await conn.execute(
                "UPDATE approvals SET status=%s, decided_by=%s, decided_at=%s, comment=%s WHERE approval_id=%s AND status='PENDING'",
                (updated.status.value, updated.decided_by, updated.decided_at, updated.comment, req.approval_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"approval {req.approval_id} already decided")
        return updated

    async def expire(self, approval_id: str) -> ApprovalRequest | None:
        async with await self._conn() as conn:
            await conn.execute("UPDATE approvals SET status='EXPIRED', decided_at=%s WHERE approval_id=%s AND status='PENDING'", (utcnow(), approval_id))
        return await self.get(approval_id)

    async def list(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        sql = "SELECT data, status, decided_by, decided_at, comment FROM approvals WHERE tenant_id=%s"
        args: list = [tenant_id]
        if status is not None:
            sql += " AND status=%s"
            args.append(status.value)
        async with await self._conn() as conn:
            cur = await conn.execute(sql + " ORDER BY requested_at DESC", args)
            return [self._merge(r) for r in await cur.fetchall()]
