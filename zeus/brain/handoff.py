"""Incremental context diff + HandoffPacket có trần kích thước."""

from __future__ import annotations

from pydantic import BaseModel, Field

from zeus.contracts.models import ContextPacket, HandoffPacket


class ContextDiff(BaseModel):
    added: list[str] = Field(default_factory=list)  # memory_id mới
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    canonical_changed: list[str] = Field(default_factory=list)  # key canonical_state đổi/thêm/bỏ
    new_conflicts: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.canonical_changed or self.new_conflicts)


def context_diff(old: ContextPacket, new: ContextPacket) -> ContextDiff:
    o = {h.memory.memory_id for h in old.hits}
    n = {h.memory.memory_id for h in new.hits}
    keys = set(old.canonical_state) | set(new.canonical_state)
    return ContextDiff(
        added=sorted(n - o),
        removed=sorted(o - n),
        unchanged=sorted(o & n),
        canonical_changed=sorted(k for k in keys if old.canonical_state.get(k) != new.canonical_state.get(k)),
        new_conflicts=[c for c in new.conflicts if c not in set(old.conflicts)],
    )


def apply_diff_view(old: ContextPacket, new: ContextPacket) -> ContextPacket:
    """Gói chỉ chứa phần MỚI (hit mới + canonical đổi) để gửi tiếp cho agent đã có ``old``."""
    d = context_diff(old, new)
    added = set(d.added)
    return new.model_copy(
        update={
            "hits": [h for h in new.hits if h.memory.memory_id in added],
            "canonical_state": {k: new.canonical_state[k] for k in d.canonical_changed if k in new.canonical_state},
            "conflicts": d.new_conflicts,
        }
    )


def _size(p: HandoffPacket) -> int:
    return len(p.model_dump_json().encode())


def build_handoff(
    *,
    tenant_id: str,
    task_id: str,
    from_agent: str,
    to_agent: str,
    summary: str,
    context: ContextPacket | None = None,
    open_questions: list[str] | None = None,
    constraints: list[str] | None = None,
    artifact_refs: list[str] | None = None,
    next_action: str | None = None,
    max_bytes: int = 8192,
) -> HandoffPacket:
    """Handoff ngắn: nếu vượt ``max_bytes`` thì cắt dần hit (điểm thấp trước), rồi bỏ context, rồi cắt summary."""
    p = HandoffPacket(
        tenant_id=tenant_id, task_id=task_id, from_agent=from_agent, to_agent=to_agent, summary=summary,
        context=context, open_questions=open_questions or [], constraints=constraints or [],
        artifact_refs=artifact_refs or [], next_action=next_action,
    )
    if p.context is not None:
        ctx = p.context
        while _size(p) > max_bytes and ctx.hits:
            ctx = ctx.model_copy(update={"hits": ctx.hits[:-1]})
            p = p.model_copy(update={"context": ctx})
        if _size(p) > max_bytes:
            p = p.model_copy(update={"context": None})
    while _size(p) > max_bytes and len(p.summary) > 80:
        p = p.model_copy(update={"summary": p.summary[: max(80, int(len(p.summary) * 0.8))].rstrip() + "…"})
    if _size(p) > max_bytes:
        raise ValueError(f"handoff vượt {max_bytes} byte dù đã cắt (constraints/questions quá lớn)")
    return p
