"""CLI: ``python -m zeus_worker --config /etc/zeus-worker/worker.toml`` (chạy dưới systemd)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from zeus_worker.agent import WorkerAgent
from zeus_worker.client import WorkerApiClient
from zeus_worker.config import WorkerConfig


async def amain(cfg: WorkerConfig) -> None:
    client = WorkerApiClient(cfg.server_url, cfg.token())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await WorkerAgent(cfg, client).run_forever(stop)
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeus_worker", description="ZeusVN thin worker (không có LLM, chỉ typed executors)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(amain(WorkerConfig.load(args.config)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
