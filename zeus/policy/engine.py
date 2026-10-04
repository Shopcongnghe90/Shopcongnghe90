"""Policy Engine: thuần, deterministic, đọc config/policy.yaml. Mọi quyết định có rule_ids.

Thứ tự đánh giá: deny-list -> tenant -> mẫu nguy hiểm trong args -> budget task -> rủi ro (R2/R3 cần duyệt) -> allow.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from zeus.contracts.models import (
    ActionSpec,
    PolicyDecision,
    PolicyEffect,
    RiskLevel,
    Task,
    TypedAction,
    ZeusModel,
)
from zeus.intent.text import fold
from zeus.risk.patterns import find_dangerous


class TenantPolicy(ZeusModel):
    allow_cloud_llm: bool = False
    allow_actions: list[str] = Field(default_factory=list)


class PolicyConfig(ZeusModel):
    version: str = "0"
    approval_min: RiskLevel = RiskLevel.R2
    external_min: RiskLevel = RiskLevel.R2
    approval_timeout_s: int = 3600
    required_approvers: dict[str, int] = Field(default_factory=lambda: {"R2": 1, "R3": 1})
    deny_actions: list[str] = Field(default_factory=list)
    per_task_usd: float | None = None
    per_day_usd: float | None = None
    tenants: dict[str, TenantPolicy] = Field(default_factory=dict)
    # Task có nguồn gốc untrusted (khách ngoài kênh) chỉ được tự chạy các action đọc thuần này; còn lại cần duyệt.
    untrusted_auto_actions: list[str] = Field(default_factory=lambda: ["*.read", "*.get", "*.list", "*.search", "*.check"])

    @model_validator(mode="after")
    def _single_approver_only(self) -> "PolicyConfig":
        # Kiểm soát "2 người duyệt" CHƯA được thực thi (một APPROVED là đủ) => từ chối cấu hình thay vì chấp nhận âm thầm.
        bad = {k: v for k, v in self.required_approvers.items() if int(v) > 1}
        if bad:
            raise ValueError(f"required_approvers > 1 chưa được hỗ trợ ({bad}); đặt 1 hoặc triển khai đa người duyệt trước")
        return self

    def tenant(self, tenant_id: str) -> TenantPolicy:
        return self.tenants.get(tenant_id) or self.tenants.get("default") or TenantPolicy()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PolicyConfig":
        risk = raw.get("risk", {})
        appr = raw.get("approval", {})
        bud = raw.get("budget", {})
        return cls(
            version=str(raw.get("version", "0")),
            approval_min=RiskLevel(risk.get("approval_min", "R2")),
            external_min=RiskLevel(risk.get("external_min", "R2")),
            approval_timeout_s=int(appr.get("timeout_s", 3600)),
            required_approvers=dict(appr.get("required_approvers", {"R2": 1, "R3": 1})),
            deny_actions=list(raw.get("deny_actions", [])),
            **({"untrusted_auto_actions": list(raw["untrusted"]["auto_actions"])} if (raw.get("untrusted") or {}).get("auto_actions") is not None else {}),
            per_task_usd=bud.get("per_task_usd"),
            per_day_usd=bud.get("per_day_usd"),
            tenants={k: TenantPolicy(**v) for k, v in (raw.get("tenants") or {}).items()},
        )

    @classmethod
    def load(cls, path: str | Path = "config/policy.yaml") -> "PolicyConfig":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})


def effective_risk(spec: ActionSpec, cfg: PolicyConfig) -> RiskLevel:
    level = spec.risk
    if spec.external:
        level = RiskLevel.max(level, cfg.external_min)
    return level


class DefaultPolicyEngine:
    def __init__(self, config: PolicyConfig, task_spent_usd: Callable[[str], float] | None = None) -> None:
        self.config = config
        self._task_spent = task_spent_usd

    def evaluate(self, action: TypedAction, spec: ActionSpec, task: Task | None = None) -> PolicyDecision:
        cfg = self.config
        tp = cfg.tenant(action.tenant_id)
        base: dict[str, Any] = {"redact_pii": True, "allow_cloud": tp.allow_cloud_llm}

        def deny(reason: str, rule: str) -> PolicyDecision:
            return PolicyDecision(effect=PolicyEffect.DENY, reasons=[reason], rule_ids=[rule], **base)

        if any(fnmatch.fnmatchcase(action.name, g) for g in cfg.deny_actions):
            return deny(f"action {action.name} nằm trong deny-list", "P-DENYLIST")
        if task is not None and task.tenant_id != action.tenant_id:
            return deny("tenant của action khác tenant của task", "P-TENANT-MISMATCH")
        if not any(fnmatch.fnmatchcase(action.name, g) for g in tp.allow_actions):
            return deny(f"tenant {action.tenant_id} không được phép dùng {action.name}", "P-TENANT-ALLOWLIST")
        hits = find_dangerous(fold(json.dumps(action.args, ensure_ascii=False, default=str)))
        if hits:
            return deny("tham số chứa mẫu nguy hiểm: " + "; ".join(d for _, d in hits), "P-DANGEROUS-ARGS")
        remaining: float | None = None
        if task is not None and task.budget_usd is not None and self._task_spent is not None:
            remaining = task.budget_usd - self._task_spent(task.task_id)
            base["budget_remaining_usd"] = remaining
            if remaining <= 0:
                return deny("task đã hết ngân sách", "P-BUDGET-TASK")
        level = effective_risk(spec, cfg)
        if level >= cfg.approval_min and not action.dry_run:
            n = int(cfg.required_approvers.get(level.value, 1))
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reasons=[f"rủi ro {level.value} cần người duyệt"],
                rule_ids=[f"P-APPROVAL-{level.value}"],
                required_approvers=n,
                **base,
            )
        if task is not None and task.untrusted and not action.dry_run and not any(fnmatch.fnmatchcase(action.name, g) for g in cfg.untrusted_auto_actions):
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reasons=["task từ nguồn không tin cậy: action có tác dụng cần người duyệt"],
                rule_ids=["P-UNTRUSTED-ORIGIN"],
                required_approvers=1,
                **base,
            )
        return PolicyDecision(effect=PolicyEffect.ALLOW, reasons=[f"rủi ro {level.value}"], rule_ids=[f"P-ALLOW-{level.value}"], **base)
