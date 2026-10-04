"""Thin worker: executor typed, timeout, cancel, upload, evidence — chạy qua Worker API thật + Postgres thật."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests.workers.conftest import make_info
from zeus.contracts.models import Task, TaskFamily, TaskNode, TypedAction, utcnow
from zeus.workers.dispatch import DispatchRequest
from zeus_worker.agent import WorkerAgent
from zeus_worker.client import WorkerApiClient
from zeus_worker.config import WorkerConfig
from zeus_worker.executors import ExecutorRegistry, HttpFetch, parse_pytest_summary

pytestmark = pytest.mark.pg


async def make_agent(stack, tmp_path: Path, **cfg_kw):
    token = await stack.registry.issue_token("w-t")
    cfg = WorkerConfig(
        server_url="http://zeus", worker_id="w-t", work_dir=tmp_path, file_roots=[tmp_path], repo_roots=[tmp_path],
        http_allow_domains=["zeusvn.vn"], extra_capabilities=["python"], **cfg_kw,
    )
    client = WorkerApiClient("http://zeus", token, transport=stack.transport())
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok-body") if req.url.host == "shop.zeusvn.vn" else httpx.Response(500))
    agent = WorkerAgent(cfg, client, http_transport=transport)
    await agent.register()
    await agent.heartbeat_once()
    return agent, client


async def run_action(stack, agent, name: str, timeout_s=30, **args):
    task = Task(family=TaskFamily.BACKEND, goal="g")
    node = TaskNode(node_id="n", title="t", required_capabilities=["python"])
    a = await stack.dispatcher.dispatch(
        DispatchRequest(task=task, node=node, action=TypedAction(name=name, args=args), timeout_s=timeout_s), b"TOK"
    )
    got = await agent.poll_once(wait_s=0)
    assert [x.assignment_id for x in got] == [a.assignment_id]
    return a


async def finished(stack, agent, a):
    await asyncio.wait_for(agent.wait_idle(), 20)
    n = len(stack.completer.calls)
    return stack.completer.calls[-1][1] if n else None


async def test_inventory_and_heartbeat_from_proc(stack, tmp_path):
    agent, client = await make_agent(stack, tmp_path)
    info = await stack.registry.get("w-t")
    assert info.inventory.cpu_count >= 1 and info.inventory.ram_mb > 0 and info.inventory.disk_free_gb > 0
    names = {c.name for c in info.capabilities}
    assert {"python", "action:noop.echo", "action:http.fetch", "action:file.checksum", "action:repo.tests.run"} <= names
    assert not any("shell" == n or n == "action:shell" for n in names)  # không có executor shell tự do
    hb = await stack.registry.last_heartbeat("w-t")
    assert 0 <= hb.cpu_available_pct <= 100 and hb.ram_available_mb > 0
    await client.aclose()


async def test_noop_echo_result_log_and_evidence_upload(stack, tmp_path):
    agent, client = await make_agent(stack, tmp_path)
    a = await run_action(stack, agent, "noop.echo", msg="xin chào")
    res = await finished(stack, agent, a)
    assert res.ok and res.output == {"echo": {"msg": "xin chào"}} and res.executed_by == "w-t"
    row = await stack.queue.status(a.assignment_id)
    assert row["status"] == "COMPLETED" and row["activity_completed"]
    import psycopg

    with psycopg.connect(stack.dsn) as c:
        kinds = [r[0] for r in c.execute("SELECT kind FROM worker_artifacts WHERE assignment_id=%s", (a.assignment_id,))]
        stored = c.execute("SELECT result FROM assignment_results WHERE assignment_id=%s", (a.assignment_id,)).fetchone()[0]
    assert kinds == ["log"] and stored["log_ref"].startswith("artifact://zeusvn/")
    await client.aclose()


async def test_unknown_executor_and_dry_run(stack, tmp_path):
    agent, client = await make_agent(stack, tmp_path)
    await run_action(stack, agent, "shell.run", cmd="rm -rf /")
    res = await finished(stack, agent, None)
    assert not res.ok and "không tồn tại" in res.error
    task = Task(family=TaskFamily.BACKEND, goal="g")
    node = TaskNode(node_id="n", title="t", required_capabilities=["python"])
    await stack.dispatcher.dispatch(
        DispatchRequest(task=task, node=node, action=TypedAction(name="file.checksum", args={"path": "/etc/passwd"}, dry_run=True)), b"T2"
    )
    await agent.poll_once(wait_s=0)
    res = await finished(stack, agent, None)
    assert res.ok and res.output == {"dry_run": True}
    await client.aclose()


async def test_timeout_kills_slow_action(stack, tmp_path):
    agent, client = await make_agent(stack, tmp_path)
    a = await run_action(stack, agent, "noop.echo", timeout_s=1, delay_s=30)
    res = await finished(stack, agent, a)
    assert not res.ok and "timeout" in res.error
    await client.aclose()


async def test_cancel_via_heartbeat_acks(stack, tmp_path):
    agent, client = await make_agent(stack, tmp_path)
    a = await run_action(stack, agent, "noop.echo", delay_s=60)
    assert a.assignment_id in agent.running
    assert await stack.dispatcher.cancel(a.assignment_id) == "CANCEL_REQUESTED"
    resp = await agent.heartbeat_once()
    assert resp.cancel_assignment_ids == [a.assignment_id]
    await asyncio.wait_for(agent.wait_idle(), 10)
    assert (await stack.queue.status(a.assignment_id))["status"] == "CANCELLED"
    assert stack.completer.calls[-1][1].ok is False
    await client.aclose()


async def test_file_checksum_confined_to_roots(stack, tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    agent, client = await make_agent(stack, tmp_path)
    await run_action(stack, agent, "file.checksum", path=str(f), expected_sha256=hashlib.sha256(b"abc").hexdigest())
    res = await finished(stack, agent, None)
    assert res.ok and res.output["sha256"] == hashlib.sha256(b"abc").hexdigest()
    await run_action(stack, agent, "file.checksum", path="/etc/passwd")
    res = await finished(stack, agent, None)
    assert not res.ok and "ngoài thư mục" in res.error
    await run_action(stack, agent, "file.checksum", path=str(tmp_path / ".." / "etc" / "passwd"))
    assert not (await finished(stack, agent, None)).ok
    await client.aclose()


async def test_http_fetch_allowlist(stack, tmp_path):
    assert HttpFetch.allowed("shop.zeusvn.vn", ["zeusvn.vn"]) and HttpFetch.allowed("zeusvn.vn", ["zeusvn.vn"])
    assert not HttpFetch.allowed("evilzeusvn.vn", ["zeusvn.vn"]) and not HttpFetch.allowed("x.com", [])
    agent, client = await make_agent(stack, tmp_path)
    await run_action(stack, agent, "http.fetch", url="https://shop.zeusvn.vn/health")
    res = await finished(stack, agent, None)
    assert res.ok and res.output["status"] == 200 and len(res.evidence_ids) == 1
    await run_action(stack, agent, "http.check", url="https://169.254.169.254/latest/meta-data")
    res = await finished(stack, agent, None)
    assert not res.ok and "allowlist" in res.error
    await run_action(stack, agent, "http.fetch", url="file:///etc/passwd")
    assert not (await finished(stack, agent, None)).ok
    await client.aclose()


async def test_repo_tests_run_sends_real_test_evidence(stack, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "test_ok.py").write_text(textwrap.dedent("def test_a():\n    assert 1+1 == 2\n\ndef test_b():\n    assert True\n"))
    agent, client = await make_agent(stack, tmp_path)
    a = await run_action(stack, agent, "repo.tests.run", timeout_s=120, repo=str(repo))
    res = await finished(stack, agent, a)
    assert res.ok, res.error
    assert res.output["test_run"]["passed"] == 2 and res.output["test_run"]["exit_code"] == 0
    import psycopg

    with psycopg.connect(stack.dsn) as c:
        stored = c.execute("SELECT result FROM assignment_results WHERE assignment_id=%s", (a.assignment_id,)).fetchone()[0]
    ev = stored["evidence"][0]
    assert ev["kind"] == "test_result" and ev["passed"] is True and ev["ref"] == stored["log_ref"] and ev["sha256"]
    # test fail => ok False, vẫn có evidence
    (repo / "test_bad.py").write_text("def test_x():\n    assert False\n")
    await run_action(stack, agent, "repo.tests.run", timeout_s=120, repo=str(repo))
    res = await finished(stack, agent, None)
    assert not res.ok and res.output["test_run"]["failed"] == 1
    await run_action(stack, agent, "repo.tests.run", repo=str(repo), target="--co")
    assert "target" in (await finished(stack, agent, None)).error
    await client.aclose()


async def test_shell_allowlisted_only_named_commands(stack, tmp_path):
    agent, client = await make_agent(stack, tmp_path, shell_allowlist={"hello": [sys.executable, "-c", "print('hi')"]})
    await run_action(stack, agent, "shell.allowlisted", command="hello")
    res = await finished(stack, agent, None)
    assert res.ok and res.output["stdout"].strip() == "hi"
    await run_action(stack, agent, "shell.allowlisted", command="rm -rf /")
    assert not (await finished(stack, agent, None)).ok
    await client.aclose()


def test_zeus_worker_imports_only_contracts_and_httpx():
    """zeus_worker KHÔNG import zeus.workers, Temporal SDK hay SDK nhà cung cấp model."""
    import ast

    banned = {"temporalio", "anthropic", "openai", "psycopg", "fastapi"}
    root = Path(__file__).resolve().parents[2] / "zeus_worker"
    for p in root.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for m in mods:
                assert m.split(".")[0] not in banned, f"{p.name} import {m}"
                assert not m.startswith(("zeus.workers", "zeus.storage")), f"{p.name} import {m}"


def test_parse_pytest_summary_and_registry():
    assert parse_pytest_summary("..F\n1 failed, 2 passed, 1 skipped in 0.1s") == {"passed": 2, "failed": 1, "skipped": 1, "error": 0}
    assert "http.check" in ExecutorRegistry().names()


async def test_main_loop_end_to_end(stack, tmp_path):
    """run_forever thật: register -> poll -> thực thi -> result, rồi dừng sạch."""
    token = await stack.registry.issue_token("w-loop")
    cfg = WorkerConfig(server_url="http://zeus", worker_id="w-loop", work_dir=tmp_path, extra_capabilities=["python"], heartbeat_interval_s=0.2, poll_wait_s=1)
    client = WorkerApiClient("http://zeus", token, transport=stack.transport())
    agent = WorkerAgent(cfg, client)
    stop = asyncio.Event()
    loop_task = asyncio.create_task(agent.run_forever(stop))
    for _ in range(100):
        if await stack.registry.get("w-loop"):
            break
        await asyncio.sleep(0.05)
    task = Task(family=TaskFamily.BACKEND, goal="g")
    await stack.dispatcher.dispatch(
        DispatchRequest(task=task, node=TaskNode(node_id="n", title="t", required_capabilities=["python"]), action=TypedAction(name="noop.echo", args={"k": 1})), b"TK"
    )
    for _ in range(200):
        if stack.completer.calls:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(loop_task, 10)
    assert stack.completer.calls and stack.completer.calls[0][1].output == {"echo": {"k": 1}}
    await client.aclose()
