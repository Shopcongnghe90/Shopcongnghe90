"""Cloud Exit / MODEL_FALLBACK: không có key cloud nào => broker + workflow vẫn hoàn thành bằng model local;
local cũng tắt => ProviderUnavailable (xếp hàng / Temporal retry), task không mất."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from tests.control_plane.conftest import FakeWorkerLoop, llm_script, make_rig, make_task, mock_openai_transport
from zeus.broker.factory import build_broker
from zeus.config import Settings
from zeus.contracts.api import TASK_QUEUE_CONTROL
from zeus.contracts.interfaces import ProviderUnavailable
from zeus.contracts.models import (
    Channel,
    ChatMessage,
    Event,
    ModelRequest,
    ModelRole,
    Outcome,
    ProviderKind,
    TaskFamily,
    TaskStatus,
    VerdictDecision,
)
from zeus.gateway.gateway import EventGateway
from zeus.intent.engine import DefaultIntentEngine
from zeus.orchestration.client import build_worker, start_task_workflow
from zeus.planning.critic import DefaultCritic
from zeus.planning.judge import DeterministicJudge
from zeus.planning.planner import DefaultPlanner

pytestmark = pytest.mark.cloud_exit_model_fallback

LOCAL_ENV = {"ZEUS_LOCAL_LLM_URL": "http://127.0.0.1:18080/v1", "ZEUS_LOCAL_LLM_MODEL": "qwen-local"}


def local_broker(models_cfg, policy_cfg, handler_state):
    """Broker dựng từ Settings thật, KHÔNG có key cloud; local llama.cpp giả bằng httpx.MockTransport."""
    calls: list = []
    base = mock_openai_transport(llm_script(), calls=calls)

    def handler(request: httpx.Request) -> httpx.Response:
        if not handler_state["up"]:
            return httpx.Response(503, json={"error": "loading model"})
        return base.handler(request)  # type: ignore[attr-defined]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = build_broker(Settings.from_env(LOCAL_ENV), models_cfg, policy_cfg, env={}, http_client=client)
    return broker, calls


async def test_broker_completes_with_local_when_no_cloud_key(models_cfg, policy_cfg):
    broker, calls = local_broker(models_cfg, policy_cfg, {"up": True})
    assert not broker.providers[ProviderKind.ANTHROPIC].available  # không key
    for role, fam in [(ModelRole.FORGE, TaskFamily.BACKEND), (ModelRole.COMMAND, TaskFamily.SECURITY), (ModelRole.RADAR, TaskFamily.VISUAL_QA)]:
        resp = await broker.complete(ModelRequest(task_family=fam, role=role, messages=[ChatMessage(role="user", content="ping")]))
        assert resp.provider is ProviderKind.LOCAL and resp.model == "qwen-local" and resp.cost.usd == 0
    assert calls and all(c["url"].startswith("http://127.0.0.1:18080/v1/") for c in calls)  # chỉ gọi local


async def test_local_down_and_cloud_off_raises_provider_unavailable_for_queueing(models_cfg, policy_cfg):
    broker, _ = local_broker(models_cfg, policy_cfg, {"up": False})
    with pytest.raises(ProviderUnavailable):
        await broker.complete(ModelRequest(messages=[ChatMessage(role="user", content="ping")]))


async def test_intent_planner_critic_judge_all_run_on_local_only(models_cfg, policy_cfg):
    broker, calls = local_broker(models_cfg, policy_cfg, {"up": True})
    rig = await make_rig(models_cfg, policy_cfg, broker=broker, judge_broker=broker)
    gw = EventGateway(rig.store, DefaultIntentEngine(broker), rig.deps.risk)
    res = await gw.ingest(Event(channel=Channel.WORKBENCH, text="giúp mình cái này với", external_id="cx-1"))
    assert res.intent.classifier == "local:qwen-local" and res.task.family is TaskFamily.BACKEND
    from zeus.contracts.models import ContextPacket

    ctx = ContextPacket(tenant_id="zeusvn", goal=res.task.goal)
    plan = await rig.deps.planner.plan(res.task, ctx)
    assert plan.planner == "local:qwen-local" and plan.route.provider is ProviderKind.LOCAL
    crit = await rig.deps.critic.critique(plan, ctx)
    assert crit.reviewer_provider is ProviderKind.LOCAL  # không còn hãng khác => ghi nhận cùng hãng
    assert any("cùng hãng" in i.message for i in crit.issues)
    assert len(calls) >= 3


async def test_taskworkflow_full_cycle_on_temporal_with_local_model_only(temporal_env, models_cfg, policy_cfg):
    """Vòng đời đầy đủ trên Temporal thật: event -> intent(LLM local) -> risk -> plan(LLM local) -> critique -> dispatch -> judge."""
    broker, calls = local_broker(models_cfg, policy_cfg, {"up": True})
    rig = await make_rig(models_cfg, policy_cfg, broker=broker, judge_broker=broker)
    queue = f"{TASK_QUEUE_CONTROL}-cx-{uuid.uuid4().hex[:6]}"
    gw = EventGateway(rig.store, DefaultIntentEngine(broker), rig.deps.risk)
    res = await gw.ingest(Event(channel=Channel.WORKBENCH, text="giúp mình cái này với", external_id="cx-2", signature_verified=True, untrusted=False))
    async with build_worker(temporal_env.client, rig.deps, queue):
        fw = FakeWorkerLoop(temporal_env.client, rig).start()
        try:
            handle = await start_task_workflow(temporal_env.client, res.task, task_queue=queue)
            verdict = await asyncio.wait_for(handle.result(), 60)
        finally:
            await fw.stop()
    assert verdict.decision is VerdictDecision.PASS and verdict.outcome is Outcome.VERIFIED_SUCCESS
    assert fw.executed == ["code.apply_patch", "test.run"]  # kế hoạch do model local lập
    rec = (await rig.evidence.list_for_task("zeusvn", res.task.task_id))[0]
    assert rec.model_provider is ProviderKind.LOCAL and rec.model_name == "qwen-local" and rec.verified_by.startswith("deterministic")
    assert (await rig.store.get_task("zeusvn", res.task.task_id)).status is TaskStatus.SUCCEEDED
    assert not any("anthropic" in c["url"] or "openai" in c["url"] for c in calls)


async def test_taskworkflow_local_outage_retries_and_task_is_not_lost(temporal_env, models_cfg, policy_cfg):
    state = {"up": False}
    broker, calls = local_broker(models_cfg, policy_cfg, state)
    rig = await make_rig(models_cfg, policy_cfg, broker=broker)
    rig.deps.planner = DefaultPlanner(broker, rig.gateway.list_actions, require_model=True)  # chế độ "bắt buộc model": hết provider => retry
    rig.deps.critic = DefaultCritic(None, rig.gateway.list_actions)
    task = make_task()
    queue = f"{TASK_QUEUE_CONTROL}-cx-{uuid.uuid4().hex[:6]}"
    async with build_worker(temporal_env.client, rig.deps, queue):
        fw = FakeWorkerLoop(temporal_env.client, rig).start()
        try:
            handle = await start_task_workflow(temporal_env.client, task, task_queue=queue, retry_initial_s=0.2, max_activity_attempts=20)
            await asyncio.sleep(1.5)
            assert (await handle.describe()).status.name == "RUNNING" and fw.seen == []  # đang xếp hàng, chưa mất
            state["up"] = True  # llama.cpp lên lại
            verdict = await asyncio.wait_for(handle.result(), 60)
        finally:
            await fw.stop()
    assert verdict.decision is VerdictDecision.PASS
    assert any(c["url"].endswith("/chat/completions") for c in calls)
