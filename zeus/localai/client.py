"""Client cho llama.cpp ``llama-server`` (API tương thích OpenAI): health, chat, embeddings."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class LocalAIUnavailable(RuntimeError):
    """Server local không sẵn sàng (GPU vắng, đang nạp model, timeout...)."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float

    @property
    def tokens_per_s(self) -> float:
        return self.completion_tokens / self.latency_s if self.latency_s > 0 else 0.0


class LlamaServerClient:
    def __init__(
        self,
        base_url: str,
        model: str = "qwen3.5-9b-q4_k_m",
        *,
        embed_url: str | None = None,
        embed_model: str = "embed",
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``base_url`` kết thúc bằng /v1 (vd http://10.90.30.20:8080/v1)."""
        self.base_url = base_url.rstrip("/")
        self.embed_url = (embed_url or base_url).rstrip("/")
        self.model, self.embed_model = model, embed_model
        self._c = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._c.aclose()

    @staticmethod
    def _root(url: str) -> str:
        return url[:-3] if url.endswith("/v1") else url

    async def health(self) -> bool:
        """True nếu llama-server báo ok. Không bao giờ raise."""
        try:
            r = await self._c.get(self._root(self.base_url) + "/health", timeout=5)
            return r.status_code == 200 and r.json().get("status", "ok") == "ok"
        except (httpx.HTTPError, ValueError):
            return False

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._c.post(url, json=payload)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalAIUnavailable(f"{url}: {type(exc).__name__}: {exc}") from exc

    async def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 256, temperature: float = 0.0) -> ChatResult:
        t0 = time.perf_counter()
        data = await self._post(
            f"{self.base_url}/chat/completions",
            {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": False},
        )
        dt = time.perf_counter() - t0
        usage = data.get("usage", {})
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalAIUnavailable(f"phản hồi chat sai định dạng: {data!r:.200}") from exc
        return ChatResult(text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), dt)

    async def embeddings(self, texts: list[str]) -> list[list[float]]:
        data = await self._post(f"{self.embed_url}/embeddings", {"model": self.embed_model, "input": texts})
        try:
            return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
        except (KeyError, TypeError) as exc:
            raise LocalAIUnavailable(f"phản hồi embeddings sai định dạng: {data!r:.200}") from exc
