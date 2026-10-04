"""Fixtures workstream C: DB đã migrate, Worker API thật (ASGI) + completer giả đếm lần hoàn thành."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from zeus.app.main import create_app
from zeus.config import Settings
from zeus.contracts.models import (
    ActionResult,
    Inventory,
    WorkerCapability,
    WorkerInfo,
    WorkerKind,
)
from zeus.storage.migrate import apply_migrations
from zeus.workers.api import WorkerServices, install, router
from zeus.workers.config import load_config
from zeus.workers.queue import PgAssignmentQueue
from zeus.workers.registry import PgWorkerRegistry
from zeus.workers.scheduler import DeterministicScheduler, PgScheduleStore
from zeus.workers.dispatch import Dispatcher

REPO = Path(__file__).resolve().parents[2]


@dataclass
class CountingCompleter:
    calls: list[tuple[bytes, ActionResult]] = field(default_factory=list)
    fail_next: bool = False

    async def complete(self, task_token: bytes, result: ActionResult) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("temporal down")
        self.calls.append((task_token, result))


@dataclass
class Stack:
    dsn: str
    cfg: object
    registry: PgWorkerRegistry
    queue: PgAssignmentQueue
    store: PgScheduleStore
    scheduler: DeterministicScheduler
    dispatcher: Dispatcher
    completer: CountingCompleter
    services: WorkerServices
    app: object

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)


@pytest.fixture()
def stack(pg_dsn: str, tmp_path: Path) -> Stack:
    apply_migrations(pg_dsn, REPO / "migrations")
    cfg = load_config()
    registry = PgWorkerRegistry(pg_dsn)
    queue = PgAssignmentQueue(pg_dsn, lease_ttl_s=cfg.lease_ttl_s, max_attempts=cfg.max_attempts)
    store = PgScheduleStore(pg_dsn)
    scheduler = DeterministicScheduler(cfg.scheduler, stats_provider=store.success_stats, stale_ttl_s=cfg.stale_ttl_s)
    completer = CountingCompleter()
    dispatcher = Dispatcher(registry, queue, scheduler, store, cfg, completer)
    services = WorkerServices(registry, queue, cfg, completer, tmp_path / "artifacts", pg_dsn)
    app = create_app(Settings(env="test"))
    install(app, services)
    return Stack(pg_dsn, cfg, registry, queue, store, scheduler, dispatcher, completer, services, app)


def make_info(worker_id: str, caps: tuple[str, ...] = ("python", "git"), **kw) -> WorkerInfo:
    base = dict(
        worker_id=worker_id,
        kind=WorkerKind.LINUX_VM,
        capabilities=[WorkerCapability(name=c) for c in caps],
        inventory=Inventory(hostname=worker_id, os="Linux", cpu_count=4, ram_mb=8192, disk_free_gb=50),
    )
    base.update(kw)
    return WorkerInfo(**base)


__all__ = ["router"]
