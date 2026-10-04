"""Kho idempotency của Tool Gateway cho action external.

``begin`` chiếm khoá TRƯỚC khi gọi provider; ``finish`` lưu kết quả SAU; ``release`` nhả khoá khi thất bại đã biết chắc là chưa
có tác dụng. Khoá còn PENDING (process chết giữa chừng / timeout) => kết quả KHÔNG rõ: gateway không chạy lại, trả lỗi để
người vận hành kiểm tra, tránh gửi tin/ghi bản ghi hai lần (review R7).
"""

from __future__ import annotations

from typing import Literal, Protocol

from psycopg.types.json import Jsonb

from zeus.contracts.models import ActionResult

State = Literal["new", "done", "pending"]


class IdempotencyStore(Protocol):
    async def begin(self, tenant_id: str, key: str) -> tuple[State, ActionResult | None]: ...

    async def finish(self, tenant_id: str, key: str, result: ActionResult) -> None: ...

    async def release(self, tenant_id: str, key: str) -> None: ...


class InMemoryIdempotency:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ActionResult | None] = {}

    async def begin(self, tenant_id: str, key: str) -> tuple[State, ActionResult | None]:
        k = (tenant_id, key)
        if k not in self._rows:
            self._rows[k] = None
            return "new", None
        prior = self._rows[k]
        return ("done", prior) if prior is not None else ("pending", None)

    async def finish(self, tenant_id: str, key: str, result: ActionResult) -> None:
        self._rows[(tenant_id, key)] = result

    async def release(self, tenant_id: str, key: str) -> None:
        self._rows.pop((tenant_id, key), None)


class PgIdempotency:
    """Bảng ``tool_idempotency`` (migration 102)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def begin(self, tenant_id: str, key: str) -> tuple[State, ActionResult | None]:
        from zeus.storage.db import aconnect

        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "INSERT INTO tool_idempotency (tenant_id, idem_key, status) VALUES (%s,%s,'PENDING') ON CONFLICT DO NOTHING", (tenant_id, key)
            )
            if cur.rowcount == 1:
                return "new", None
            cur = await conn.execute("SELECT status, result FROM tool_idempotency WHERE tenant_id=%s AND idem_key=%s", (tenant_id, key))
            row = await cur.fetchone()
        if row is None:  # bị nhả giữa hai câu lệnh: coi như đang xử lý, lần sau sẽ chiếm được
            return "pending", None
        if row["status"] == "DONE" and row["result"] is not None:
            return "done", ActionResult.model_validate(row["result"])
        return "pending", None

    async def finish(self, tenant_id: str, key: str, result: ActionResult) -> None:
        from zeus.storage.db import aconnect

        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO tool_idempotency (tenant_id, idem_key, status, result) VALUES (%s,%s,'DONE',%s) "
                "ON CONFLICT (tenant_id, idem_key) DO UPDATE SET status='DONE', result=EXCLUDED.result, updated_at=now()",
                (tenant_id, key, Jsonb(result.model_dump(mode="json"))),
            )

    async def release(self, tenant_id: str, key: str) -> None:
        from zeus.storage.db import aconnect

        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute("DELETE FROM tool_idempotency WHERE tenant_id=%s AND idem_key=%s AND status='PENDING'", (tenant_id, key))
