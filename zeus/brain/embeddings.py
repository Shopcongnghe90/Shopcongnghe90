"""EmbeddingProvider: HashingEmbedding (offline, deterministic) + OpenAICompatibleEmbedding (/v1/embeddings)."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

import httpx

from zeus.brain.text import tokenize


class EmbeddingUnavailable(RuntimeError):
    """Embedding không dùng được => caller hạ cấp xuống lexical-only."""


class HashingEmbedding:
    """Feature hashing (unigram + bigram, dấu ngẫu nhiên) rồi chuẩn hoá L2. Không cần GPU/mạng."""

    def __init__(self, dim: int = 256, model_name: str = "hashing-v1") -> None:
        self.dim = dim
        self.model_name = f"{model_name}-{dim}"

    def _vec(self, text: str) -> list[float]:
        toks = tokenize(text)
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        v = [0.0] * self.dim
        for f in feats:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            v[idx] += 1.0 if h[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v] if norm else v

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class OpenAICompatibleEmbedding:
    """Client ``POST {base_url}/embeddings`` (OpenAI-compatible; llama.cpp local). ``transport`` để test."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        api_key: str | None = None,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        batch_size: int = 32,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.dim = dim
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout_s
        self._transport = transport
        self._batch = batch_size

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                for i in range(0, len(texts), self._batch):
                    chunk = list(texts[i : i + self._batch])
                    resp = await client.post(
                        f"{self.base_url}/embeddings",
                        json={"model": self.model_name, "input": chunk},
                        headers=self._headers,
                    )
                    resp.raise_for_status()
                    data = sorted(resp.json()["data"], key=lambda d: d["index"])
                    if len(data) != len(chunk):
                        raise EmbeddingUnavailable("embedding count mismatch")
                    for d in data:
                        vec = [float(x) for x in d["embedding"]]
                        if len(vec) != self.dim:
                            raise EmbeddingUnavailable(f"embedding dim {len(vec)} != {self.dim}")
                        out.append(vec)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise EmbeddingUnavailable(str(exc)) from exc
        return out
