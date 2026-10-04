"""PgMemoryStore: memory_items + canonical_state (versioned) + decision_ledger (append-only). Tenant bắt buộc."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from zeus.brain.embeddings import EmbeddingUnavailable
from zeus.brain.pg import PgBase, vec_literal
from zeus.brain.text import strip_accents
from zeus.contracts.interfaces import EmbeddingProvider
from zeus.contracts.models import MemoryItem, MemoryKind, TrustLevel, utcnow

_COLS = (
    "memory_id, tenant_id, kind, title, content, source_ref, trust, confidence, tags, "
    "valid_from, valid_to, supersedes, embedding_model, created_at"
)


def row_to_item(r: dict[str, Any]) -> MemoryItem:
    return MemoryItem(
        memory_id=r["memory_id"],
        tenant_id=r["tenant_id"],
        kind=MemoryKind(r["kind"]),
        content=r["content"],
        title=r["title"],
        source_ref=r["source_ref"],
        trust=TrustLevel(r["trust"]),
        confidence=float(r["confidence"]),
        tags=list(r["tags"]),
        valid_from=r["valid_from"],
        valid_to=r["valid_to"],
        supersedes=r["supersedes"],
        embedding_model=r["embedding_model"],
        created_at=r["created_at"],
    )


class PgMemoryStore(PgBase):
    """Implements ``zeus.contracts.interfaces.MemoryStore`` trên Postgres."""

    def __init__(self, dsn: str, embedder: EmbeddingProvider | None = None) -> None:
        super().__init__(dsn)
        self.embedder = embedder
        self._has_vector: bool | None = None

    async def has_vector(self) -> bool:
        if self._has_vector is None:
            async with self.conn() as c:
                cur = await c.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_name='memory_items' AND column_name='embedding'"
                )
                self._has_vector = await cur.fetchone() is not None
        return self._has_vector

    async def _embed_one(self, item: MemoryItem) -> list[float] | None:
        if self.embedder is None or not await self.has_vector():
            return None
        try:
            return (await self.embedder.embed([f"{item.title or ''}\n{item.content}"]))[0]
        except EmbeddingUnavailable:
            return None  # hạ cấp: lưu không vector, vẫn tìm được bằng lexical

    async def put(self, item: MemoryItem) -> str:
        vec = await self._embed_one(item)
        search_text = strip_accents(f"{item.title or ''} {item.content}")
        cols = _COLS.replace("embedding_model", "embedding_model, search_text")
        vals = [
            item.memory_id, item.tenant_id, item.kind.value, item.title, item.content, item.source_ref,
            item.trust.value, item.confidence, item.tags, item.valid_from, item.valid_to, item.supersedes,
            self.embedder.model_name if vec is not None and self.embedder else item.embedding_model, search_text,
            item.created_at,
        ]
        ph = ", ".join(["%s"] * len(vals))
        if vec is not None:
            cols += ", embedding"
            ph += ", %s::vector"
            vals.append(vec_literal(vec))
        async with self.conn() as c:
            await c.execute(f"INSERT INTO memory_items ({cols}) VALUES ({ph})", vals)
        return item.memory_id

    async def get(self, tenant_id: str, memory_id: str) -> MemoryItem | None:
        async with self.conn() as c:
            cur = await c.execute(
                f"SELECT {_COLS} FROM memory_items WHERE tenant_id=%s AND memory_id=%s", (tenant_id, memory_id)
            )
            r = await cur.fetchone()
        return row_to_item(r) if r else None

    async def list(self, tenant_id: str, kind: MemoryKind | None = None, limit: int = 100) -> list[MemoryItem]:
        sql = f"SELECT {_COLS} FROM memory_items WHERE tenant_id=%s"
        args: list[Any] = [tenant_id]
        if kind is not None:
            sql += " AND kind=%s"
            args.append(kind.value)
        sql += " ORDER BY created_at DESC, memory_id LIMIT %s"
        args.append(limit)
        async with self.conn() as c:
            cur = await c.execute(sql, args)
            return [row_to_item(r) for r in await cur.fetchall()]

    async def expire(self, tenant_id: str, memory_ids: list[str], at: datetime | None = None) -> int:
        """Đặt valid_to (không xoá) — dùng bởi compactor/supersede."""
        async with self.conn() as c:
            cur = await c.execute(
                "UPDATE memory_items SET valid_to=%s WHERE tenant_id=%s AND memory_id = ANY(%s) AND valid_to IS NULL",
                (at or utcnow(), tenant_id, memory_ids),
            )
            return cur.rowcount

    async def reindex(self, tenant_id: str) -> int:
        """Backfill embedding cho item chưa có vector (sau khi embedding server hoạt động lại)."""
        if self.embedder is None or not await self.has_vector():
            return 0
        async with self.conn() as c:
            cur = await c.execute(
                "SELECT memory_id, coalesce(title,'') || E'\\n' || content AS t FROM memory_items "
                "WHERE tenant_id=%s AND embedding IS NULL",
                (tenant_id,),
            )
            rows = await cur.fetchall()
            if not rows:
                return 0
            vecs = await self.embedder.embed([r["t"] for r in rows])
            for r, v in zip(rows, vecs):
                await c.execute(
                    "UPDATE memory_items SET embedding=%s::vector, embedding_model=%s WHERE memory_id=%s AND tenant_id=%s",
                    (vec_literal(v), self.embedder.model_name, r["memory_id"], tenant_id),
                )
        return len(rows)

    # ------------------------------------------------------------ canonical state (versioned)

    async def set_canonical(self, tenant_id: str, key: str, value: Any, updated_by: str = "system") -> int:
        async with self.conn(autocommit=False) as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"cs:{tenant_id}:{key}",))
            cur = await c.execute(
                "SELECT coalesce(max(version),0)+1 AS v FROM canonical_state WHERE tenant_id=%s AND key=%s",
                (tenant_id, key),
            )
            v = (await cur.fetchone())["v"]
            await c.execute(
                "INSERT INTO canonical_state (tenant_id, key, version, value, updated_by) VALUES (%s,%s,%s,%s,%s)",
                (tenant_id, key, v, Jsonb(value), updated_by),
            )
            await c.commit()
        return v

    async def get_canonical(self, tenant_id: str, version_of: dict[str, int] | None = None) -> dict[str, Any]:
        """Giá trị hiện hành (version cao nhất) của mọi key."""
        async with self.conn() as c:
            cur = await c.execute(
                "SELECT DISTINCT ON (key) key, value, version FROM canonical_state WHERE tenant_id=%s "
                "ORDER BY key, version DESC",
                (tenant_id,),
            )
            rows = await cur.fetchall()
        if version_of is not None:
            version_of.update({r["key"]: r["version"] for r in rows})
        return {r["key"]: r["value"] for r in rows}

    async def canonical_history(self, tenant_id: str, key: str) -> list[dict[str, Any]]:
        async with self.conn() as c:
            cur = await c.execute(
                "SELECT version, value, updated_by, created_at FROM canonical_state WHERE tenant_id=%s AND key=%s "
                "ORDER BY version",
                (tenant_id, key),
            )
            return list(await cur.fetchall())

    # ------------------------------------------------------------ decision ledger (append-only)

    async def record_decision(
        self,
        tenant_id: str,
        title: str,
        decision: str,
        rationale: str = "",
        status: str = "ACCEPTED",
        supersedes: str | None = None,
        evidence_ref: str | None = None,
    ) -> str:
        did = f"dec_{uuid.uuid4().hex}"
        async with self.conn() as c:
            await c.execute(
                "INSERT INTO decision_ledger (decision_id, tenant_id, title, decision, rationale, status, supersedes, "
                "evidence_ref) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (did, tenant_id, title, decision, rationale, status, supersedes, evidence_ref),
            )
        return did

    async def list_decisions(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.conn() as c:
            cur = await c.execute(
                "SELECT * FROM decision_ledger WHERE tenant_id=%s ORDER BY created_at DESC, decision_id LIMIT %s",
                (tenant_id, limit),
            )
            return list(await cur.fetchall())
