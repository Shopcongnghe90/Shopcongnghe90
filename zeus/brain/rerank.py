"""Reranker: LexicalReranker (offline) + HTTPReranker (interface /rerank, vd llama.cpp/Jina/Cohere style)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import httpx

from zeus.brain.text import tokenize
from zeus.contracts.models import RetrievalHit


class LexicalReranker:
    """Điểm = độ phủ token truy vấn trong tài liệu (có trọng số idf cục bộ) + thưởng khớp tiêu đề/cụm."""

    async def rerank(self, query: str, hits: Sequence[RetrievalHit], top_k: int) -> list[RetrievalHit]:
        q = set(tokenize(query))
        if not q or not hits:
            return list(hits)[:top_k]
        docs = [set(tokenize(f"{h.memory.title or ''} {h.memory.content}")) for h in hits]
        n = len(docs)
        idf = {t: math.log(1 + (n + 1) / (1 + sum(t in d for d in docs))) for t in q}
        total = sum(idf.values())
        out: list[RetrievalHit] = []
        for h, d in zip(hits, docs):
            cover = sum(idf[t] for t in q if t in d) / total
            title = set(tokenize(h.memory.title or ""))
            bonus = 0.15 * (len(q & title) / len(q))
            out.append(h.model_copy(update={"rerank_score": cover + bonus}))
        out.sort(key=lambda h: (h.rerank_score or 0.0, h.score), reverse=True)
        return out[:top_k]


class HTTPReranker:
    """POST {url} {"model","query","documents":[...]} -> {"results":[{"index":i,"relevance_score":s}]}.

    Lỗi mạng/định dạng => trả thứ tự gốc (không làm hỏng retrieval)."""

    def __init__(self, url: str, model: str = "reranker", timeout_s: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url, self.model, self._timeout, self._transport = url, model, timeout_s, transport

    async def rerank(self, query: str, hits: Sequence[RetrievalHit], top_k: int) -> list[RetrievalHit]:
        hits = list(hits)
        if not hits:
            return []
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as c:
                r = await c.post(
                    self.url,
                    json={"model": self.model, "query": query, "documents": [h.memory.content for h in hits]},
                )
                r.raise_for_status()
                scores = {int(x["index"]): float(x.get("relevance_score", x.get("score"))) for x in r.json()["results"]}
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return hits[:top_k]
        out = [h.model_copy(update={"rerank_score": scores[i]}) for i, h in enumerate(hits) if i in scores]
        out.sort(key=lambda h: h.rerank_score or 0.0, reverse=True)
        return out[:top_k]
