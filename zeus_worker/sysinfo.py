"""Thu thập CPU/RAM/disk (/proc + shutil) và inventory phần mềm. Không cần psutil."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

from zeus.contracts.models import GpuInfo, Inventory, WorkerCapability

SOFTWARE_PROBES = ["git", "python3", "node", "npm", "docker", "chromium", "google-chrome", "pytest", "nvidia-smi", "odoo"]


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _cpu_times() -> tuple[int, int]:
    """(idle, total) từ dòng 'cpu ' của /proc/stat."""
    line = next((l for l in _read("/proc/stat").splitlines() if l.startswith("cpu ")), "")
    parts = [int(x) for x in line.split()[1:]] if line else []
    if len(parts) < 5:
        return 0, 0
    idle = parts[3] + parts[4]  # idle + iowait
    return idle, sum(parts[:8])


class CpuSampler:
    """% CPU rảnh giữa hai lần gọi (lần đầu: so với lúc khởi động)."""

    def __init__(self) -> None:
        self._prev = _cpu_times()

    def available_pct(self) -> float:
        idle, total = _cpu_times()
        pi, pt = self._prev
        self._prev = (idle, total)
        dt = total - pt
        if dt <= 0:
            return 100.0
        return max(0.0, min(100.0, 100.0 * (idle - pi) / dt))


def ram_mb() -> tuple[int, int]:
    """(total_mb, available_mb)."""
    info: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.split():
            info[k] = int(v.split()[0])
    total = info.get("MemTotal", 0) // 1024
    return max(total, 1), info.get("MemAvailable", info.get("MemFree", 0)) // 1024


def disk_free_gb(path: str | Path = "/") -> float:
    try:
        return round(shutil.disk_usage(path).free / 1024**3, 2)
    except OSError:
        return 0.0


def gpus() -> list[GpuInfo]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    res = []
    for line in out.strip().splitlines():
        name, mem, drv = [x.strip() for x in line.split(",")]
        res.append(GpuInfo(name=name, vram_mb=int(float(mem)), driver=drv))
    return res


def software_inventory() -> list[WorkerCapability]:
    return [WorkerCapability(name=n.replace("-", "_").replace("3", ""), attrs={"path": p}) for n in SOFTWARE_PROBES if (p := shutil.which(n))]


def inventory(labels: dict[str, str] | None = None, disk_path: str | Path = "/") -> Inventory:
    total, _ = ram_mb()
    return Inventory(
        hostname=socket.gethostname(), os=f"{platform.system()} {platform.release()}", arch=platform.machine() or "x86_64",
        cpu_count=os.cpu_count() or 1, ram_mb=total, disk_free_gb=disk_free_gb(disk_path), gpus=gpus(), labels=labels or {},
    )
