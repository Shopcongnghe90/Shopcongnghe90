"""Quét tĩnh: mã runtime không chứa phụ thuộc Claude Cloud (cùng danh sách mẫu với scripts/cloud_exit_check.sh)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _patterns() -> list[re.Pattern[str]]:
    lines = (ROOT / "scripts" / "cloud_exit_forbidden.txt").read_text().splitlines()
    return [re.compile(l.strip(), re.I) for l in lines if l.strip() and not l.startswith("#")]


def test_runtime_code_has_no_claude_cloud_dependency():
    pats = _patterns()
    assert len(pats) >= 6
    hits = []
    for pkg in ("zeus", "zeus_worker"):
        for p in (ROOT / pkg).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".sql", ".yaml", ".yml", ".json", ".html", ".js", ".css", ".toml", ".sh"}:
                text = p.read_text(encoding="utf-8", errors="replace")
                for pat in pats:
                    for m in pat.finditer(text):
                        hits.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    assert hits == []


def test_cloud_exit_flag_defaults_false():
    from zeus.config import Settings

    assert Settings.from_env({}).claude_cloud_available is False
