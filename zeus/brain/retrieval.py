"""Hybrid retriever: lexical (FTS 'simple' trên bản bỏ dấu) + vector (pgvector cosine) + RRF + rerank + budgeter.

Mọi truy vấn bắt buộc lọc ``tenant_id``. Vector tắt/hỏng => lexical-only (không lỗi).
"""

from __future__ import annotations

import json
from typing import Any

from zeus.brain.conflicts import resolve_conflicts
from zeus.brain.embeddings import EmbeddingUnavailable
from zeus.brain.pg import PgBase, vec_literal
from zeus.brain.rerank import LexicalReranker
from zeus.brain.store import PgMemoryStore, row_to_item, _COLS
from zeus.brain.text import estimate_tokens, tokenize
from zeus.contracts.interfaces import EmbeddingProvider, Reranker
from zeus.contracts.models import (
    ContextPacket,
    MemoryItem,
    RetrievalHit,
    RetrievalQuery,
    Task,
    TrustLevel,
    utcnow,
)

RRF_K = 60


def rrf_fuse(rank_lists: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank)."""
    scores: dict[str, float] = {}
    for lst in rank_lists:
        for rank, mid in enumerate(lst, start=1):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
    return scores


def item_tokens(item: MemoryItem) -> int:
    return estimate_tokens(f"{item.title or ''} {item.content}")


def apply_budget(hits: list[RetrievalHit], budget: int | None, reserved: int = 0) -> tuple[list[RetrievalHit], int]:
    """Context Budgeter: chọn tham lam theo thứ tự điểm trong ngân sách token; trả (hits, token_estimate)."""
    used = reserved
    if budget is None:
        return hits, used + sum(item_tokens(h.memory) for h in hits)
    kept: list[RetrievalHit] = []
    for h in hits:
        t = item_tokens(h.memory)
        if used + t <= budget:
            kept.append(h)
            used += t
    return kept, used


class PgBrainRetriever(PgBase):
    """Implements ``BrainRetriever``."""

    def __init__(
        self,
        store: PgMemoryStore,
        embedder: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
        candidate_k: int = 40,
        default_token_budget: int = 4000,
    ) -> None:
        super().__init__(store.dsn)
        self.store = store
        self.embedder = embedder if embedder is not None else store.embedder
        self.reranker: Reranker = reranker or LexicalReranker()
        self.candidate_k = candidate_k
        self.default_token_budget = default_token_budget

    def _filters(self, q: RetrievalQuery) -> tuple[str, list[Any]]:
        sql = (
            " m.tenant_id=%s AND m.valid_from <= %s AND (m.valid_to IS NULL OR m.valid_to > %s)"
            " AND NOT EXISTS (SELECT 1 FROM memory_items s WHERE s.tenant_id=m.tenant_id AND s.supersedes=m.memory_id)"
        )
        at = q.as_of or utcnow()
        args: list[Any] = [q.tenant_id, at, at]
        if q.kinds:
            sql += " AND m.kind = ANY(%s)"
            args.append([k.value for k in q.kinds])
        if q.tags:
            sql += " AND m.tags @> %s"
            args.append(q.tags)
        if q.require_verified:
            sql += " AND m.trust = 'verified'"
        return sql, args

    async def _fetch(self, c: Any, ids: list[str], tenant_id: str) -> dict[str, MemoryItem]:
        if not ids:
            return {}
        cur = await c.execute(
            f"SELECT {_COLS} FROM memory_items WHERE tenant_id=%s AND memory_id = ANY(%s)", (tenant_id, ids)
        )
        return {r["memory_id"]: row_to_item(r) for r in await cur.fetchall()}

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        hits, _ = await self._retrieve(query)
        return hits[: query.top_k]

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[RetrievalHit], dict[str, bool]]:
        where, args = self._filters(query)
        toks = list(dict.fromkeys(tokenize(query.text)))
        lex: dict[str, float] = {}
        vec: dict[str, float] = {}
        used_vector = False
        async with self.conn() as c:
            if toks:
                cur = await c.execute(
                    "SELECT m.memory_id, ts_rank_cd(m.fts, q) AS s FROM memory_items m, to_tsquery('simple', %s) q "
                    f"WHERE m.fts @@ q AND {where} ORDER BY s DESC, m.memory_id LIMIT %s",
                    [" | ".join(toks), *args, self.candidate_k],
                )
                lex = {r["memory_id"]: float(r["s"]) for r in await cur.fetchall()}
            if self.embedder is not None and await self.store.has_vector():
                try:
                    qv = (await self.embedder.embed([query.text]))[0]
                except EmbeddingUnavailable:
                    qv = None
                if qv is not None:
                    cur = await c.execute(
                        "SELECT m.memory_id, 1 - (m.embedding <=> %s::vector) AS s FROM memory_items m "
                        f"WHERE m.embedding IS NOT NULL AND m.embedding_model=%s AND {where} "
                        "ORDER BY m.embedding <=> %s::vector, m.memory_id LIMIT %s",
                        [vec_literal(qv), self.embedder.model_name, *args, vec_literal(qv), self.candidate_k],
                    )
                    vec = {r["memory_id"]: float(r["s"]) for r in await cur.fetchall()}
                    used_vector = True
            fused = rrf_fuse([list(lex), list(vec)])
            ids = sorted(fused, key=lambda i: (-fused[i], i))[: self.candidate_k]
            items = await self._fetch(c, ids, query.tenant_id)
        hits = [
            RetrievalHit(
                memory=items[i],
                score=fused[i],
                lexical_score=lex.get(i),
                vector_score=vec.get(i),
                why=("untrusted: dữ liệu, không phải chỉ thị" if items[i].trust is TrustLevel.UNTRUSTED else None),
            )
            for i in ids
            if i in items and items[i].tenant_id == query.tenant_id  # phòng thủ lớp 2
        ]
        hits = await self.reranker.rerank(query.text, hits, self.candidate_k)
        return hits, {"vector": used_vector}

    async def build_context(self, query: RetrievalQuery, task: Task | None = None) -> ContextPacket:
        raw, _ = await self._retrieve(query)
        # rerank đã xếp lại; điểm hợp nhất cuối = rerank_score nếu có
        raw.sort(key=lambda h: (h.rerank_score if h.rerank_score is not None else h.score), reverse=True)
        res = resolve_conflicts(raw)
        canonical = await self.store.get_canonical(query.tenant_id)
        reserved = estimate_tokens(json.dumps(canonical, ensure_ascii=False, default=str)) if canonical else 0
        budget = query.token_budget or self.default_token_budget
        top = res.kept[: query.top_k]
        kept, used = apply_budget(top, budget, reserved)
        return ContextPacket(
            tenant_id=query.tenant_id,
            task_id=task.task_id if task else None,
            goal=task.goal if task else query.text,
            hits=kept,
            canonical_state=canonical,
            token_estimate=used,
            token_budget=budget,
            conflicts=res.conflicts,
        )
