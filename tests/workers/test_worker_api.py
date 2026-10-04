from __future__ import annotations

import hashlib

import httpx
import pytest

from tests.workers.conftest import make_info
from zeus.contracts.api import HEADER_WORKER_TOKEN, AssignmentResult, Paths, WorkerRegisterRequest
from zeus.contracts.models import ActionResult, TypedAction, WorkerHeartbeat
from zeus_worker.client import WorkerApiClient

pytestmark = pytest.mark.pg


def http(stack, token: str | None = None) -> httpx.AsyncClient:
    h = {HEADER_WORKER_TOKEN: token} if token else {}
    return httpx.AsyncClient(transport=stack.transport(), base_url="http://zeus", headers=h)


async def test_auth_and_scope_enforced(stack):
    t1 = await stack.registry.issue_token("w1", ["zeusvn"])
    async with http(stack) as anon, http(stack, "zwt_bogus") as bogus, http(stack, t1) as c1:
        body = WorkerRegisterRequest(info=make_info("w1")).model_dump(mode="json")
        assert (await anon.post(Paths.WORKER_REGISTER, json=body)).status_code == 401
        assert (await bogus.post(Paths.WORKER_REGISTER, json=body)).status_code == 401
        # token của w1 không được đăng ký/heartbeat/poll hộ w2
        other = WorkerRegisterRequest(info=make_info("w2")).model_dump(mode="json")
        assert (await c1.post(Paths.WORKER_REGISTER, json=other)).status_code == 403
        # tenant_scope vượt quyền token
        wide = WorkerRegisterRequest(info=make_info("w1", tenant_scope=["zeusvn", "acme"])).model_dump(mode="json")
        assert (await c1.post(Paths.WORKER_REGISTER, json=wide)).status_code == 403
        assert (await c1.post(Paths.WORKER_REGISTER, json=body)).status_code == 200
        assert (await c1.post(Paths.WORKER_HEARTBEAT, json=WorkerHeartbeat(worker_id="w2").model_dump(mode="json"))).status_code == 403
        assert (await c1.post(Paths.WORKER_POLL, json={"worker_id": "w2", "wait_s": 0})).status_code == 403
        await stack.registry.revoke_tokens("w1")
        assert (await c1.post(Paths.WORKER_POLL, json={"worker_id": "w1", "wait_s": 0})).status_code == 401


async def _two_workers(stack):
    clients = {}
    for w in ("w1", "w2"):
        tok = await stack.registry.issue_token(w)
        clients[w] = WorkerApiClient("http://zeus", tok, transport=stack.transport())
        await clients[w].register(make_info(w))
    return clients


async def _dispatch(stack, name="noop.echo", **args):
    from zeus.contracts.models import Task, TaskFamily, TaskNode
    from zeus.workers.dispatch import DispatchRequest

    task = Task(family=TaskFamily.BACKEND, goal="g")
    node = TaskNode(node_id="n1", title="t", required_capabilities=["python"])
    act = TypedAction(name=name, args=args, task_id=task.task_id)
    return await stack.dispatcher.dispatch(DispatchRequest(task=task, node=node, action=act), b"TOKEN")


async def test_result_ownership_idempotency_and_retry(stack):
    cl = await _two_workers(stack)
    await cl["w1"].heartbeat(WorkerHeartbeat(worker_id="w1"))
    a = await _dispatch(stack)
    owner = (await stack.queue.status(a.assignment_id))["worker_id"]
    other = "w2" if owner == "w1" else "w1"
    got = await cl[owner].poll(__import__("zeus.contracts.api", fromlist=["PollRequest"]).PollRequest(worker_id=owner, wait_s=0))
    assert [x.assignment_id for x in got.assignments] == [a.assignment_id]
    res = lambda w: AssignmentResult(assignment_id=a.assignment_id, worker_id=w, result=ActionResult(action_id=a.action.action_id, ok=True, output={"v": 1}))
    # worker khác (token hợp lệ) không được báo kết quả hộ
    with pytest.raises(httpx.HTTPStatusError) as e:
        await cl[other].post_result(res(other))
    assert e.value.response.status_code == 403
    # completer lỗi lần đầu => 502, worker gửi lại thành công, activity chỉ hoàn thành 1 lần
    stack.completer.fail_next = True
    with pytest.raises(httpx.HTTPStatusError) as e:
        await cl[owner].post_result(res(owner))
    assert e.value.response.status_code == 502 and stack.completer.calls == []
    assert (await cl[owner].post_result(res(owner))).ok
    assert len(stack.completer.calls) == 1
    token, result = stack.completer.calls[0]
    assert token == b"TOKEN" and result.ok and result.executed_by == owner
    again = await cl[owner].post_result(res(owner))
    assert again.detail == "đã nhận trước đó" and len(stack.completer.calls) == 1
    for c in cl.values():
        await c.aclose()


async def test_heartbeat_carries_cancel_and_lease_extension(stack):
    from zeus.contracts.api import PollRequest

    cl = await _two_workers(stack)
    a = await _dispatch(stack)
    owner = (await stack.queue.status(a.assignment_id))["worker_id"]
    await cl[owner].poll(PollRequest(worker_id=owner, wait_s=0))
    r = await cl[owner].heartbeat(WorkerHeartbeat(worker_id=owner, running_assignment_ids=[a.assignment_id]))
    assert r.cancel_assignment_ids == []
    assert await stack.dispatcher.cancel(a.assignment_id) == "CANCEL_REQUESTED"
    r = await cl[owner].heartbeat(WorkerHeartbeat(worker_id=owner, running_assignment_ids=[a.assignment_id]))
    assert r.cancel_assignment_ids == [a.assignment_id]
    from zeus.contracts.api import CancelAck

    await cl[owner].cancel_ack(CancelAck(assignment_id=a.assignment_id, worker_id=owner, cancelled=True))
    assert len(stack.completer.calls) == 1 and stack.completer.calls[0][1].ok is False
    assert stack.completer.calls[0][1].error == "cancelled" or "cancel" in stack.completer.calls[0][1].error
    for c in cl.values():
        await c.aclose()


async def test_cancel_before_lease_completes_activity_immediately(stack):
    cl = await _two_workers(stack)
    a = await _dispatch(stack)
    assert await stack.dispatcher.cancel(a.assignment_id) == "CANCELLED"
    assert len(stack.completer.calls) == 1 and stack.completer.calls[0][1].ok is False
    from zeus.contracts.api import PollRequest

    for w, c in cl.items():
        assert (await c.poll(PollRequest(worker_id=w, wait_s=0))).assignments == []
        await c.aclose()


async def test_artifact_upload_content_addressed_and_owner_only(stack):
    from zeus.contracts.api import PollRequest

    cl = await _two_workers(stack)
    a = await _dispatch(stack)
    owner = (await stack.queue.status(a.assignment_id))["worker_id"]
    other = "w2" if owner == "w1" else "w1"
    await cl[owner].poll(PollRequest(worker_id=owner, wait_s=0))
    data = b"hello log\n"
    ref = await cl[owner].upload(a.assignment_id, data, kind="log", name="../../etc/x.log", mime="text/plain")
    assert ref.sha256 == hashlib.sha256(data).hexdigest() and ref.ref == f"artifact://zeusvn/{ref.sha256}"
    assert ref.name == "x.log"  # tên bị làm sạch
    assert (stack.services.artifact_dir / "zeusvn" / ref.sha256).read_bytes() == data
    with pytest.raises(httpx.HTTPStatusError) as e:
        await cl[other].upload(a.assignment_id, data, kind="log", name="x")
    assert e.value.response.status_code == 403
    for c in cl.values():
        await c.aclose()
