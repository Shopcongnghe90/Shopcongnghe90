"""Dựng broker từ Settings + config/models.yaml + config/policy.yaml. Không có key => provider tự coi là unavailable."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from zeus.broker.broker import DefaultModelBroker, RouteSink
from zeus.broker.config import ModelsConfig
from zeus.broker.providers import AnthropicProvider, OpenAICompatibleProvider
from zeus.config import Settings
from zeus.contracts.interfaces import ModelProvider
from zeus.contracts.models import ProviderKind, RouterStat
from zeus.policy.budget import BudgetLedger, BudgetPolicy, InMemoryBudgetLedger
from zeus.policy.engine import PolicyConfig


def build_providers(
    cfg: ModelsConfig, settings: Settings, env: Mapping[str, str] | None = None, *, http_client: httpx.AsyncClient | None = None
) -> dict[ProviderKind, ModelProvider]:
    out: dict[ProviderKind, ModelProvider] = {}
    for conf in cfg.providers.values():
        if not conf.enabled:
            continue
        key = settings.provider_key(conf.kind, env)
        secret = key.get_secret_value() if key else None
        if conf.kind is ProviderKind.ANTHROPIC:
            out[conf.kind] = AnthropicProvider(secret, models=cfg.models)
        elif conf.kind is ProviderKind.OPENAI:
            out[conf.kind] = OpenAICompatibleProvider(conf.kind, settings.openai_base_url or conf.base_url, secret, models=cfg.models, client=http_client, max_tokens_field="max_completion_tokens")
        elif conf.kind is ProviderKind.GEMINI:
            out[conf.kind] = OpenAICompatibleProvider(conf.kind, settings.gemini_base_url or conf.base_url, secret, models=cfg.models, client=http_client)
        elif conf.kind is ProviderKind.LOCAL:
            out[conf.kind] = OpenAICompatibleProvider(conf.kind, settings.local_llm_url, None, models=cfg.models, client=http_client, require_key=False)
    return out


def build_broker(
    settings: Settings,
    cfg: ModelsConfig | None = None,
    policy: PolicyConfig | None = None,
    *,
    ledger: BudgetLedger | None = None,
    route_sink: RouteSink | None = None,
    stats: Sequence[RouterStat] = (),
    env: Mapping[str, str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> DefaultModelBroker:
    cfg = (cfg or ModelsConfig.load(settings.models_config)).with_local_model(settings.local_llm_model)
    policy = policy or PolicyConfig.load(settings.policy_config)
    ledger = ledger or InMemoryBudgetLedger()
    budget = BudgetPolicy(ledger, policy.per_task_usd, policy.per_day_usd)
    return DefaultModelBroker(
        cfg,
        build_providers(cfg, settings, env, http_client=http_client),
        policy=policy,
        budget=budget,
        ledger=ledger,
        route_sink=route_sink,
        stats=stats,
        cloud_enabled=settings.cloud_providers_enabled(),
    )
