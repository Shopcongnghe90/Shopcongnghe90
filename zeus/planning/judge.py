"""Judge: deterministic trước. VERIFIED_SUCCESS chỉ khi có bằng chứng MẠNH đã pass (test/build/so khớp/http/người xác nhận)
và không có bằng chứng mạnh nào fail. Lời khẳng định của model (MODEL_JUDGEMENT) không bao giờ đủ.
Model chỉ được phép HẠ (PASS -> NEEDS_HUMAN), không bao giờ nâng."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from zeus.broker.util import parse_json_object
from zeus.contracts.interfaces import ModelBroker, ProviderUnavailable
from zeus.contracts.models import (
    ChatMessage,
    EvidenceItem,
    EvidenceRecord,
    EvidenceStrength,
    ModelRequest,
    ModelRole,
    Outcome,
    ProviderKind,
    Task,
    TestRun,
    Verdict,
    VerdictDecision,
)
from zeus.policy.budget import BudgetExceeded


def strong_signals(items: Sequence[EvidenceItem], tests: Sequence[TestRun]) -> tuple[int, int]:
    """(số bằng chứng mạnh pass, số bằng chứng mạnh fail)."""
    ok = sum(1 for e in items if e.strength is EvidenceStrength.STRONG and e.passed is True) + sum(1 for t in tests if t.ok)
    bad = sum(1 for e in items if e.strength is EvidenceStrength.STRONG and e.passed is False) + sum(1 for t in tests if t.exit_code not in (None, 0) or t.failed > 0)
    return ok, bad


def derive_outcome(items: Sequence[EvidenceItem], tests: Sequence[TestRun], actions_ok: bool = True) -> Outcome:
    ok, bad = strong_signals(items, tests)
    if bad > 0 or (not actions_ok and ok == 0 and (items or tests)):
        return Outcome.VERIFIED_FAILURE
    if ok > 0 and actions_ok:
        return Outcome.VERIFIED_SUCCESS
    return Outcome.UNVERIFIED


_SYSTEM = (
    "Bạn là giám khảo độc lập. Dựa trên tóm tắt bằng chứng trong khối <untrusted_data>, cho biết có mâu thuẫn nào khiến "
    'kết luận THÀNH CÔNG không đáng tin hay không. Trả JSON: {"contradiction": true|false, "reason": "..."}.'
)


class DeterministicJudge:
    def __init__(self, broker: ModelBroker | None = None, maker_providers: Collection[ProviderKind] = ()) -> None:
        self.broker = broker
        self.maker_providers = frozenset(maker_providers)

    def for_makers(self, vendors: Collection[ProviderKind]) -> "DeterministicJudge":
        """Bản sao dùng model phản biện KHÁC hãng với các hãng đã làm việc (planner/executor)."""
        return DeterministicJudge(self.broker, vendors)

    async def judge(self, task: Task, evidence: Sequence[EvidenceRecord]) -> Verdict:
        ids = [e.record_id for e in evidence]
        if not evidence:
            return Verdict(task_id=task.task_id, decision=VerdictDecision.NEEDS_HUMAN, outcome=Outcome.UNVERIFIED, judge="deterministic", reasons=["không có EvidenceRecord"])
        good, bad = [], []
        for rec in evidence:
            actions_ok = all(a.ok for a in rec.actions)
            out = derive_outcome(rec.evidence, rec.tests, actions_ok)
            (good if out is Outcome.VERIFIED_SUCCESS else bad if out is Outcome.VERIFIED_FAILURE else []).append(rec.record_id)
        if bad:
            return Verdict(task_id=task.task_id, decision=VerdictDecision.FAIL, outcome=Outcome.VERIFIED_FAILURE, judge="deterministic", evidence_ids=bad, reasons=["có bằng chứng mạnh cho thấy thất bại"])
        if not good:
            return Verdict(task_id=task.task_id, decision=VerdictDecision.NEEDS_HUMAN, outcome=Outcome.UNVERIFIED, judge="deterministic", evidence_ids=ids, reasons=["không có bằng chứng mạnh (test/build/so khớp/http/người xác nhận); lời khẳng định của model không đủ"])
        verdict = Verdict(task_id=task.task_id, decision=VerdictDecision.PASS, outcome=Outcome.VERIFIED_SUCCESS, judge="deterministic", evidence_ids=good, reasons=["bằng chứng mạnh đã pass"])
        return await self._second_opinion(task, evidence, verdict)

    async def _second_opinion(self, task: Task, evidence: Sequence[EvidenceRecord], verdict: Verdict) -> Verdict:
        if self.broker is None:
            return verdict
        summary = "\n".join(f"- {e.kind.value}: {e.summary} passed={e.passed}" for r in evidence for e in r.evidence)[:4000]
        req = ModelRequest(
            tenant_id=task.tenant_id,
            task_family=task.family,
            role=ModelRole.COMMAND,
            system=_SYSTEM,
            messages=[ChatMessage(role="user", content=f"Mục tiêu: {task.goal}\nBằng chứng:\n{summary}", untrusted=True)],
            max_tokens=300,
            temperature=0.0,
            json_output=True,
            trace=task.trace,
            prompt_version="judge-1",
        )
        try:
            complete = self.broker.complete
            resp = await complete(req, exclude=self.maker_providers) if self.maker_providers else await complete(req)  # type: ignore[call-arg]
            data = parse_json_object(resp.text)
        except (ProviderUnavailable, BudgetExceeded, ValueError):
            return verdict  # không có model => giữ kết luận deterministic
        if resp.provider in self.maker_providers and len(self.maker_providers) > 0:
            raise AssertionError("judge phải khác hãng với bên làm khi có thể")
        who = f"deterministic+{resp.provider.value}:{resp.model}"
        if data.get("contradiction") is True:
            return Verdict(
                task_id=task.task_id,
                decision=VerdictDecision.NEEDS_HUMAN,
                outcome=Outcome.UNVERIFIED,
                judge=who,
                evidence_ids=verdict.evidence_ids,
                reasons=[f"model phản biện nêu mâu thuẫn: {str(data.get('reason', ''))[:300]}"],
            )
        return verdict.model_copy(update={"judge": who})
