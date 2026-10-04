"""Cấu hình thin worker (TOML). Token KHÔNG nằm trong file: đọc từ biến môi trường hoặc token_file (0600)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkerConfig:
    server_url: str
    worker_id: str
    kind: str = "linux_vm"
    tenant_scope: list[str] = field(default_factory=lambda: ["zeusvn"])
    network_zone: str = "agent"
    data_localities: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    extra_capabilities: list[str] = field(default_factory=list)
    max_concurrent: int = 1
    work_dir: Path = Path("/var/lib/zeus-worker")
    token_env: str = "ZEUS_WORKER_TOKEN"
    token_file: Path | None = None
    # executor policy
    http_allow_domains: list[str] = field(default_factory=list)  # rỗng => http.fetch từ chối mọi domain
    file_roots: list[Path] = field(default_factory=list)
    repo_roots: list[Path] = field(default_factory=list)
    shell_allowlist: dict[str, list[str]] = field(default_factory=dict)  # tên -> argv cố định, không nhận tham số
    test_repo: Path | None = None  # test.run: repo chạy pytest (phải nằm trong repo_roots); None => không quảng bá test.run
    test_target: str = ""  # test.run: đường dẫn tương đối trong repo (vd "tests")
    heartbeat_interval_s: float = 15.0
    poll_wait_s: int = 20
    max_output_bytes: int = 64 * 1024
    send_retries: int = 6  # số lần gửi kết quả/upload (lỗi mạng, 5xx, 429); server idempotent nên gửi lại an toàn
    send_retry_base_s: float = 1.0  # backoff gấp đôi mỗi lần, tối đa 30s

    def token(self) -> str:
        if self.token_file and Path(self.token_file).exists():
            return Path(self.token_file).read_text().strip()
        value = os.environ.get(self.token_env, "").strip()
        if not value:
            raise RuntimeError(f"thiếu token worker: đặt {self.token_env} hoặc token_file")
        return value

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkerConfig":
        srv, wk, ex = d.get("server", {}), d.get("worker", {}), d.get("executors", {})
        return cls(
            server_url=srv["url"],
            worker_id=wk["worker_id"],
            kind=wk.get("kind", "linux_vm"),
            tenant_scope=wk.get("tenant_scope", ["zeusvn"]),
            network_zone=wk.get("network_zone", "agent"),
            data_localities=wk.get("data_localities", []),
            labels=wk.get("labels", {}),
            extra_capabilities=wk.get("capabilities", []),
            max_concurrent=wk.get("max_concurrent", 1),
            work_dir=Path(wk.get("work_dir", "/var/lib/zeus-worker")),
            token_env=srv.get("token_env", "ZEUS_WORKER_TOKEN"),
            token_file=Path(srv["token_file"]) if srv.get("token_file") else None,
            http_allow_domains=[x.lower() for x in ex.get("http_allow_domains", [])],
            file_roots=[Path(p) for p in ex.get("file_roots", [])],
            repo_roots=[Path(p) for p in ex.get("repo_roots", [])],
            shell_allowlist={k: list(v) for k, v in ex.get("shell_allowlist", {}).items()},
            test_repo=Path(ex["test_repo"]) if ex.get("test_repo") else None,
            test_target=str(ex.get("test_target", "")),
            heartbeat_interval_s=float(wk.get("heartbeat_interval_s", 15)),
            poll_wait_s=int(wk.get("poll_wait_s", 20)),
            send_retries=int(wk.get("send_retries", 6)),
            send_retry_base_s=float(wk.get("send_retry_base_s", 1.0)),
        )

    @classmethod
    def load(cls, path: str | Path) -> "WorkerConfig":
        return cls.from_dict(tomllib.loads(Path(path).read_text(encoding="utf-8")))
