"""Benchmark llama.cpp local: tok/s, độ trễ embeddings, VRAM (nvidia-smi nếu có). Xuất evidence JSON.

Chạy trên server thật sau gate G1/G2:
    python -m zeus.localai.benchmark --url http://10.90.30.20:8080/v1 --out var/bench-gpu.json
GPU vắng / server chưa lên => ghi evidence ``status: unavailable`` và thoát mã 0 (hệ thống vẫn chạy bằng cloud/queue).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zeus.localai.client import LlamaServerClient, LocalAIUnavailable

PROMPTS = [
    "Viết hàm Python kiểm tra số nguyên tố, có docstring tiếng Việt.",
    "Tóm tắt trong 3 gạch đầu dòng: lợi ích của việc dùng hàng đợi công việc có lease.",
    "Giải thích khác biệt giữa idempotent và at-most-once trong 2 câu.",
]


def vram_used_mb() -> int | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True).stdout
        return max(int(float(x)) for x in out.split())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


async def run_benchmark(client: LlamaServerClient, *, rounds: int = 1, max_tokens: int = 128) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "kind": "localai_benchmark",
        "at": datetime.now(timezone.utc).isoformat(),
        "model": client.model,
        "base_url": client.base_url,
    }
    if not await client.health():
        return {**ev, "status": "unavailable", "reason": "llama-server không phản hồi /health (GPU vắng hoặc chưa khởi động)"}
    try:
        rates: list[float] = []
        for _ in range(rounds):
            for p in PROMPTS:
                r = await client.chat([{"role": "user", "content": p}], max_tokens=max_tokens)
                if r.completion_tokens:
                    rates.append(r.tokens_per_s)
        emb = await client.embeddings(["thử embedding", "kiểm tra độ trễ"])
    except LocalAIUnavailable as exc:
        return {**ev, "status": "unavailable", "reason": str(exc)}
    return {
        **ev,
        "status": "ok",
        "tokens_per_s": {"mean": round(statistics.fmean(rates), 2) if rates else 0.0, "min": round(min(rates), 2) if rates else 0.0, "samples": len(rates)},
        "embedding_dim": len(emb[0]) if emb else 0,
        "vram_used_mb": vram_used_mb(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark llama.cpp local (tok/s, VRAM) -> evidence JSON")
    ap.add_argument("--url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--embed-url")
    ap.add_argument("--model", default="qwen3.5-9b-q4_k_m")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    async def go() -> dict[str, Any]:
        c = LlamaServerClient(args.url, args.model, embed_url=args.embed_url)
        try:
            return await run_benchmark(c, rounds=args.rounds)
        finally:
            await c.aclose()

    result = asyncio.run(go())
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
