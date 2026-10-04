"""Backup/restore Brain DB theo tenant: JSONL có checksum + kiểm khôi phục (evidence data_match).

Định dạng: dòng 1 = header {tables:{name:{count,sha256}}}; các dòng sau {"t": table, "row": {...}}.
Sidecar ``<file>.sha256`` = sha256 của toàn bộ phần thân (sau header). Tệp artifact (nhị phân) không nằm trong JSONL —
chúng content-addressed nên sao chép thư mục artifacts riêng (rsync/ZFS snapshot) và kiểm bằng ``ArtifactStore.verify``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from zeus.brain.pg import PgBase
from zeus.contracts.models import EvidenceItem, EvidenceKind

# thứ tự tôn trọng FK
TABLES = [
    "memory_items", "canonical_state", "decision_ledger", "retention_policies", "artifacts", "evidence_records",
    "evidence_artifact_links", "outcomes", "dataset_records", "router_stats", "champion_challenger",
    "eval_datasets", "eval_runs", "eval_results",
]


class BackupError(RuntimeError):
    pass


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
        async with self.conn() as c:
            data = await _dump(c, tenant_id)
        body = [json.dumps({"t": t, "row": json.loads(ln)}, sort_keys=True, ensure_ascii=False) for t, rows in data.items() for ln in rows]
        header = {
            "format": "zeus-brain-backup-1",
            "tenant_id": tenant_id,
            "tables": {t: {"count": len(rows), "sha256": _sha(rows)} for t, rows in data.items()},
        }
        path.write_text("\n".join([json.dumps(header, sort_keys=True), *body]) + "\n", encoding="utf-8")
        body_sha = _sha(body)
        Path(str(path) + ".sha256").write_text(body_sha + "\n")
        return {**header, "body_sha256": body_sha}

    @staticmethod
    def read_backup(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        path = Path(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise BackupError("backup rỗng")
        side = Path(str(path) + ".sha256")
        if not side.exists() or side.read_text().strip() != _sha(lines[1:]):
            raise BackupError("checksum backup không khớp (tệp hỏng hoặc bị sửa)")
        return json.loads(lines[0]), [json.loads(x) for x in lines[1:]]

    async def restore(self, path: str | Path, target_tenant: str | None = None) -> EvidenceItem:
        """Nhập vào DB của instance này (đã migrate); kiểm lại count+sha từng bảng; trả EvidenceItem data_match."""
        header, rows = self.read_backup(path)
        tenant = target_tenant or header["tenant_id"]
        if tenant != header["tenant_id"]:
            raise BackupError("đổi tenant khi restore chưa hỗ trợ")
        async with self.conn(autocommit=False) as c:
            await c.execute(
                "INSERT INTO tenants (tenant_id, display_name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (tenant, tenant)
            )
            colcache: dict[str, list[str]] = {}
            for r in rows:
                t = r["t"]
                if t not in TABLES:
                    raise BackupError(f"bảng lạ trong backup: {t}")
                cols = colcache.setdefault(t, await _columns(c, t))
                cl = ", ".join(cols)
                try:
                    await c.execute(
                        f"INSERT INTO {t} ({cl}) SELECT {cl} FROM jsonb_populate_record(NULL::{t}, %s)", (Jsonb(r["row"]),)
                    )
                except psycopg.Error as exc:
                    await c.rollback()
                    raise BackupError(f"restore lỗi ở bảng {t}: {exc}") from exc
            await c.commit()
        async with self.conn() as c:
            got = await _dump(c, tenant)
        bad = [
            t for t, meta in header["tables"].items()
            if len(got.get(t, [])) != meta["count"] or _sha(got.get(t, [])) != meta["sha256"]
        ]
        total = sum(m["count"] for m in header["tables"].values())
        return EvidenceItem(
            kind=EvidenceKind.DATA_MATCH,
            summary=f"restore {total} dòng/{len(header['tables'])} bảng; lệch: {bad or 'không'}",
            sha256=_sha([json.dumps(header["tables"], sort_keys=True)]),
            passed=not bad,
        )
