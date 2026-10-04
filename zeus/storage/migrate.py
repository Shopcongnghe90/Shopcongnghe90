"""Migration runner: áp dụng migrations/NNN_<tên>.sql theo thứ tự số, ghi schema_migrations.

Quy tắc (ADR-009):
- Tên tệp ``NNN_<workstream>_<mô_tả>.sql`` (NNN 3 chữ số). Dải sở hữu: 0xx shared, 1xx A, 2xx B, 3xx C, 4xx D.
- Migration đã áp dụng là BẤT BIẾN: checksum lệch => MigrationError (sửa bằng migration mới).
- Mỗi migration chạy trong 1 transaction; advisory lock chống chạy song song.
- Chạy lại là idempotent (không áp dụng lại bản đã có).

CLI: ``python -m zeus.storage.migrate [--dsn DSN] [--dir migrations] [--status]``
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

_NAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x5A455553  # "ZEUS"
_OWNERS = {0: "shared", 1: "A", 2: "B", 3: "C", 4: "D"}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str

    @property
    def owner(self) -> str:
        return migration_owner(self.version)


def migration_owner(version: int) -> str:
    owner = _OWNERS.get(version // 100)
    if owner is None:
        raise MigrationError(f"migration version {version:03d} nằm ngoài dải 000-499 đã phân công")
    return owner


def discover_migrations(directory: str | Path) -> list[Migration]:
    d = Path(directory)
    if not d.is_dir():
        raise MigrationError(f"không thấy thư mục migrations: {d}")
    found: dict[int, Migration] = {}
    for p in sorted(d.iterdir()):
        if p.suffix != ".sql":
            continue
        m = _NAME_RE.match(p.name)
        if not m:
            raise MigrationError(f"tên migration không hợp lệ: {p.name} (cần NNN_ten.sql)")
        version = int(m.group(1))
        migration_owner(version)
        if version in found:
            raise MigrationError(f"trùng số migration {version:03d}: {found[version].path.name} và {p.name}")
        body = p.read_bytes()
        found[version] = Migration(version, m.group(2), p, hashlib.sha256(body).hexdigest())
    return [found[v] for v in sorted(found)]


_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


def _applied(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
        return {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}


def apply_migrations(dsn: str, directory: str | Path = "migrations") -> list[Migration]:
    """Áp dụng các migration chưa có. Trả danh sách vừa áp dụng."""
    migrations = discover_migrations(directory)
    applied_now: list[Migration] = []
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(_BOOTSTRAP)
            done = _applied(conn)
            for mig in migrations:
                if mig.version in done:
                    name, checksum = done[mig.version]
                    if checksum != mig.checksum:
                        raise MigrationError(
                            f"migration {mig.version:03d}_{mig.name} đã áp dụng nhưng checksum thay đổi "
                            f"({checksum[:12]} -> {mig.checksum[:12]}); migration là bất biến"
                        )
                    continue
                sql = mig.path.read_text(encoding="utf-8")
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(sql)  # type: ignore[arg-type]
                        cur.execute(
                            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                            (mig.version, mig.name, mig.checksum),
                        )
                applied_now.append(mig)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now


def migration_status(dsn: str, directory: str | Path = "migrations") -> list[dict[str, object]]:
    migrations = discover_migrations(directory)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute(_BOOTSTRAP)
        done = _applied(conn)
    rows: list[dict[str, object]] = []
    for mig in migrations:
        state = "pending"
        if mig.version in done:
            state = "applied" if done[mig.version][1] == mig.checksum else "CHECKSUM_MISMATCH"
        rows.append({"version": mig.version, "name": mig.name, "owner": mig.owner, "state": state})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ZeusVN Brain migration runner")
    ap.add_argument("--dsn", default=os.environ.get("ZEUS_DB_DSN"))
    ap.add_argument("--dir", default=os.environ.get("ZEUS_MIGRATIONS_DIR", "migrations"))
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    if not args.dsn:
        print("thiếu --dsn hoặc ZEUS_DB_DSN", file=sys.stderr)
        return 2
    if args.status:
        for row in migration_status(args.dsn, args.dir):
            print(f"{row['version']:03d} {row['owner']:6} {row['state']:18} {row['name']}")
        return 0
    for mig in apply_migrations(args.dsn, args.dir):
        print(f"applied {mig.version:03d}_{mig.name} ({mig.owner})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
