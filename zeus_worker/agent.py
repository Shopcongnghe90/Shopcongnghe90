"""Vòng đời thin worker: register -> heartbeat + poll -> execute (typed) -> upload -> result. Không có LLM."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx

from zeus.contracts.api import AssignmentResult, CancelAck, HeartbeatResponse, PollRequest, TaskAssignment
from zeus.contracts.models import (
    ActionResult,
    ArtifactRef,
    EvidenceItem,
    EvidenceKind,
    WorkerCapability,
    WorkerHeartbeat,
    WorkerInfo,
    WorkerKind,
    WorkerStatus,
    utcnow,
)
from zeus_worker import __version__, sysinfo
from zeus_worker.client import WorkerApiClient
from zeus_worker.config import WorkerConfig
from zeus_worker.executors import ExecContext, ExecError, ExecOutput, ExecutorRegistry

log = logging.getLogger("zeus_worker")
_LOG_EVIDENCE = {EvidenceKind.TEST_RESULT, EvidenceKind.BUILD_LOG, EvidenceKind.LOG}


class WorkerAgent:
    def __init__(
        self,
        cfg: WorkerConfig,
        client: WorkerApiClient,
        executors: ExecutorRegistry | None = None,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg, self.client = cfg, client
        self.executors = executors or ExecutorRegistry()
        self.http_transport = http_transport
        self.cpu = sysinfo.CpuSampler()
        self.running: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        self.drain = False
        self.recent: list[bool] = []  # kết quả gần nhất (True = ok) để tính recent_error_rate

    # ------------------------------------------------------------------ info
    def build_info(self) -> WorkerInfo:
        caps = {c.name: c for c in sysinfo.software_inventory()}
        for name in self.cfg.extra_capabilities:
            caps.setdefault(name, WorkerCapability(name=name))
        for name in self.executors.names():
            ex = self.executors.get(name)
            available = getattr(ex, "available", None)
            if available is not None and not available(self.cfg):
                continue  # executor cần cấu hình mà worker chưa có (vd test.run thiếu test_repo) => không quảng bá
            caps.setdefault(f"action:{name}", WorkerCapability(name=f"action:{name}"))
        return WorkerInfo(
            worker_id=self.cfg.worker_id, kind=WorkerKind(self.cfg.kind), tenant_scope=self.cfg.tenant_scope,
            capabilities=sorted(caps.values(), key=lambda c: c.name),
            inventory=sysinfo.inventory(self.cfg.labels, self.cfg.work_dir if self.cfg.work_dir.exists() else "/"),
            network_zone=self.cfg.network_zone, data_localities=self.cfg.data_localities, version=__version__,  # type: ignore[arg-type]
        )

    def build_heartbeat(self) -> WorkerHeartbeat:
        _, avail = sysinfo.ram_mb()
        window = self.recent[-20:]
        return WorkerHeartbeat(
            worker_id=self.cfg.worker_id, at=utcnow(), status=WorkerStatus.DRAINING if self.drain else WorkerStatus.ONLINE,
            cpu_available_pct=round(self.cpu.available_pct(), 1), ram_available_mb=avail, queue_depth=len(self.running),
            running_assignment_ids=sorted(self.running), recent_error_rate=(window.count(False) / len(window)) if window else 0.0,
        )

    # ------------------------------------------------------------------ protocol steps
    async def register(self) -> None:
        resp = await self.client.register(self.build_info())
        self.cfg.heartbeat_interval_s = float(resp.heartbeat_interval_s)
        self.cfg.poll_wait_s = resp.poll_wait_s

    async def heartbeat_once(self) -> HeartbeatResponse:
        resp = await self.client.heartbeat(self.build_heartbeat())
        self.drain = resp.drain
        for aid in resp.cancel_assignment_ids:
            self.cancel(aid)
        return resp

    def cancel(self, assignment_id: str) -> None:
        task = self.running.get(assignment_id)
        if task and assignment_id not in self._cancel_requested:
            self._cancel_requested.add(assignment_id)
            task.cancel()

    async def poll_once(self, wait_s: int | None = None) -> list[TaskAssignment]:
        free = self.cfg.max_concurrent - len(self.running)
        if free <= 0 or self.drain:
            return []
        resp = await self.client.poll(
            PollRequest(worker_id=self.cfg.worker_id, max_assignments=min(free, 16), wait_s=self.cfg.poll_wait_s if wait_s is None else wait_s)
        )
        for a in resp.assignments:
            self.running[a.assignment_id] = asyncio.create_task(self._run(a), name=f"asg-{a.assignment_id}")
        return resp.assignments

    async def wait_idle(self) -> None:
        while self.running:
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)
            await asyncio.sleep(0)

    # ------------------------------------------------------------------ execution
    async def _execute(self, a: TaskAssignment) -> ExecOutput:
        action = a.action
        ex = self.executors.get(action.name)
        if ex is None:
            return ExecOutput(ok=False, error=f"executor '{action.name}' không tồn tại (worker không chạy lệnh tự do)")
        if action.dry_run:
            return ExecOutput(output={"dry_run": True}, log="dry_run: không thực thi\n")
        try:
            return await asyncio.wait_for(ex.run(ExecContext(action, self.cfg, self.http_transport)), timeout=a.timeout_s)
        except asyncio.TimeoutError:
            return ExecOutput(ok=False, error=f"timeout sau {a.timeout_s}s", log=f"timeout sau {a.timeout_s}s\n")
        except ExecError as exc:
            return ExecOutput(ok=False, error=str(exc), log=f"từ chối/lỗi args: {exc}\n")
        except (httpx.HTTPError, OSError) as exc:
            return ExecOutput(ok=False, error=f"{type(exc).__name__}: {exc}", log=f"{type(exc).__name__}: {exc}\n")

    async def _run(self, a: TaskAssignment) -> None:
        started = utcnow()
        try:
            out = await self._execute(a)
            result = await self._upload(a, out, started)
            self.recent.append(out.ok)
            await self.client.post_result(result)
        except asyncio.CancelledError:
            if a.assignment_id not in self._cancel_requested:
                raise  # tắt worker, không phải lệnh huỷ từ control
            try:
                await self.client.cancel_ack(
                    CancelAck(assignment_id=a.assignment_id, worker_id=self.cfg.worker_id, cancelled=True, detail="cancelled by control")
                )
            except Exception:
                log.exception("cancel-ack %s lỗi", a.assignment_id)
        except Exception:
            log.exception("assignment %s lỗi khi báo kết quả", a.assignment_id)
        finally:
            self.running.pop(a.assignment_id, None)
            self._cancel_requested.discard(a.assignment_id)

    async def _upload(self, a: TaskAssignment, out: ExecOutput, started) -> AssignmentResult:
        aid = a.assignment_id
        log_bytes = out.log.encode("utf-8")
        log_art: ArtifactRef | None = await self.client.upload(aid, log_bytes, kind="log", name="execution.log", mime="text/plain")
        arts = [await self.client.upload(aid, data, kind="artifact", name=n, mime=m) for n, data, m in out.artifacts]
        evidence: list[EvidenceItem] = []
        for e in out.evidence:
            if e.ref is None and e.kind in _LOG_EVIDENCE:
                e = e.model_copy(update={"ref": log_art.ref, "sha256": e.sha256 or hashlib.sha256(log_bytes).hexdigest()})
            evidence.append(e)
        finished = utcnow()
        result = ActionResult(
            action_id=a.action.action_id, ok=out.ok, output=out.output, error=out.error, started_at=started, finished_at=finished,
            evidence_ids=[e.evidence_id for e in evidence], executed_by=self.cfg.worker_id,
        )
        return AssignmentResult(
            assignment_id=aid, worker_id=self.cfg.worker_id, result=result, log_ref=log_art.ref, artifacts=arts, evidence=evidence,
            finished_at=finished, metrics={"duration_s": (finished - started).total_seconds()},
        )

    # ------------------------------------------------------------------ main loop
    async def run_forever(self, stop: asyncio.Event) -> None:
        await self._retry(self.register, stop)
        hb = asyncio.create_task(self._heartbeat_loop(stop))
        try:
            while not stop.is_set():
                try:
                    got = await self.poll_once()
                except httpx.HTTPError as exc:
                    log.warning("poll lỗi: %s", exc)
                    await self._sleep(stop, 2)
                    continue
                if not got:
                    await self._sleep(stop, 0.2)
        finally:
            hb.cancel()
            for t in list(self.running.values()):
                t.cancel()
            await asyncio.gather(*self.running.values(), hb, return_exceptions=True)

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.heartbeat_once()
            except httpx.HTTPError as exc:
                log.warning("heartbeat lỗi: %s", exc)
            await self._sleep(stop, self.cfg.heartbeat_interval_s)

    async def _retry(self, fn, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                return await fn()
            except httpx.HTTPError as exc:
                log.warning("%s lỗi: %s (thử lại sau %.0fs)", fn.__name__, exc, delay)
                await self._sleep(stop, delay)
                delay = min(delay * 2, 30)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
