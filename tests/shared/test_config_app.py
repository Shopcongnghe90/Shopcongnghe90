from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zeus.app.main import create_app
from zeus.config import Settings, parse_bool
from zeus.contracts.models import ProviderKind


def test_defaults_have_no_secrets_and_cloud_off():
    s = Settings.from_env({})
    assert s.claude_cloud_available is False
    assert s.db_dsn is None
    dumped = s.model_dump()
    assert not any("key" in k and k.endswith("_env") is False for k in dumped), dumped
    assert s.provider_key(ProviderKind.ANTHROPIC, env={}) is None
    assert s.provider_key(ProviderKind.LOCAL, env={}) is None


def test_env_parsing():
    s = Settings.from_env({
        "CLAUDE_CLOUD_AVAILABLE": "true", "ZEUS_DB_DSN": "host=x", "ZEUS_LOCAL_LLM_URL": "http://10.0.0.2:8080/v1",
        "ZEUS_ALLOW_CLOUD_LLM": "0", "ZEUS_ENV": "test",
    })
    assert s.claude_cloud_available and s.db_dsn == "host=x" and not s.allow_cloud_llm
    key = s.provider_key(ProviderKind.OPENAI, env={"OPENAI_API_KEY": "sk-test"})
    assert key is not None and key.get_secret_value() == "sk-test" and "sk-test" not in repr(key)
    with pytest.raises(ValueError):
        parse_bool("maybe")
    with pytest.raises(ValueError):
        Settings.from_env({"ZEUS_ENV": "galaxy"})


def test_healthz():
    app = create_app(Settings.from_env({}))
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["contracts_version"] == "1.0.0"
    assert body["claude_cloud_available"] is False
