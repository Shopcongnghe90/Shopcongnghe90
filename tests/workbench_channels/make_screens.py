"""Chụp màn hình Workbench thật (uvicorn + Chromium headless) -> docs/reports/D/screens/*.png.

Chạy: /opt/zeus/venv/bin/python -m tests.workbench_channels.make_screens
Dữ liệu là DỮ LIỆU GIẢ của zeus.workbench.demo; autologin chỉ tồn tại trong demo.
"""

from __future__ import annotations

import glob
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/reports/D/screens"
PAGES = {"tong-quan": "", "ra-lenh": "/command", "workflow-dag": None, "duyet": "/approvals", "workers": "/workers", "chi-phi": "/costs", "hoc-tap": "/learning"}


def chromium(small: bool = False) -> str:
    """Chrome headless mới có độ rộng cửa sổ tối thiểu ~500px; ảnh mobile dùng headless_shell để đúng 390px."""
    shell = sorted(glob.glob("/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell"))
    full = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    c = (shell or full) if small else (full or shell)
    if not c:
        raise SystemExit("không thấy Chromium trong /opt/pw-browsers")
    return c[0]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {"ZEUS_WB_DEMO_PASSWORD": "demo-chi-de-chup-hinh", "PATH": "/usr/bin:/bin"}
    srv = subprocess.Popen([sys.executable, "-m", "zeus.workbench.demo", "--port", str(port), "--autologin"], cwd=ROOT, env=env)
    try:
        base = f"http://127.0.0.1:{port}/wb"
        for _ in range(100):
            try:
                httpx.get(base + "/login", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        tid = re.search(r"/wb/workflows/(tsk_[a-z0-9]+)\">Sửa lỗi in hoá đơn", httpx.get(base + "/workflows").text).group(1)  # type: ignore[union-attr]
        for name, path in PAGES.items():
            url = base + (path if path is not None else f"/workflows/{tid}")
            for tag, size in (("desktop", "1280,860"), ("mobile", "390,844")):
                if tag == "mobile" and name not in ("tong-quan", "duyet"):
                    continue
                out = OUT / f"{name}-{tag}.png"
                subprocess.run([chromium(tag == "mobile"), "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", f"--window-size={size}", f"--screenshot={out}", url], check=True, capture_output=True, timeout=60)
                print(out.relative_to(ROOT), out.stat().st_size)
    finally:
        srv.terminate()
        srv.wait(10)


if __name__ == "__main__":
    main()
