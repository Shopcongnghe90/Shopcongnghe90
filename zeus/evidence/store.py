"""PgEvidenceStore (bất biến khi verified) + ArtifactStore (content-addressed sha256)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from zeus.brain.pg import PgBase
from zeus.contracts.models import ArtifactRef, EvidenceRecord, Outcome
from zeus.evidence.rules import artifact_refs, check_record

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceImmutable(RuntimeError):
    """Bản ghi đã verified — không sửa/ghi đè."""


class ArtifactMissing(RuntimeError):
    pass


class ArtifactStore(PgBase):
    """Nội dung ở ``root/<tenant>/<sha[:2]>/<sha>``; metadata bất biến ở bảng ``artifacts``."""

    def __init__(self, dsn: str, root: str | Path) -> None:
        super().__init__(dsn)
        self.root = Path(root)

    def _path(self, tenant_id: str, sha: str) -> Path:
        if not _SHA_RE.match(sha) or not re.match(r"^[a-z0-9][a-z0-9_-]{1,62}$", tenant_id):
            raise ValueError("sha256/tenant không hợp lệ")
        return self.root / tenant_id / sha[:2] / sha

    async def put(self, tenant_id: str, data: bytes, mime: str = "application/octet-stream", name: str | None = None) -> ArtifactRef:
        sha = hashlib.sha256(data).hexdigest()
        p = self._path(tenant_id, sha)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        async with self.conn() as c:
            await c.execute(
                "INSERT INTO artifacts (tenant_id, sha256, size_bytes, mime, name) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, sha256) DO NOTHING",
                (tenant_id, sha, len(data), mime, name),
            )
        return ArtifactRef(ref=f"artifact://{tenant_id}/{sha}", sha256=sha, size_bytes=len(data), mime=mime, name=name)

    async def exists(self, tenant_id: str, sha: str) -> bool:
        async with self.conn() as c:
            cur = await c.execute("SELECT 1 FROM artifacts WHERE tenant_id=%s AND sha256=%s", (tenant_id, sha))
            return await cur.fetchone() is not None

    async def get(self, tenant_id: str, sha: str) -> bytes:
        if not await self.exists(tenant_id, sha):
            raise ArtifactMissing(sha)
        data = self._path(tenant_id, sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ArtifactMissing(f"artifact {sha} bị hỏng (sha256 không khớp)")
        return data

    async def verify(self, tenant_id: str, sha: str) -> bool:
        try:
            await self.get(tenant_id, sha)
            return True
        except (ArtifactMissing, FileNotFoundError):
            return False


class PgEvidenceStore(PgBase):
    """Implements ``EvidenceStore``. ``get`` nhận thêm ``tenant_id`` (xem CONTRACT_CHANGE_REQUEST)."""

    async def put(self, record: EvidenceRecord) -> str:
        strength = check_record(record)  # luật độ mạnh (ngoài validator của contract)
        payload = record.model_dump(mode="json")
        refs = artifact_refs(record.model_dump_json())
        for tenant, _sha in refs:
            if tenant != record.tenant_id:
                raise ValueError(f"evidence tham chiếu artifact của tenant khác: {tenant}")
        async with self.conn(autocommit=False) as c:
            try:
                cur = await c.execute(
                    "SELECT final_outcome, payload FROM evidence_records WHERE record_id=%s AND tenant_id=%s FOR UPDATE",
                    (record.record_id, record.tenant_id),
                )
                old = await cur.fetchone()
                if old is not None:
                    if old["payload"] == payload:
                        return record.record_id  # idempotent (retry Temporal)
                    if old["final_outcome"] != Outcome.UNVERIFIED.value:
                        raise EvidenceImmutable(f"{record.record_id} đã verified, không được ghi đè")
                    await c.execute(
                        "UPDATE evidence_records SET final_outcome=%s, strength=%s, payload=%s, trace_id=%s, task_family=%s "
                        "WHERE record_id=%s AND tenant_id=%s",
                        (record.final_outcome.value, strength, Jsonb(payload), record.trace_id, record.task_family.value,
                         record.record_id, record.tenant_id),
                    )
                else:
                    await c.execute(
                        "INSERT INTO evidence_records (record_id, tenant_id, task_id, trace_id, task_family, final_outcome, "
                        "strength, payload, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (record.record_id, record.tenant_id, record.task_id, record.trace_id, record.task_family.value,
                         record.final_outcome.value, strength, Jsonb(payload), record.created_at),
                    )
                for tenant, sha in sorted(refs):
                    cur = await c.execute("SELECT 1 FROM artifacts WHERE tenant_id=%s AND sha256=%s", (tenant, sha))
                    if await cur.fetchone() is None:
                        raise ArtifactMissing(f"evidence tham chiếu artifact không tồn tại: {sha}")
                    await c.execute(
                        "INSERT INTO evidence_artifact_links (tenant_id, record_id, sha256) VALUES (%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (tenant, record.record_id, sha),
                    )
                await c.commit()
            except BaseException:
                await c.rollback()
                raise
        return record.record_id

    async def get(self, record_id: str, tenant_id: str | None = None) -> EvidenceRecord | None:
        sql, args = "SELECT payload FROM evidence_records WHERE record_id=%s", [record_id]
        if tenant_id is not None:
            sql += " AND tenant_id=%s"
            args.append(tenant_id)
        async with self.conn() as c:
            r = await (await c.execute(sql, args)).fetchone()
        return EvidenceRecord.model_validate(r["payload"]) if r else None

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[EvidenceRecord]:
        async with self.conn() as c:
            cur = await c.execute(
                "SELECT payload FROM evidence_records WHERE tenant_id=%s AND task_id=%s ORDER BY created_at, record_id",
                (tenant_id, task_id),
            )
            return [EvidenceRecord.model_validate(r["payload"]) for r in await cur.fetchall()]

    async def trace(self, tenant_id: str, task_id: str) -> list[dict[str, Any]]:
        """task -> evidence -> artifacts (metadata)."""
        async with self.conn() as c:
            cur = await c.execute(
                "SELECT e.record_id, e.final_outcome, e.strength, "
                "coalesce(jsonb_agg(jsonb_build_object('sha256', a.sha256, 'size_bytes', a.size_bytes, 'name', a.name)) "
                "FILTER (WHERE a.sha256 IS NOT NULL), '[]'::jsonb) AS artifacts "
                "FROM evidence_records e LEFT JOIN evidence_artifact_links l ON l.record_id=e.record_id AND l.tenant_id=e.tenant_id "
                "LEFT JOIN artifacts a ON a.tenant_id=l.tenant_id AND a.sha256=l.sha256 "
                "WHERE e.tenant_id=%s AND e.task_id=%s GROUP BY e.record_id, e.final_outcome, e.strength, e.created_at "
                "ORDER BY e.created_at, e.record_id",
                (tenant_id, task_id),
            )
            return list(await cur.fetchall())


__all__ = ["ArtifactStore", "PgEvidenceStore", "EvidenceImmutable", "ArtifactMissing"]
