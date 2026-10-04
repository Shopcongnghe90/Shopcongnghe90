from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from zeus.localai.benchmark import run_benchmark
from zeus.localai.client import LlamaServerClient, LocalAIUnavailable

ROOT = Path(__file__).resolve().parents[2]


def fake_llama(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    body = json.loads(req.content)
    if req.url.path == "/v1/chat/completions":
        assert body["model"] == "qwen3.5-9b-q4_k_m" and body["stream"] is False
        return httpx.Response(200, json={"choices": [{"message": {"content": "xin chào"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 40}})
    if req.url.path == "/v1/embeddings":
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i)] * 8} for i, _ in enumerate(body["input"])][::-1]})
    return httpx.Response(404)


async def test_client_health_chat_embeddings():
    c = LlamaServerClient("http://gpu:8080/v1", transport=httpx.MockTransport(fake_llama))
    assert await c.health()
    r = await c.chat([{"role": "user", "content": "hi"}])
    assert r.text == "xin chào" and r.completion_tokens == 40 and r.tokens_per_s > 0
    emb = await c.embeddings(["a", "b"])
    assert emb[0] == [0.0] * 8 and emb[1] == [1.0] * 8  # sắp theo index
    await c.aclose()


async def test_gpu_absent_degrades_gracefully():
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = LlamaServerClient("http://gpu:8080/v1", transport=httpx.MockTransport(down))
    assert await c.health() is False
    with pytest.raises(LocalAIUnavailable):
        await c.chat([{"role": "user", "content": "x"}])
    ev = await run_benchmark(c)
    assert ev["status"] == "unavailable" and "reason" in ev
    await c.aclose()


async def test_benchmark_evidence_when_available():
    c = LlamaServerClient("http://gpu:8080/v1", transport=httpx.MockTransport(fake_llama))
    ev = await run_benchmark(c, rounds=1, max_tokens=8)
    assert ev["status"] == "ok" and ev["tokens_per_s"]["samples"] == 3 and ev["tokens_per_s"]["mean"] > 0 and ev["embedding_dim"] == 8
    json.dumps(ev)
    await c.aclose()


def test_deploy_files_parse_and_match_architecture():
    prof = yaml.safe_load((ROOT / "zeus/localai/deploy/incus-gpu-profile.yaml").read_text())
    assert prof["config"]["nvidia.runtime"] == "true" and prof["devices"]["eth0"]["ipv4.address"] == "10.90.30.20"
    unit = (ROOT / "zeus/localai/deploy/llama-server.service").read_text()
    assert "Qwen3.5-9B-Q4_K_M.gguf" in unit and "--n-gpu-layers" in unit
    assert "--embeddings" in (ROOT / "zeus/localai/deploy/llama-embed.service").read_text()
