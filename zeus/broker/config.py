"""Nạp config/models.yaml: bảng model, giá, năng lực, prior vai trò, trọng số router."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from zeus.contracts.models import ModelRole, ProviderCapability, ProviderKind, TaskFamily, ZeusModel

UNVERIFIED_MODEL_ID = "CAN_DIEN"


class ModelSpec(ZeusModel):
    id: str
    provider: ProviderKind
    roles: list[ModelRole] = Field(default_factory=list)
    verified_model_id: bool = False
    local: bool = False
    context_window: int = 8000
    max_output_tokens: int = 4096
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    latency_ms: int = 3000
    supports: dict[str, bool] = Field(default_factory=dict)
    quality: dict[str, float] = Field(default_factory=dict)

    @property
    def routable(self) -> bool:
        """Model id 'CAN_DIEN' / chưa kiểm chứng => không bao giờ được route."""
        return self.verified_model_id and self.id != UNVERIFIED_MODEL_ID

    def capability(self, available: bool = True) -> ProviderCapability:
        s = self.supports
        return ProviderCapability(
            provider=self.provider,
            model=self.id,
            local=self.local,
            context_window=self.context_window,
            max_output_tokens=self.max_output_tokens,
            supports_tools=s.get("tools", False),
            supports_vision=s.get("vision", False),
            supports_json=s.get("json", False),
            price_in_per_mtok=self.price_in_per_mtok,
            price_out_per_mtok=self.price_out_per_mtok,
            roles=self.roles,
            available=available and self.routable,
            verified_model_id=self.routable,
        )


class ProviderConf(ZeusModel):
    kind: ProviderKind
    enabled: bool = False
    key_env: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None
    model_env: str | None = None


class FamilyPrior(ZeusModel):
    role: ModelRole
    kind: str  # coding | planning | reasoning | vision | classify


class ModelsConfig(ZeusModel):
    version: str
    pricing_ref: str
    policy_version: str = "bootstrap-1"
    providers: dict[str, ProviderConf]
    models: list[ModelSpec]
    family_priors: dict[str, FamilyPrior]
    weights: dict[str, float]

    def prior(self, family: TaskFamily) -> FamilyPrior:
        return self.family_priors.get(family.value) or self.family_priors["default"]

    def enabled_models(self) -> list[ModelSpec]:
        on = {p.kind for p in self.providers.values() if p.enabled}
        return [m for m in self.models if m.provider in on]

    def with_local_model(self, name: str | None) -> "ModelsConfig":
        """Thay id model local bằng tên model thực của llama.cpp server (ZEUS_LOCAL_LLM_MODEL)."""
        if not name:
            return self
        return self.model_copy(update={"models": [m.model_copy(update={"id": name}) if m.provider is ProviderKind.LOCAL else m for m in self.models]})

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelsConfig":
        return cls(
            version=str(raw["version"]),
            pricing_ref=str(raw.get("pricing_ref", f"models.yaml@{raw['version']}")),
            policy_version=str(raw.get("policy_version", "bootstrap-1")),
            providers={k: ProviderConf(**v) for k, v in raw["providers"].items()},
            models=[ModelSpec(**m) for m in raw["models"]],
            family_priors={k: FamilyPrior(**v) for k, v in raw["family_priors"].items()},
            weights={k: float(v) for k, v in raw["weights"].items()},
        )

    @classmethod
    def load(cls, path: str | Path = "config/models.yaml") -> "ModelsConfig":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
