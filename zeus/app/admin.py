"""CLI quản trị tối thiểu (chạy trên server control, cần ZEUS_DB_DSN).

    python -m zeus.app.admin issue-worker-token w-code-1 [--tenant zeusvn]   # in token MỘT LẦN; DB chỉ giữ SHA-256
    python -m zeus.app.admin revoke-worker-tokens w-code-1
    python -m zeus.app.admin hash-password                                  # băm mật khẩu Workbench (đọc từ stdin)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from zeus.config import Settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeus.app.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("issue-worker-token")
    p.add_argument("worker_id")
    p.add_argument("--tenant", action="append", dest="tenants")
    p = sub.add_parser("revoke-worker-tokens")
    p.add_argument("worker_id")
    sub.add_parser("hash-password")
    args = ap.parse_args(argv)

    if args.cmd == "hash-password":
        from zeus.workbench.security import hash_password

        pw = getpass.getpass("Mật khẩu Workbench: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        if len(pw) < 12:
            print("mật khẩu tối thiểu 12 ký tự", file=sys.stderr)
            return 2
        print(hash_password(pw))
        return 0

    settings = Settings.from_env()
    if not settings.db_dsn:
        print("thiếu ZEUS_DB_DSN", file=sys.stderr)
        return 2
    from zeus.workers.registry import PgWorkerRegistry

    reg = PgWorkerRegistry(settings.db_dsn)
    if args.cmd == "issue-worker-token":
        print(asyncio.run(reg.issue_token(args.worker_id, args.tenants)))
    else:
        print(asyncio.run(reg.revoke_tokens(args.worker_id)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
