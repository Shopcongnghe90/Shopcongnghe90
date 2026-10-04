"""Backup/restore Brain DB theo tenant: JSONL có checksum + kiểm khôi phục (evidence data_match).

Định dạng: dòng 1 = header {tables:{name:{count,sha256}}}; các dòng sau {"t": table, "row": {...}}.
Sidecar ``<file>.sha256`` = sha256 của TOÀN BỘ tệp (header + thân). Đặt ``ZEUS_BACKUP_HMAC_KEY`` => sidecar là
``hmac-sha256:<hex>`` (có khoá, chống sửa tệp rồi tính lại checksum); đã đặt khoá thì từ chối sidecar không ký.
Export đọc trong MỘT transaction REPEATABLE READ READ ONLY (snapshot nhất quán, FK không gãy) và ghi tệp tạm rồi
``os.replace``. Restore từ chối bất kỳ dòng nào có tenant_id khác tenant đích (không cho bơm dữ liệu sang tenant khác).
KHÔNG thay thế pg_dump toàn hệ thống: tasks/events/approvals/audit_log/assignments không nằm trong backup này.
Tệp artifact (nhị phân) không nằm trong JSONL —
chúng content-addressed nên sao chép thư mục artifacts riêng (rsync/ZFS snapshot) và kiểm bằng ``ArtifactStore.verify``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zeus.brain.pg import PgBase
from zeus.contracts.models import EvidenceItem, EvidenceKind

# thứ tự tôn trọng FK
TABLES = [
    "tenants", "memory_items", "canonical_state", "decision_ledger", "retention_policies", "artifacts", "evidence_records",
    "evidence_artifact_links", "outcomes", "dataset_records", "router_stats", "champion_challenger",
    "eval_datasets", "eval_runs", "eval_results",
]


class BackupError(RuntimeError):
    pass


_HMAC_PREFIX = "hmac-sha256:"


def _checksum(lines: list[str]) -> str:
    """Checksum sidecar của toàn bộ tệp (header + thân); HMAC nếu có ZEUS_BACKUP_HMAC_KEY."""
    key = os.environ.get("ZEUS_BACKUP_HMAC_KEY")
    if not key:
        return _sha(lines)
    h = hmac.new(key.encode(), digestmod=hashlib.sha256)
    for ln in lines:
        h.update(ln.encode())
        h.update(b"\n")
    return _HMAC_PREFIX + h.hexdigest()


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _sha(lines: list[str]) -> str:
    h = hashlib.sha256()
    for ln in lines:
        h.update(ln.encode())
        h.update(b"\n")
    return h.hexdigest()


async def _columns(c: Any, table: str) -> list[str]:
    cur = await c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND is_generated='NEVER' "
        "ORDER BY ordinal_position",
        (table,),
    )
    return [r["column_name"] for r in await cur.fetchall()]


async def _dump(c: Any, tenant_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in TABLES:
        cols = await _columns(c, t)
        if not cols:
            continue
        sel = "jsonb_build_object(" + ",".join(f"'{col}', t.{col}" for col in cols) + ")::text"
        cur = await c.execute(f"SELECT {sel} AS j FROM {t} t WHERE t.tenant_id=%s ORDER BY 1", (tenant_id,))
        out[t] = [r["j"] for r in await cur.fetchall()]
    return out


class BrainBackup(PgBase):
    async def export(self, tenant_id: str, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        async with self.conn(autocommit=False) as c:
            # snapshot nhất quán cho mọi bảng: ghi đồng thời không làm outcomes trỏ tới evidence không có trong tệp (R10)
            await c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            data = await _dump(c, tenant_id)
            await c.rollback()
        body = [json.dumps({"t": t, "row": json.loads(ln)}, sort_keys=True, ensure_ascii=False) for t, rows in data.items() for ln in rows]
        header = {
            "format": "zeus-brain-backup-1",
            "tenant_id": tenant_id,
            "tables": {t: {"count": len(rows), "sha256": _sha(rows)} for t, rows in data.items()},
        }
        lines = [json.dumps(header, sort_keys=True), *body]
        digest = _checksum(lines)
        _write_atomic(path, "\n".join(lines) + "\n")
        _write_atomic(Path(str(path) + ".sha256"), digest + "\n")
        return {**header, "body_sha256": _sha(body)}

    @staticmethod
    def read_backup(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        path = Path(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise BackupError("backup rỗng")
        side = Path(str(path) + ".sha256")
        if not side.exists() or not hmac.compare_digest(side.read_text().strip(), _checksum(lines)):
            raise BackupError("checksum backup không khớp (tệp hỏng, bị sửa, hoặc thiếu/sai ZEUS_BACKUP_HMAC_KEY)")
        header = json.loads(lines[0])
        rows = [json.loads(x) for x in lines[1:]]
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["t"]] = counts.get(r["t"], 0) + 1
        for t, meta in header.get("tables", {}).items():
            if counts.get(t, 0) != meta["count"]:
                raise BackupError(f"số dòng bảng {t} không khớp header ({counts.get(t, 0)} != {meta['count']})")
        if set(counts) - set(header.get("tables", {})):
            raise BackupError("backup có bảng không khai báo trong header")
        return header, rows

    async def restore(self, path: str | Path, target_tenant: str | None = None) -> EvidenceItem:
        """Nhập vào DB của instance này (đã migrate); kiểm lại count+sha từng bảng; trả EvidenceItem data_match."""
        header, rows = self.read_backup(path)
        tenant = target_tenant or header["tenant_id"]
        if tenant != header["tenant_id"]:
            raise BackupError("đổi tenant khi restore chưa hỗ trợ")
        for r in rows:  # kiểm TRƯỚC khi ghi bất cứ thứ gì: không dòng nào được thuộc tenant khác (SEC-4)
            if r["t"] not in TABLES:
                raise BackupError(f"bảng lạ trong backup: {r['t']}")
            if r["row"].get("tenant_id") != tenant:
                raise BackupError(f"dòng của bảng {r['t']} thuộc tenant {r['row'].get('tenant_id')!r}, khác tenant đích {tenant!r}")
        async with self.conn(autocommit=False) as c:
            cur = await c.execute("SELECT 1 FROM tenants WHERE tenant_id=%s", (tenant,))
            tenant_preexisting = await cur.fetchone() is not None
            if not any(r["t"] == "tenants" for r in rows):  # backup cũ không có bảng tenants
                await c.execute("INSERT INTO tenants (tenant_id, display_name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (tenant, tenant))
            colcache: dict[str, list[str]] = {}
            for r in rows:
                t = r["t"]
                cols = colcache.setdefault(t, await _columns(c, t))
                cl = ", ".join(cols)
                conflict = " ON CONFLICT (tenant_id) DO NOTHING" if t == "tenants" else ""
                try:
                    await c.execute(
                        f"INSERT INTO {t} ({cl}) SELECT {cl} FROM jsonb_populate_record(NULL::{t}, %s){conflict}", (Jsonb(r["row"]),)
                    )
                except psycopg.Error as exc:
                    await c.rollback()
                    raise BackupError(f"restore lỗi ở bảng {t}: {exc}") from exc
            await c.commit()
        async with self.conn() as c:
            got = await _dump(c, tenant)
        bad = [
            t for t, meta in header["tables"].items()
            if not (t == "tenants" and tenant_preexisting)  # tenant có sẵn trên instance này: giữ nguyên cấu hình, không so
            and (len(got.get(t, [])) != meta["count"] or _sha(got.get(t, [])) != meta["sha256"])
        ]
        total = sum(m["count"] for m in header["tables"].values())
        return EvidenceItem(
            kind=EvidenceKind.DATA_MATCH,
            summary=f"restore {total} dòng/{len(header['tables'])} bảng; lệch: {bad or 'không'}",
            sha256=_sha([json.dumps(header["tables"], sort_keys=True)]),
            passed=not bad,
        )
