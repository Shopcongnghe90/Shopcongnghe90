"""Risk Engine deterministic R0–R3, mỗi kết luận có lý do.

R0 đọc; R1 ghi nội bộ; R2 ghi dữ liệu thật/production/ra ngoài; R3 tiền/xoá/secret/leo thang.
Mẫu nguy hiểm => R3 + data_class ``deny.*`` (thực thi bị từ chối cứng, không thể duyệt qua workflow).
"""

from __future__ import annotations

import json
import re

from zeus.contracts.models import ActionSpec, Event, Intent, RiskAssessment, RiskLevel, TaskFamily, TypedAction
from zeus.intent.text import fold
from zeus.policy.redact import detect_pii
from zeus.risk.patterns import find_dangerous, find_injection

_FAMILY_BASE: dict[TaskFamily, RiskLevel] = {
    TaskFamily.ERP_BUG: RiskLevel.R1,
    TaskFamily.ERP_FEATURE: RiskLevel.R1,
    TaskFamily.WEBSITE_EDIT: RiskLevel.R1,
    TaskFamily.WEBSITE_BUILD: RiskLevel.R1,
    TaskFamily.FRONTEND: RiskLevel.R1,
    TaskFamily.BACKEND: RiskLevel.R1,
    TaskFamily.DATABASE: RiskLevel.R1,
    TaskFamily.CUSTOMER_SUPPORT: RiskLevel.R1,
    TaskFamily.ZALO_ISSUE: RiskLevel.R1,
    TaskFamily.FACEBOOK_ISSUE: RiskLevel.R1,
    TaskFamily.SHOPEE_ISSUE: RiskLevel.R1,
    TaskFamily.DOMAIN_PROVISIONING: RiskLevel.R2,
    TaskFamily.DEPLOYMENT: RiskLevel.R2,
    TaskFamily.SECURITY: RiskLevel.R2,
    TaskFamily.WORKER_SCHEDULING: RiskLevel.R1,
    TaskFamily.VISUAL_QA: RiskLevel.R0,
    TaskFamily.MODEL_ROUTING: RiskLevel.R0,
    TaskFamily.TOOL_SELECTION: RiskLevel.R0,
    TaskFamily.GENERAL: RiskLevel.R0,
}
_WRITE_RX = re.compile(r"\b(xoa|sua|update|delete|alter|migrate|ghi|cap nhat|insert)\b")
_PROD_RX = re.compile(r"\b(production|prod|that|live|khach hang that)\b")


def is_denied(assessment: RiskAssessment) -> bool:
    return any(c.startswith("deny.") for c in assessment.data_classes)


class DefaultRiskEngine:
    async def assess(self, event: Event, intent: Intent) -> RiskAssessment:
        text = fold(event.text)
        level = _FAMILY_BASE.get(intent.family, RiskLevel.R0)
        reasons = [f"[R-FAMILY] family {intent.family.value} mặc định {level.value}"]
        classes: list[str] = []

        if intent.family is TaskFamily.DATABASE and _WRITE_RX.search(text):
            level = RiskLevel.max(level, RiskLevel.R2)
            reasons.append("[R-DB-WRITE] thao tác ghi cơ sở dữ liệu => R2")
        if _PROD_RX.search(text) and _WRITE_RX.search(text):
            level = RiskLevel.max(level, RiskLevel.R2)
            reasons.append("[R-PROD-WRITE] ghi vào môi trường thật/production => R2")

        pii = detect_pii(event.text or "")
        if pii:
            classes += pii
            reasons.append(f"[R-PII] phát hiện dữ liệu cá nhân: {', '.join(pii)}")

        hits = find_dangerous(text)
        if hits:
            level = RiskLevel.R3
            for rid, desc in hits:
                classes.append("deny." + rid.removeprefix("D-").lower())
                reasons.append(f"[{rid}] {desc} => R3, từ chối")

        inj = find_injection(text)
        injection = bool(inj) and event.untrusted
        if injection:
            level = RiskLevel.max(level, RiskLevel.R2)
            reasons.append(f"[R-INJECTION] nghi ngờ prompt injection ({', '.join(inj)}) trong nội dung không tin cậy => tối thiểu R2")

        return RiskAssessment(
            level=level,
            reasons=reasons,
            requires_approval=level >= RiskLevel.R2,
            pii_detected=bool(pii),
            data_classes=sorted(set(classes)),
            injection_suspected=injection,
        )

    def assess_action(self, action: TypedAction, spec: ActionSpec) -> RiskAssessment:
        level = spec.risk
        reasons = [f"[R-SPEC] ActionSpec {spec.name} khai báo {spec.risk.value}"]
        classes: list[str] = []
        if spec.external:
            level = RiskLevel.max(level, RiskLevel.R2)
            reasons.append("[R-EXTERNAL] tác động ra ngoài hệ thống => tối thiểu R2")
        if not spec.reversible and level >= RiskLevel.R1:
            level = RiskLevel.max(level, RiskLevel.R2)
            reasons.append("[R-IRREVERSIBLE] không thể hoàn tác => tối thiểu R2")
        hits = find_dangerous(fold(json.dumps(action.args, ensure_ascii=False, default=str)))
        for rid, desc in hits:
            level = RiskLevel.R3
            classes.append("deny." + rid.removeprefix("D-").lower())
            reasons.append(f"[{rid}] {desc} trong tham số => R3, từ chối")
        return RiskAssessment(level=level, reasons=reasons, requires_approval=level >= RiskLevel.R2, data_classes=sorted(set(classes)))
