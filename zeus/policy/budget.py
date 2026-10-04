"""Budget policy + ledger (trần chi phí mỗi task / mỗi ngày). Ledger in-memory và Postgres (bảng budget_ledger)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from zeus.contracts.interfaces import PolicyDenied
from pydantic import Field

from zeus.contracts.models import ZeusModel, utcnow


class BudgetExceeded(PolicyDenied):
    """Vượt trần chi phí; broker không gọi provider có phí nữa (local vẫn dùng được vì chi phí 0)."""


class LedgerEntry(ZeusModel):
    tenant_id: str
    task_id: str | None = None
    request_id: str | None = None
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    occurred_at: datetime = Field(default_factory=utcnow)


class BudgetLedger(Protocol):
    async def record(self, entry: LedgerEntry) -> None: ...

    async def spent_task(self, task_id: str) -> float: ...

    async def spent_day(self, tenant_id: str, now: datetime | None = None) -> float: ...


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


class InMemoryBudgetLedger:
    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []

    async def record(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    async def spent_task(self, task_id: str) -> float:
        return sum(e.usd for e in self.entries if e.task_id == task_id)

    async def spent_day(self, tenant_id: str, now: datetime | None = None) -> float:
        start = _day_start(now or utcnow())
        return sum(e.usd for e in self.entries if e.tenant_id == tenant_id and start <= e.occurred_at < start + timedelta(days=1))


class PgBudgetLedger:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def record(self, entry: LedgerEntry) -> None:
        from zeus.storage.db import aconnect

        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO budget_ledger (tenant_id, task_id, request_id, provider, model, input_tokens, output_tokens, usd, occurred_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (entry.tenant_id, entry.task_id, entry.request_id, entry.provider, entry.model, entry.input_tokens, entry.output_tokens, entry.usd, entry.occurred_at),
            )

    async def spent_task(self, task_id: str) -> float:
        from zeus.storage.db import aconnect

        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute("SELECT COALESCE(SUM(usd),0)::float8 AS s FROM budget_ledger WHERE task_id=%s", (task_id,))
            return float((await cur.fetchone())["s"])  # type: ignore[index]

    async def spent_day(self, tenant_id: str, now: datetime | None = None) -> float:
        from zeus.storage.db import aconnect

        start = _day_start(now or utcnow())
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(usd),0)::float8 AS s FROM budget_ledger WHERE tenant_id=%s AND occurred_at >= %s AND occurred_at < %s",
                (tenant_id, start, start + timedelta(days=1)),
            )
            return float((await cur.fetchone())["s"])  # type: ignore[index]


class BudgetPolicy:
    """Kiểm tra trần trước khi gọi provider có phí. ``None`` = không giới hạn."""

    def __init__(self, ledger: BudgetLedger, per_task_usd: float | None = None, per_day_usd: float | None = None) -> None:
        self.ledger = ledger
        self.per_task_usd = per_task_usd
        self.per_day_usd = per_day_usd

    async def check(self, tenant_id: str, task_id: str | None, est_usd: float, task_budget_usd: float | None = None) -> None:
        if est_usd <= 0:
            return
        cap_task = min([c for c in (self.per_task_usd, task_budget_usd) if c is not None], default=None)
        if task_id and cap_task is not None:
            spent = await self.ledger.spent_task(task_id)
            if spent + est_usd > cap_task:
                raise BudgetExceeded(f"vượt trần task: đã {spent:.4f} + ước tính {est_usd:.4f} > {cap_task:.4f} USD")
        if self.per_day_usd is not None:
            spent_d = await self.ledger.spent_day(tenant_id)
            if spent_d + est_usd > self.per_day_usd:
                raise BudgetExceeded(f"vượt trần ngày: đã {spent_d:.4f} + ước tính {est_usd:.4f} > {self.per_day_usd:.4f} USD")
