"""Conflict Resolver theo SOURCE PRIORITY: repo/tests/evidence > decisions > MASTER STATE > spec > historical.

Hai hit xung đột khi cùng tag ``topic:<x>`` nhưng nội dung khác. Thắng: ưu tiên nguồn cao hơn; cùng mức: valid_from mới hơn.
Nguồn được suy ra từ tag ``src:<repo|tests|evidence|decision|master|spec|historical>`` hoặc từ ``kind``.
"""

from __future__ import annotations

from dataclasses import dataclass

from zeus.brain.text import strip_accents
from zeus.contracts.models import MemoryItem, MemoryKind, RetrievalHit

PRIORITY = {"repo": 5, "tests": 5, "evidence": 5, "decision": 4, "master": 3, "spec": 2, "historical": 1}
_KIND_DEFAULT = {
    MemoryKind.EVIDENCE: "evidence",
    MemoryKind.DECISION: "decision",
    MemoryKind.CANONICAL_STATE: "master",
    MemoryKind.SEMANTIC: "spec",
    MemoryKind.ARTIFACT: "spec",
    MemoryKind.EPISODIC: "historical",
}


def source_class(item: MemoryItem) -> str:
    for t in item.tags:
        if t.startswith("src:") and t[4:] in PRIORITY:
            return t[4:]
    return _KIND_DEFAULT[item.kind]


def source_priority(item: MemoryItem) -> int:
    return PRIORITY[source_class(item)]


def _topics(item: MemoryItem) -> set[str]:
    return {t for t in item.tags if t.startswith("topic:")}


def _same_claim(a: MemoryItem, b: MemoryItem) -> bool:
    return strip_accents(" ".join(a.content.split())) == strip_accents(" ".join(b.content.split()))


@dataclass
class Resolution:
    kept: list[RetrievalHit]
    conflicts: list[str]


def resolve_conflicts(hits: list[RetrievalHit]) -> Resolution:
    """Giữ 1 hit thắng cho mỗi topic xung đột; ghi lại mô tả xung đột (không lặng lẽ bỏ)."""
    dropped: set[str] = set()
    notes: list[str] = []
    by_topic: dict[str, list[RetrievalHit]] = {}
    for h in hits:
        for t in _topics(h.memory):
            by_topic.setdefault(t, []).append(h)
    for topic in sorted(by_topic):
        group = [h for h in by_topic[topic] if h.memory.memory_id not in dropped]
        if len(group) < 2:
            continue
        ranked = sorted(group, key=lambda h: (source_priority(h.memory), h.memory.valid_from), reverse=True)
        winner = ranked[0]
        for loser in ranked[1:]:
            if _same_claim(winner.memory, loser.memory):
                continue
            dropped.add(loser.memory.memory_id)
            wp, lp = source_class(winner.memory), source_class(loser.memory)
            reason = "nguồn ưu tiên cao hơn" if source_priority(winner.memory) > source_priority(loser.memory) else "mới hơn"
            notes.append(
                f"{topic}: giữ {winner.memory.memory_id} ({wp}), loại {loser.memory.memory_id} ({lp}) — {reason}"
            )
    kept = [h for h in hits if h.memory.memory_id not in dropped]
    return Resolution(kept, notes)
