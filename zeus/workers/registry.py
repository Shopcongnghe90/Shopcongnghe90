"""Worker Registry trên PostgreSQL + token theo worker (chỉ lưu hash SHA-256)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from zeus.contracts.models import (
    Inventory,
    WorkerCapability,
    WorkerHeartbeat,
    WorkerInfo,
    WorkerKind,
    WorkerStatus,
    utcnow,
)
from zeus.storage.db import aconnect


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenGrant:
    worker_id: str
    allowed_tenants: list[str]


class PgWorkerRegistry:
    """Implements ``zeus.contracts.interfaces.WorkerRegistry`` + quản lý token."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    # ------------------------------------------------------------------ token
    async def issue_token(self, worker_id: str, allowed_tenants: list[str] | None = None) -> str:
        """Phát hành token mới cho worker (trả plaintext đúng một lần; DB chỉ giữ hash)."""
        token = "zwt_" + secrets.token_urlsafe(32)
        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO worker_tokens (token_hash, worker_id, allowed_tenants) VALUES (%s, %s, %s)",
                (hash_token(token), worker_id, allowed_tenants or ["zeusvn"]),
            )
        return token

    async def revoke_tokens(self, worker_id: str) -> int:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "UPDATE worker_tokens SET revoked_at = now() WHERE worker_id = %s AND revoked_at IS NULL", (worker_id,)
            )
            return cur.rowcount

    async def verify_token(self, token: str | None) -> TokenGrant | None:
        if not token:
            return None
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT worker_id, allowed_tenants FROM worker_tokens WHERE token_hash = %s AND revoked_at IS NULL",
                (hash_token(token),),
            )
            row = await cur.fetchone()
        return TokenGrant(row["worker_id"], list(row["allowed_tenants"])) if row else None

    # ------------------------------------------------------------------ registry
    async def register(self, info: WorkerInfo) -> WorkerInfo:
        caps = [c.model_dump(mode="json") for c in info.capabilities]
        async with await aconnect(self.dsn, autocommit=False) as conn:
            await conn.execute(
                """INSERT INTO workers (worker_id, kind, tenant_scope, capabilities, status, network_zone,
                                        data_localities, version, registered_at, last_seen_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (worker_id) DO UPDATE SET kind=EXCLUDED.kind, tenant_scope=EXCLUDED.tenant_scope,
                       capabilities=EXCLUDED.capabilities, status=EXCLUDED.status, network_zone=EXCLUDED.network_zone,
                       data_localities=EXCLUDED.data_localities, version=EXCLUDED.version, last_seen_at=now()""",
                (info.worker_id, info.kind.value, info.tenant_scope, Jsonb(caps), info.status.value,
                 info.network_zone, info.data_localities, info.version, info.registered_at),
            )
            await conn.execute(
                """INSERT INTO worker_inventory (worker_id, inventory) VALUES (%s, %s)
                   ON CONFLICT (worker_id) DO UPDATE SET inventory = EXCLUDED.inventory, updated_at = now()""",
                (info.worker_id, Jsonb(info.inventory.model_dump(mode="json"))),
            )
            await conn.commit()
        return info

    async def heartbeat(self, hb: WorkerHeartbeat) -> None:
        async with await aconnect(self.dsn, autocommit=False) as conn:
            cur = await conn.execute(
                "UPDATE workers SET status = %s, last_seen_at = now() WHERE worker_id = %s",
                (hb.status.value, hb.worker_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"unknown worker {hb.worker_id}")
            await conn.execute(
                """INSERT INTO worker_heartbeats (worker_id, at, heartbeat) VALUES (%s,%s,%s)
                   ON CONFLICT (worker_id) DO UPDATE SET at = EXCLUDED.at, heartbeat = EXCLUDED.heartbeat""",
                (hb.worker_id, hb.at, Jsonb(hb.model_dump(mode="json"))),
            )
            await conn.commit()

    async def set_status(self, worker_id: str, status: WorkerStatus) -> None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            await conn.execute("UPDATE workers SET status = %s WHERE worker_id = %s", (status.value, worker_id))

    _SELECT = """SELECT w.*, i.inventory FROM workers w JOIN worker_inventory i USING (worker_id)"""

    @staticmethod
    def _row_to_info(r: dict) -> WorkerInfo:
        return WorkerInfo(
            worker_id=r["worker_id"],
            kind=WorkerKind(r["kind"]),
            tenant_scope=list(r["tenant_scope"]),
            capabilities=[WorkerCapability.model_validate(c) for c in r["capabilities"]],
            inventory=Inventory.model_validate(r["inventory"]),
            status=WorkerStatus(r["status"]),
            network_zone=r["network_zone"],
            data_localities=list(r["data_localities"]),
            version=r["version"],
            registered_at=r["registered_at"],
        )

    async def get(self, worker_id: str) -> WorkerInfo | None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(self._SELECT + " WHERE w.worker_id = %s", (worker_id,))
            row = await cur.fetchone()
        return self._row_to_info(row) if row else None

    async def list(self, status: WorkerStatus | None = None) -> list[WorkerInfo]:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            if status is None:
                cur = await conn.execute(self._SELECT + " ORDER BY w.worker_id")
            else:
                cur = await conn.execute(self._SELECT + " WHERE w.status = %s ORDER BY w.worker_id", (status.value,))
            rows = await cur.fetchall()
        return [self._row_to_info(r) for r in rows]

    async def last_heartbeat(self, worker_id: str) -> WorkerHeartbeat | None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute("SELECT heartbeat FROM worker_heartbeats WHERE worker_id = %s", (worker_id,))
            row = await cur.fetchone()
        return WorkerHeartbeat.model_validate(row["heartbeat"]) if row else None

    async def heartbeats(self) -> dict[str, WorkerHeartbeat]:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute("SELECT worker_id, heartbeat FROM worker_heartbeats")
            rows = await cur.fetchall()
        return {r["worker_id"]: WorkerHeartbeat.model_validate(r["heartbeat"]) for r in rows}

    async def mark_stale(self, now: datetime, ttl_s: int) -> list[str]:
        """Worker không heartbeat quá ttl_s => OFFLINE. Trả danh sách worker vừa bị đánh dấu."""
        cutoff = now - timedelta(seconds=ttl_s)
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                """UPDATE workers SET status = 'OFFLINE'
                   WHERE status <> 'OFFLINE' AND last_seen_at < %s RETURNING worker_id""",
                (cutoff,),
            )
            rows = await cur.fetchall()
        return sorted(r["worker_id"] for r in rows)


__all__ = ["PgWorkerRegistry", "TokenGrant", "hash_token", "utcnow"]
