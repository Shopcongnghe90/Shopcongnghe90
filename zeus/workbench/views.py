"""Logic thuần cho các trang: tổng hợp chi phí/độ trễ, champion/challenger, bố cục DAG SVG, đọc Decision Ledger."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from zeus.contracts.models import EvidenceRecord, Outcome, RouterStat, TaskGraph, TaskStatus

MIN_N_FOR_CHAMPION = 5  # dưới ngưỡng này chưa đủ dữ liệu để phong champion
STALE_HEARTBEAT_S = 60


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


@dataclass
class CostRow:
    key: str
    n: int
    cost_usd: float
    avg_latency_ms: int
    p95_latency_ms: int
    verified_success: int

    @property
    def cost_per_verified_success(self) -> float | None:
        return self.cost_usd / self.verified_success if self.verified_success else None


def aggregate_costs(records: list[EvidenceRecord], by: str, today: date | None = None) -> list[CostRow]:
    """by: 'model' | 'day' | 'family'."""
    groups: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for r in records:
        key = {"model": f"{r.model_provider.value}/{r.model_name}" if r.model_provider else "(không dùng model)", "day": r.created_at.date().isoformat(), "family": r.task_family.value}[by]
        groups[key].append(r)
    rows = []
    for k, rs in groups.items():
        lat = [r.latency_ms for r in rs]
        rows.append(
            CostRow(k, len(rs), round(sum(r.cost_usd for r in rs), 6), int(sum(lat) / len(lat)), _pct(lat, 0.95), sum(1 for r in rs if r.final_outcome is Outcome.VERIFIED_SUCCESS))
        )
    return sorted(rows, key=lambda r: r.key, reverse=(by == "day"))


def cost_today(records: list[EvidenceRecord], now: datetime) -> float:
    d = now.astimezone(timezone.utc).date()
    return round(sum(r.cost_usd for r in records if r.created_at.astimezone(timezone.utc).date() == d), 6)


def champions(stats: list[RouterStat]) -> list[dict[str, object]]:
    """Mỗi family: champion = model có cost/verified success thấp nhất trong số n >= MIN_N; còn lại là challenger."""
    by_family: dict[str, list[RouterStat]] = defaultdict(list)
    for s in stats:
        by_family[s.task_family.value].append(s)
    out: list[dict[str, object]] = []
    for fam, ss in sorted(by_family.items()):
        eligible = [s for s in ss if s.n >= MIN_N_FOR_CHAMPION and s.cost_per_verified_success is not None]
        champ = min(eligible, key=lambda s: s.cost_per_verified_success or 0.0) if eligible else None
        for s in sorted(ss, key=lambda s: (s.cost_per_verified_success is None, s.cost_per_verified_success or 0.0)):
            role = "champion" if s is champ else ("challenger" if champ else "chưa đủ dữ liệu")
            out.append({"family": fam, "stat": s, "role": role})
    return out


def is_stale(last_seen: datetime | None, now: datetime) -> bool:
    return last_seen is None or (now - last_seen) > timedelta(seconds=STALE_HEARTBEAT_S)


def count_by_status(tasks: list) -> dict[str, int]:
    c: dict[str, int] = {s.value: 0 for s in TaskStatus}
    for t in tasks:
        c[t.status.value] += 1
    return c


# --------------------------------------------------------------------------- DAG -> SVG

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 168, 52, 56, 28, 16


def layout_dag(graph: TaskGraph) -> dict[str, object]:
    """Bố cục theo tầng (độ sâu = đường dài nhất từ nguồn), trái -> phải. Trả toạ độ để template vẽ SVG."""
    nodes = {n.node_id: n for n in graph.nodes}
    depth: dict[str, int] = {}

    def d(nid: str) -> int:
        if nid not in depth:
            depth[nid] = 1 + max((d(p) for p in nodes[nid].depends_on if p in nodes), default=-1)
        return depth[nid]

    for nid in nodes:
        d(nid)
    layers: dict[int, list[str]] = defaultdict(list)
    for nid in nodes:  # giữ thứ tự khai báo trong từng tầng
        layers[depth[nid]].append(nid)
    pos: dict[str, tuple[int, int]] = {}
    for dep, ids in layers.items():
        for row, nid in enumerate(ids):
            pos[nid] = (PAD + dep * (NODE_W + GAP_X), PAD + row * (NODE_H + GAP_Y))
    out_nodes = [{"id": nid, "title": nodes[nid].title, "status": nodes[nid].status.value, "risk": nodes[nid].risk.value, "x": x, "y": y} for nid, (x, y) in pos.items()]
    edges = []
    for n in graph.nodes:
        for p in n.depends_on:
            if p in pos:
                x1, y1 = pos[p][0] + NODE_W, pos[p][1] + NODE_H // 2
                x2, y2 = pos[n.node_id][0], pos[n.node_id][1] + NODE_H // 2
                mx = (x1 + x2) // 2
                edges.append({"path": f"M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}", "from": p, "to": n.node_id})
    width = PAD * 2 + (max(layers) + 1) * NODE_W + max(layers) * GAP_X if layers else PAD * 2
    height = PAD * 2 + (max(len(v) for v in layers.values()) if layers else 0) * (NODE_H + GAP_Y) - GAP_Y
    return {"nodes": out_nodes, "edges": edges, "width": width, "height": max(height, 40), "node_w": NODE_W, "node_h": NODE_H}


# --------------------------------------------------------------------------- Decision Ledger

_ADR_RE = re.compile(r"^## (ADR-\d+) — (.+)$")
_BACKTICK_RE = re.compile(r"`+")


def parse_ledger(path: Path) -> list[dict[str, str]]:
    """Đọc docs/state/DECISION_LEDGER.md -> [{id,title,date,decision,status}]. Chỉ đọc, không sửa."""
    if not path.is_file():
        return []
    items: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ADR_RE.match(line)
        if m:
            cur = {"id": m.group(1), "title": _BACKTICK_RE.sub("", m.group(2)), "date": "", "decision": "", "status": ""}
            items.append(cur)
        elif cur is not None:
            for label, key in (("Ngày", "date"), ("Quyết định", "decision"), ("Trạng thái", "status")):
                prefix = f"- **{label}:**"
                if line.startswith(prefix):
                    cur[key] = _BACKTICK_RE.sub("", line[len(prefix) :]).strip()
    return items
