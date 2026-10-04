"""Temporal worker phía control (entrypoint tích hợp).

Chạy TaskWorkflow + ScheduledTaskWorkflow + activities control (A) trên task queue ``zeus-control`` với store PostgreSQL
thật (``zeus.app.system.build_system``), cùng vòng bảo trì dispatcher (C): worker mất tín hiệu => OFFLINE, lease hết hạn
=> trả hàng đợi/giao lại, quá số lần => FAILED (activity hoàn thành với kết quả lỗi, workflow không treo).

    python -m zeus.app.worker_main            # đọc cấu hình từ biến môi trường ZEUS_* (xem docs/runbooks/SERVER_BOOTSTRAP.md)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from typing import Any

from zeus.config import Settings
from zeus.contracts.api import TASK_QUEUE_CONTROL

log = logging.getLogger("zeus.app.worker_main")


async def sweep_loop(system: Any, stop: asyncio.Event, interval_s: float) -> None:
    while not stop.is_set():
        try:
            out = await system.dispatcher.sweep()
            if any(out.values()):
                log.info("dispatcher sweep: %s", out)
        except Exception:  # vòng bảo trì không được làm chết worker
            log.exception("dispatcher sweep lỗi")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), interval_s)


async def run(
    settings: Settings,
    stop: asyncio.Event,
    *,
    client: Any = None,
    task_queue: str = TASK_QUEUE_CONTROL,
    sweep_interval_s: float = 15.0,
    system: Any = None,
) -> None:
    from zeus.app.system import build_system
    from zeus.orchestration.client import build_worker, connect

    client = client or await connect(settings.temporal_address, settings.temporal_namespace)
    system = system or build_system(settings, temporal_client=client, task_queue=task_queue)
    worker = build_worker(client, system.deps, task_queue)
    sweeper = asyncio.create_task(sweep_loop(system, stop, sweep_interval_s))
    log.info("control worker chạy trên task queue %s (cloud=%s)", task_queue, settings.claude_cloud_available)
    try:
        async with worker:
            await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(sweeper, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeus.app.worker_main", description="ZeusVN Brain — Temporal worker phía control")
    ap.add_argument("--task-queue", default=TASK_QUEUE_CONTROL)
    ap.add_argument("--sweep-interval", type=float, default=15.0, help="giây giữa hai vòng bảo trì dispatcher")
    ap.add_argument("--migrate", action="store_true", help="áp dụng migrations trước khi chạy")
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    from zeus.obs.logging import configure_logging

    configure_logging(settings.log_level)
    if args.migrate:
        from zeus.storage.migrate import apply_migrations

        applied = apply_migrations(settings.db_dsn or "", settings.migrations_dir)
        log.info("migrations áp dụng: %s", [m.version for m in applied])

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await run(settings, stop, task_queue=args.task_queue, sweep_interval_s=args.sweep_interval)

    asyncio.run(_main())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
