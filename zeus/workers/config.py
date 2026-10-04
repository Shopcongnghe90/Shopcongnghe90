"""Nạp config/workers.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "workers.yaml"


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: str = "deterministic-1"
    weights: dict[str, float]
    default_zones: list[str] = Field(default_factory=lambda: ["core", "agent", "ops", "erp_test"])
    zone_rules: dict[str, list[str]] = Field(default_factory=dict)


class WorkersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heartbeat_interval_s: int = 15
    poll_wait_s: int = 20
    stale_ttl_s: int = 60
    lease_ttl_s: int = 60
    max_attempts: int = 3
    artifact_max_bytes: int = 50 * 1024 * 1024
    scheduler: SchedulerConfig


def load_config(path: str | Path | None = None) -> WorkersConfig:
    p = Path(path) if path else DEFAULT_PATH
    return WorkersConfig.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
