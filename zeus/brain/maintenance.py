"""Memory Compactor + Retention policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from pydantic import BaseModel

from zeus.brain.pg import PgBase
from zeus.brain.store import PgMemoryStore
from zeus.contracts.models import MemoryItem, MemoryKind, TrustLevel, utcnow

Summarizer = Callable[[list[MemoryItem]], Awaitable[str]]
"""Hàm tóm tắt do caller cấp (vd bọc ModelBroker local). Compactor không tự gọi LLM."""


class CompactionReport(BaseModel):
    compacted: int
    summary_id: str | None
    source_ids: list[str]


class MemoryCompactor:
    def __init__(self, store: PgMemoryStore, summarizer: Summarizer) -> None:
        self.store, self.summarizer = store, summarizer

    async def compact_episodic(
        self, tenant_id: str, older_than: datetime, min_items: int = 3, tag: str | None = None, limit: int = 200
    ) -> CompactionReport:
        """Gom episodic cũ (còn hiệu lực) -> 1 semantic tóm tắt; nguồn được đóng (valid_to), không xoá."""
        items = [
            i
            for i in await self.store.list(tenant_id, MemoryKind.EPISODIC, limit)
            if i.valid_to is None and i.created_at < older_than and (tag is None or tag in i.tags)
        ]
        if len(items) < min_items:
            return CompactionReport(compacted=0, summary_id=None, source_ids=[])
        items.sort(key=lambda i: i.created_at)
        text = await self.summarizer(items)
        all_verified = all(i.trust is TrustLevel.VERIFIED for i in items)
        tags = sorted({t for i in items for t in i.tags} | {"compacted", "src:historical"})
        summary = MemoryItem(
            tenant_id=tenant_id,
            kind=MemoryKind.SEMANTIC,
            title=f"Tóm tắt {len(items)} bản ghi episodic",
            content=text,
            source_ref="compacted:" + ",".join(i.memory_id for i in items),
            trust=TrustLevel.VERIFIED if all_verified else TrustLevel.UNVERIFIED,
            confidence=min(i.confidence for i in items),
            tags=tags,
        )
        await self.store.put(summary)
        ids = [i.memory_id for i in items]
        await self.store.expire(tenant_id, ids)
        return CompactionReport(compacted=len(ids), summary_id=summary.memory_id, source_ids=ids)


class RetentionManager(PgBase):
    async def set_policy(self, tenant_id: str, kind: str, max_age_days: int, legal_hold: bool = False) -> None:
        async with self.conn() as c:
            await c.execute(
                "INSERT INTO retention_policies (tenant_id, kind, max_age_days, legal_hold) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, kind) DO UPDATE SET max_age_days=EXCLUDED.max_age_days, "
                "legal_hold=EXCLUDED.legal_hold, updated_at=now()",
                (tenant_id, kind, max_age_days, legal_hold),
            )

    async def apply(self, tenant_id: str, now: datetime | None = None, dry_run: bool = False) -> dict[str, int]:
        """Xoá memory_items quá hạn theo policy của từng kind (bỏ qua legal_hold). Không đụng canonical/decision/evidence
        records (bất biến); chỉ memory_items. Trả số bản ghi bị (sẽ bị) xoá theo kind."""
        now = now or utcnow()
        out: dict[str, int] = {}
        async with self.conn() as c:
            cur = await c.execute(
                "SELECT kind, max_age_days FROM retention_policies WHERE tenant_id=%s AND NOT legal_hold", (tenant_id,)
            )
            for p in await cur.fetchall():
                cutoff = now - timedelta(days=p["max_age_days"])
                if dry_run:
                    r = await c.execute(
                        "SELECT count(*) AS n FROM memory_items WHERE tenant_id=%s AND kind=%s AND created_at < %s",
                        (tenant_id, p["kind"], cutoff),
                    )
                    out[p["kind"]] = (await r.fetchone())["n"]
                else:
                    r = await c.execute(
                        "DELETE FROM memory_items WHERE tenant_id=%s AND kind=%s AND created_at < %s",
                        (tenant_id, p["kind"], cutoff),
                    )
                    out[p["kind"]] = r.rowcount
        return out
