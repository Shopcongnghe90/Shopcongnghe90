"""Cấu hình runtime từ biến môi trường. KHÔNG có giá trị secret mặc định.

API key provider không bao giờ là field của Settings: Settings chỉ giữ TÊN biến môi trường,
key được đọc tại thời điểm gọi qua ``provider_key()`` và trả về SecretStr.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from zeus.contracts.models import ProviderKind

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    env: Literal["dev", "test", "staging", "prod"] = "dev"
    db_dsn: str | None = None
    db_pool_max: int = Field(default=10, ge=0, le=200)  # 0 = tắt pool (mỗi thao tác một kết nối); ZEUS_DB_POOL_MAX
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_cli: str = "/opt/zeus/bin/temporal"
    claude_cloud_available: bool = False
    # Tên biến môi trường chứa API key (không phải giá trị key)
    anthropic_key_env: str = "ANTHROPIC_API_KEY"
    openai_key_env: str = "OPENAI_API_KEY"
    gemini_key_env: str = "GEMINI_API_KEY"
    openai_base_url: str | None = None
    gemini_base_url: str | None = None  # endpoint OpenAI-compatible của Gemini — điền trong config
    local_llm_url: str | None = None  # vd http://10.90.30.20:8080/v1 (llama.cpp server)
    local_llm_model: str | None = None
    local_embed_url: str | None = None
    worker_token_env: str = "ZEUS_WORKER_TOKEN"
    models_config: Path = Path("config/models.yaml")
    policy_config: Path = Path("config/policy.yaml")
    brain_config: Path = Path("config/brain.yaml")
    workers_config: Path = Path("config/workers.yaml")
    channels_config: Path = Path("config/channels.yaml")
    artifact_dir: Path | None = None  # None => <data_dir>/artifacts
    # Tên biến môi trường chứa token Bearer cho /api/v1 và /internal (không phải giá trị). Bắt buộc khi env=staging/prod.
    api_token_env: str = "ZEUS_API_TOKEN"
    migrations_dir: Path = Path("migrations")
    data_dir: Path = Path("var")
    log_level: str = "INFO"
    allow_cloud_llm: bool = True  # tắt => chỉ dùng local hoặc xếp hàng
    monthly_budget_usd: float | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        kw: dict[str, object] = {}
        str_map = {
            "ZEUS_ENV": "env",
            "ZEUS_DB_DSN": "db_dsn",
            "ZEUS_DB_POOL_MAX": "db_pool_max",
            "ZEUS_TEMPORAL_ADDRESS": "temporal_address",
            "ZEUS_TEMPORAL_NAMESPACE": "temporal_namespace",
            "ZEUS_TEMPORAL_CLI": "temporal_cli",
            "ZEUS_ANTHROPIC_KEY_ENV": "anthropic_key_env",
            "ZEUS_OPENAI_KEY_ENV": "openai_key_env",
            "ZEUS_GEMINI_KEY_ENV": "gemini_key_env",
            "ZEUS_OPENAI_BASE_URL": "openai_base_url",
            "ZEUS_GEMINI_BASE_URL": "gemini_base_url",
            "ZEUS_LOCAL_LLM_URL": "local_llm_url",
            "ZEUS_LOCAL_LLM_MODEL": "local_llm_model",
            "ZEUS_LOCAL_EMBED_URL": "local_embed_url",
            "ZEUS_WORKER_TOKEN_ENV": "worker_token_env",
            "ZEUS_MODELS_CONFIG": "models_config",
            "ZEUS_POLICY_CONFIG": "policy_config",
            "ZEUS_BRAIN_CONFIG": "brain_config",
            "ZEUS_WORKERS_CONFIG": "workers_config",
            "ZEUS_CHANNELS_CONFIG": "channels_config",
            "ZEUS_ARTIFACT_DIR": "artifact_dir",
            "ZEUS_API_TOKEN_ENV": "api_token_env",
            "ZEUS_MIGRATIONS_DIR": "migrations_dir",
            "ZEUS_DATA_DIR": "data_dir",
            "ZEUS_LOG_LEVEL": "log_level",
            "ZEUS_MONTHLY_BUDGET_USD": "monthly_budget_usd",
        }
        for var, field in str_map.items():
            if e.get(var):
                kw[field] = e[var]
        kw["claude_cloud_available"] = parse_bool(e.get("CLAUDE_CLOUD_AVAILABLE"), default=False)
        kw["allow_cloud_llm"] = parse_bool(e.get("ZEUS_ALLOW_CLOUD_LLM"), default=True)
        return cls(**kw)  # type: ignore[arg-type]

    def provider_key_env(self, provider: ProviderKind) -> str | None:
        return {
            ProviderKind.ANTHROPIC: self.anthropic_key_env,
            ProviderKind.OPENAI: self.openai_key_env,
            ProviderKind.GEMINI: self.gemini_key_env,
        }.get(provider)

    def provider_key(self, provider: ProviderKind, env: Mapping[str, str] | None = None) -> SecretStr | None:
        """Đọc key tại thời điểm gọi. None => provider coi như unavailable (broker fallback)."""
        name = self.provider_key_env(provider)
        if not name:
            return None
        value = (os.environ if env is None else env).get(name)
        return SecretStr(value) if value else None

    def cloud_providers_enabled(self) -> bool:
        return self.allow_cloud_llm

    def artifacts_path(self) -> Path:
        return self.artifact_dir or (self.data_dir / "artifacts")

    def api_token(self, env: Mapping[str, str] | None = None) -> SecretStr | None:
        value = (os.environ if env is None else env).get(self.api_token_env)
        return SecretStr(value) if value else None


def get_settings() -> Settings:
    return Settings.from_env()
