"""Executor có kiểu (typed). Mỗi executor nhận args đã kiểm tra; KHÔNG có executor shell tự do.

- noop.echo, http.fetch (+ http.check), file.checksum, repo.tests.run, shell.allowlisted.
- shell.allowlisted chỉ chạy lệnh có TÊN trong cấu hình (argv cố định, không nhận tham số từ LLM).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from zeus.contracts.models import EvidenceItem, EvidenceKind, TestRun, TypedAction
from zeus_worker.config import WorkerConfig


class ExecError(Exception):
    """Lỗi hợp lệ phía executor (args sai, bị chính sách từ chối)."""


@dataclass
class ExecOutput:
    output: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    log: str = ""
    evidence: list[EvidenceItem] = field(default_factory=list)
    artifacts: list[tuple[str, bytes, str]] = field(default_factory=list)  # (name, data, mime)


@dataclass
class ExecContext:
    action: TypedAction
    cfg: WorkerConfig
    http_transport: httpx.AsyncBaseTransport | None = None


class Executor(Protocol):
    name: str

    async def run(self, ctx: ExecContext) -> ExecOutput: ...


def _under(path: Path, roots: list[Path]) -> Path:
    p = path.resolve()
    for r in roots:
        try:
            p.relative_to(r.resolve())
            return p
        except ValueError:
            continue
    raise ExecError(f"đường dẫn ngoài thư mục cho phép: {path}")


async def run_process(argv: list[str], *, cwd: Path | None, limit: int) -> tuple[int, str]:
    """Chạy argv (không shell), gom stdout+stderr (cắt ở limit); bị cancel/timeout thì kill tiến trình."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await proc.communicate()
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out[-limit:].decode("utf-8", "replace")


class NoopEcho:
    name = "noop.echo"

    async def run(self, ctx: ExecContext) -> ExecOutput:
        delay = float(ctx.action.args.get("delay_s", 0))
        if not 0 <= delay <= 300:
            raise ExecError("delay_s phải trong [0,300]")
        if delay:
            await asyncio.sleep(delay)
        echo = {k: v for k, v in ctx.action.args.items() if k != "delay_s"}
        return ExecOutput(output={"echo": echo}, log=f"echo {echo}\n")


class HttpFetch:
    name = "http.fetch"

    @staticmethod
    def allowed(host: str, allow: list[str]) -> bool:
        host = host.lower()
        return any(host == d or (d.startswith(".") and host.endswith(d)) or host.endswith("." + d.lstrip(".")) for d in allow)

    async def run(self, ctx: ExecContext) -> ExecOutput:
        url = str(ctx.action.args.get("url", ""))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ExecError("url phải là http(s)")
        if not self.allowed(parts.hostname, ctx.cfg.http_allow_domains):
            raise ExecError(f"domain {parts.hostname} không nằm trong allowlist")
        expect = int(ctx.action.args.get("expect_status", 200))
        async with httpx.AsyncClient(transport=ctx.http_transport, follow_redirects=False, timeout=30) as c:
            r = await c.get(url)
        body = r.content[: ctx.cfg.max_output_bytes]
        sha = hashlib.sha256(r.content).hexdigest()
        ok = r.status_code == expect
        ev = EvidenceItem(kind=EvidenceKind.HTTP_CHECK, summary=f"GET {url} -> {r.status_code} (kỳ vọng {expect})", sha256=sha, passed=ok)
        return ExecOutput(
            output={"status": r.status_code, "sha256": sha, "size": len(r.content), "body_preview": body[:512].decode("utf-8", "replace")},
            ok=ok, error=None if ok else f"status {r.status_code} != {expect}", log=f"GET {url} -> {r.status_code}\n", evidence=[ev],
        )


class FileChecksum:
    name = "file.checksum"

    async def run(self, ctx: ExecContext) -> ExecOutput:
        p = _under(Path(str(ctx.action.args.get("path", ""))), ctx.cfg.file_roots)
        if not p.is_file():
            raise ExecError(f"không phải tệp: {p}")
        h, size = hashlib.sha256(), 0
        with p.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
                size += len(chunk)
        sha = h.hexdigest()
        expected = ctx.action.args.get("expected_sha256")
        ok = expected is None or expected == sha
        ev = EvidenceItem(kind=EvidenceKind.DATA_MATCH, summary=f"sha256({p.name})={sha[:12]}", sha256=sha, passed=ok if expected else None)
        return ExecOutput(output={"sha256": sha, "size": size}, ok=ok, error=None if ok else "sha256 không khớp", evidence=[ev])


_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|skipped|error|errors)")


def parse_pytest_summary(text: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    last = next((l for l in reversed(text.strip().splitlines()) if _PYTEST_COUNT.search(l)), "")
    for n, kind in _PYTEST_COUNT.findall(last):
        counts["error" if kind.startswith("error") else kind] += int(n)
    return counts


class RepoTestsRun:
    name = "repo.tests.run"

    async def run(self, ctx: ExecContext) -> ExecOutput:
        repo = _under(Path(str(ctx.action.args.get("repo", ""))), ctx.cfg.repo_roots)
        target = str(ctx.action.args.get("target", ""))
        if target.startswith("-") or ".." in Path(target).parts or Path(target).is_absolute():
            raise ExecError("target không hợp lệ")
        argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *([target] if target else [])]
        code, out = await run_process(argv, cwd=repo, limit=ctx.cfg.max_output_bytes)
        c = parse_pytest_summary(out)
        run = TestRun(name=f"pytest:{repo.name}", command=" ".join(argv[2:]), passed=c["passed"], failed=c["failed"] + c["error"], skipped=c["skipped"], exit_code=code)
        ev = EvidenceItem(kind=EvidenceKind.TEST_RESULT, summary=f"{run.passed} passed, {run.failed} failed, {run.skipped} skipped (exit {code})", passed=run.ok)
        return ExecOutput(output={"test_run": run.model_dump(mode="json")}, ok=run.ok, error=None if run.ok else f"tests exit {code}", log=out, evidence=[ev])


class ShellAllowlisted:
    name = "shell.allowlisted"

    async def run(self, ctx: ExecContext) -> ExecOutput:
        key = str(ctx.action.args.get("command", ""))
        argv = ctx.cfg.shell_allowlist.get(key)
        if not argv:
            raise ExecError(f"lệnh '{key}' không nằm trong allowlist")
        code, out = await run_process(argv, cwd=None, limit=ctx.cfg.max_output_bytes)
        return ExecOutput(output={"exit_code": code, "stdout": out[-4096:]}, ok=code == 0, error=None if code == 0 else f"exit {code}", log=out)


class ExecutorRegistry:
    def __init__(self, executors: list[Executor] | None = None) -> None:
        self._by_name: dict[str, Executor] = {}
        for e in executors or default_executors():
            self.add(e)
        self._by_name.setdefault("http.check", self._by_name["http.fetch"])

    def add(self, e: Executor) -> None:
        self._by_name[e.name] = e

    def get(self, name: str) -> Executor | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self._by_name)


def default_executors() -> list[Executor]:
    return [NoopEcho(), HttpFetch(), FileChecksum(), RepoTestsRun(), ShellAllowlisted()]
