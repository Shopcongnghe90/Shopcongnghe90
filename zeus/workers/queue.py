"""AssignmentQueue trên PostgreSQL: lease (SKIP LOCKED), gia hạn, hết hạn => trả hàng đợi, cancel, kết quả idempotent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from zeus.contracts.api import AssignmentResult, CancelAck, TaskAssignment
from zeus.contracts.models import utcnow
from zeus.storage.db import aconnect


@dataclass(frozen=True)
class Expired:
    """Kết quả xử lý lease hết hạn."""

    requeued: list[str]  # quay lại QUEUED (cùng worker, hoặc worker_id=NULL nếu worker đã OFFLINE)
    released: list[str]  # trong requeued: đã bỏ worker (cần scheduler chọn lại)
    failed: list[tuple[str, bytes | None]]  # vượt max_attempts => FAILED; kèm task_token để fail activity


class PgAssignmentQueue:
    """Implements ``zeus.contracts.interfaces.AssignmentQueue`` (+ phần mở rộng cho Worker API)."""

    def __init__(self, dsn: str, *, lease_ttl_s: int = 60, max_attempts: int = 3) -> None:
        self.dsn = dsn
        self.lease_ttl_s = lease_ttl_s
        self.max_attempts = max_attempts

    # ------------------------------------------------------------------ enqueue / poll
    async def enqueue(
        self, assignment: TaskAssignment, worker_id: str | None, task_token: bytes | None = None, context: dict | None = None
    ) -> None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            try:
                await conn.execute(
                    """INSERT INTO assignments (assignment_id, tenant_id, task_id, node_id, worker_id, payload, task_token,
                           attempt, lease_expires_at, schedule_decision_id, sched_context)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (assignment.assignment_id, assignment.trace.tenant_id, assignment.task_id, assignment.node_id,
                     worker_id, Jsonb(assignment.model_dump(mode="json")), task_token, assignment.attempt,
                     assignment.lease_expires_at, assignment.schedule_decision_id, Jsonb(context or {})),
                )
            except Exception as exc:  # unique violation
                if getattr(exc, "sqlstate", None) == "23505":
                    raise ValueError(f"duplicate assignment {assignment.assignment_id}") from exc
                raise

    async def poll(self, worker_id: str, max_assignments: int = 1) -> list[TaskAssignment]:
        lease_until = utcnow() + timedelta(seconds=self.lease_ttl_s)
        out: list[TaskAssignment] = []
        async with await aconnect(self.dsn, autocommit=False) as conn:
            cur = await conn.execute(
                """SELECT assignment_id, payload, attempt FROM assignments
                   WHERE worker_id = %s AND status = 'QUEUED' AND NOT cancel_requested
                   ORDER BY enqueued_at, assignment_id LIMIT %s FOR UPDATE SKIP LOCKED""",
                (worker_id, max_assignments),
            )
            rows = await cur.fetchall()
            for r in rows:
                a = TaskAssignment.model_validate(r["payload"]).model_copy(
                    update={"lease_expires_at": lease_until, "attempt": r["attempt"]}
                )
                await conn.execute(
                    "UPDATE assignments SET status='LEASED', leased_at=now(), lease_expires_at=%s WHERE assignment_id=%s",
                    (lease_until, a.assignment_id),
                )
                out.append(a)
            await conn.commit()
        return out

    async def extend_leases(self, worker_id: str, assignment_ids: list[str]) -> int:
        if not assignment_ids:
            return 0
        until = utcnow() + timedelta(seconds=self.lease_ttl_s)
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "UPDATE assignments SET lease_expires_at=%s WHERE worker_id=%s AND status='LEASED' AND assignment_id = ANY(%s)",
                (until, worker_id, assignment_ids),
            )
            return cur.rowcount

    # ------------------------------------------------------------------ complete
    async def complete(self, result: AssignmentResult) -> bytes | None:
        """Trả task_token nếu CẦN hoàn thành activity (lần đầu, hoặc lần trước hoàn thành activity thất bại).
        Lặp lại sau khi activity đã hoàn thành => None. Worker không sở hữu => PermissionError."""
        async with await aconnect(self.dsn, autocommit=False) as conn:
            cur = await conn.execute("SELECT * FROM assignments WHERE assignment_id=%s FOR UPDATE", (result.assignment_id,))
            row = await cur.fetchone()
            if row is None or row["worker_id"] != result.worker_id:
                raise PermissionError("assignment does not belong to this worker")
            if row["status"] == "COMPLETED":
                await conn.rollback()
                return None if row["activity_completed"] else bytes(row["task_token"] or b"") or None
            if row["status"] in ("CANCELLED", "FAILED"):
                await conn.rollback()
                return None
            await conn.execute(
                "INSERT INTO assignment_results (assignment_id, worker_id, result) VALUES (%s,%s,%s)",
                (result.assignment_id, result.worker_id, Jsonb(result.model_dump(mode="json"))),
            )
            await conn.execute(
                "UPDATE assignments SET status='COMPLETED', finished_at=now() WHERE assignment_id=%s", (result.assignment_id,)
            )
            await conn.commit()
        return bytes(row["task_token"]) if row["task_token"] else None

    async def mark_activity_completed(self, assignment_id: str) -> None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute("UPDATE assignments SET activity_completed = true WHERE assignment_id=%s", (assignment_id,))

    # ------------------------------------------------------------------ cancel
    async def cancel(self, assignment_id: str) -> None:
        await self.cancel_ex(assignment_id)

    async def cancel_ex(self, assignment_id: str) -> tuple[str, bytes | None]:
        """Trả (trạng thái, task_token cần hoàn thành ngay). Việc chưa được lease bị huỷ ngay (token trả về);
        việc đang chạy: đặt cancel_requested, worker nhận qua heartbeat và gửi cancel-ack."""
        async with await aconnect(self.dsn, autocommit=False) as conn:
            cur = await conn.execute("SELECT status, task_token, activity_completed FROM assignments WHERE assignment_id=%s FOR UPDATE", (assignment_id,))
            row = await cur.fetchone()
            if row is None:
                raise KeyError(assignment_id)
            if row["status"] == "QUEUED":
                await conn.execute(
                    "UPDATE assignments SET status='CANCELLED', cancel_requested=true, finished_at=now() WHERE assignment_id=%s",
                    (assignment_id,),
                )
                await conn.commit()
                return "CANCELLED", bytes(row["task_token"]) if row["task_token"] else None
            if row["status"] == "LEASED":
                await conn.execute("UPDATE assignments SET cancel_requested=true WHERE assignment_id=%s", (assignment_id,))
                await conn.commit()
                return "CANCEL_REQUESTED", None
            await conn.rollback()
            return row["status"], None

    async def pending_cancellations(self, worker_id: str) -> list[str]:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT assignment_id FROM assignments WHERE worker_id=%s AND status='LEASED' AND cancel_requested ORDER BY assignment_id",
                (worker_id,),
            )
            return [r["assignment_id"] for r in await cur.fetchall()]

    async def ack_cancel(self, ack: CancelAck) -> bytes | None:
        """Worker xác nhận đã huỷ. Trả task_token để hoàn thành activity (ok=False, cancelled)."""
        async with await aconnect(self.dsn, autocommit=False) as conn:
            cur = await conn.execute("SELECT * FROM assignments WHERE assignment_id=%s FOR UPDATE", (ack.assignment_id,))
            row = await cur.fetchone()
            if row is None or row["worker_id"] != ack.worker_id:
                raise PermissionError("assignment does not belong to this worker")
            if row["status"] != "LEASED" or not ack.cancelled:
                await conn.rollback()
                return None
            await conn.execute(
                "UPDATE assignments SET status='CANCELLED', finished_at=now() WHERE assignment_id=%s", (ack.assignment_id,)
            )
            await conn.commit()
        return bytes(row["task_token"]) if row["task_token"] else None

    # ------------------------------------------------------------------ lease hết hạn / worker OFFLINE
    async def requeue_expired(self, now: datetime | None = None) -> Expired:
        now = now or utcnow()
        requeued: list[str] = []
        released: list[str] = []
        failed: list[tuple[str, bytes | None]] = []
        async with await aconnect(self.dsn, autocommit=False) as conn:
            cur = await conn.execute(
                """SELECT a.assignment_id, a.attempt, a.task_token, a.cancel_requested, w.status AS wstatus
                   FROM assignments a LEFT JOIN workers w ON w.worker_id = a.worker_id
                   WHERE a.status='LEASED' AND a.lease_expires_at < %s FOR UPDATE OF a SKIP LOCKED""",
                (now,),
            )
            for r in await cur.fetchall():
                aid = r["assignment_id"]
                if r["attempt"] >= self.max_attempts:
                    await conn.execute("UPDATE assignments SET status='FAILED', finished_at=now() WHERE assignment_id=%s", (aid,))
                    failed.append((aid, bytes(r["task_token"]) if r["task_token"] else None))
                    continue
                release = r["wstatus"] in (None, "OFFLINE")
                await conn.execute(
                    """UPDATE assignments SET status='QUEUED', attempt=attempt+1, leased_at=NULL, lease_expires_at=NULL,
                           worker_id = CASE WHEN %s THEN NULL ELSE worker_id END,
                           payload = jsonb_set(payload, '{attempt}', to_jsonb(attempt+1))
                       WHERE assignment_id=%s""",
                    (release, aid),
                )
                requeued.append(aid)
                if release:
                    released.append(aid)
            await conn.commit()
        return Expired(requeued, released, failed)

    async def unassigned(self) -> list[tuple[TaskAssignment, dict]]:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT payload, sched_context FROM assignments WHERE status='QUEUED' AND worker_id IS NULL AND NOT cancel_requested ORDER BY enqueued_at"
            )
            return [(TaskAssignment.model_validate(r["payload"]), r["sched_context"]) for r in await cur.fetchall()]

    async def assign_worker(self, assignment_id: str, worker_id: str, decision_id: str | None = None) -> bool:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                """UPDATE assignments SET worker_id=%s, schedule_decision_id=COALESCE(%s, schedule_decision_id)
                   WHERE assignment_id=%s AND status='QUEUED' AND worker_id IS NULL""",
                (worker_id, decision_id, assignment_id),
            )
            return cur.rowcount == 1

    async def result_of(self, assignment_id: str) -> AssignmentResult | None:
        """Kết quả worker đã gửi (đã lưu) của assignment; None nếu chưa có."""
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute("SELECT result FROM assignment_results WHERE assignment_id=%s", (assignment_id,))
            row = await cur.fetchone()
        return AssignmentResult.model_validate(row["result"]) if row else None

    async def uncompleted(self, grace_s: float = 15.0, limit: int = 100) -> list[tuple[str, str, bytes]]:
        """Assignment đã kết thúc (COMPLETED/FAILED/CANCELLED) nhưng activity chưa được hoàn thành (post_result/completer lỗi,
        sweep lỗi...). Trả (assignment_id, status, task_token). ``grace_s`` tránh đua với đường hoàn thành đang chạy."""
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                """SELECT assignment_id, status, task_token FROM assignments
                   WHERE status IN ('COMPLETED','FAILED','CANCELLED') AND NOT activity_completed AND task_token IS NOT NULL
                     AND finished_at < now() - make_interval(secs => %s)
                   ORDER BY finished_at LIMIT %s""",
                (grace_s, limit),
            )
            return [(r["assignment_id"], r["status"], bytes(r["task_token"])) for r in await cur.fetchall()]

    async def status(self, assignment_id: str) -> dict | None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT assignment_id, worker_id, status, attempt, cancel_requested, activity_completed FROM assignments WHERE assignment_id=%s",
                (assignment_id,),
            )
            return await cur.fetchone()

    async def queue_depth(self, worker_id: str) -> int:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM assignments WHERE worker_id=%s AND status IN ('QUEUED','LEASED')", (worker_id,)
            )
            return int((await cur.fetchone())["n"])
