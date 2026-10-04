"""Luật độ mạnh bằng chứng — "claims are not evidence".

- strong: có EvidenceItem thuộc STRONG_EVIDENCE_KINDS đã qua (passed=True) hoặc TestRun ok.
- VERIFIED_SUCCESS: cần strong và KHÔNG có bằng chứng mạnh nào thất bại (test fail, passed=False, action lỗi).
- VERIFIED_FAILURE: cần ít nhất một bằng chứng có thật (item/test/action) — không chấp nhận lời khẳng định trống.
- model_judgement không bao giờ là strong.
"""

from __future__ import annotations

import re

from zeus.contracts.models import EvidenceRecord, EvidenceStrength, Outcome

ARTIFACT_REF_RE = re.compile(r"artifact://([a-z0-9][a-z0-9_-]{1,62})/([0-9a-f]{64})")


class EvidenceRuleError(ValueError):
    pass


def has_failing_strong(r: EvidenceRecord) -> bool:
    if any(e.strength is EvidenceStrength.STRONG and e.passed is False for e in r.evidence):
        return True
    if any(t.failed > 0 or (t.exit_code not in (None, 0)) for t in r.tests):
        return True
    return any(not a.ok for a in r.actions)


def strength_of(r: EvidenceRecord) -> str:
    strong = any(e.strength is EvidenceStrength.STRONG and e.passed is True for e in r.evidence) or any(
        t.ok for t in r.tests
    )
    if strong:
        return "strong"
    return "weak" if (r.evidence or r.tests or r.actions) else "none"


def check_record(r: EvidenceRecord) -> str:
    """Ném EvidenceRuleError nếu outcome không được bằng chứng đỡ; trả strength."""
    s = strength_of(r)
    if r.final_outcome is Outcome.VERIFIED_SUCCESS:
        if s != "strong":
            raise EvidenceRuleError("VERIFIED_SUCCESS cần bằng chứng mạnh đã qua (test/build/data_match/http/human)")
        if has_failing_strong(r):
            raise EvidenceRuleError("VERIFIED_SUCCESS bị chặn: tồn tại bằng chứng/test/action thất bại")
    elif r.final_outcome is Outcome.VERIFIED_FAILURE and s == "none":
        raise EvidenceRuleError("VERIFIED_FAILURE cần ít nhất một bằng chứng thực (item/test/action)")
    return s


def artifact_refs(payload_json: str) -> set[tuple[str, str]]:
    """(tenant, sha256) của mọi ``artifact://`` trong bản ghi."""
    return set(ARTIFACT_REF_RE.findall(payload_json))
