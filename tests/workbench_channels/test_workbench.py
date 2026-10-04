from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from tests.workbench_channels.conftest import PASSWORD, World, login
from zeus.contracts.api import Paths
from zeus.contracts.models import ApprovalStatus, Channel, EventKind, TaskStatus
from zeus.workbench import views
from zeus.workbench.control_client import ControlApiClient
from zeus.workbench.security import AuthConfig, hash_password, verify_password

ROOT = Path(__file__).resolve().parents[2]
PAGES = ["", "/command", "/workflows", "/approvals", "/evidence", "/agents", "/workers", "/costs", "/errors", "/learning", "/audit", "/brain", "/ledger"]


# ------------------------------------------------------------------ đăng nhập / phiên


def test_requires_login_everywhere(world: World):
    for p in PAGES:
        r = world.client.get(f"/wb{p}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/wb/login", p


def test_login_fails_closed_without_password_hash(world: World):
    world.ctx.auth.password_hash = None
    r = login(world)
    assert r.status_code == 503
    assert "Chưa cấu hình mật khẩu" in r.text


def test_login_wrong_password_and_lockout(world: World):
    for _ in range(5):
        assert login(world, "sai").status_code == 401
    assert login(world, PASSWORD).status_code == 429  # bị khoá sau 5 lần sai, kể cả khi đúng


def test_login_ok_then_logout(world: World):
    assert login(world).status_code == 303
    assert world.client.get("/wb").status_code == 200
    token = world.csrf()
    assert world.client.post("/wb/logout", data={"csrf_token": token}, follow_redirects=False).status_code == 303
    assert world.client.get("/wb", follow_redirects=False).status_code == 303


def test_password_hash_roundtrip():
    h = hash_password("abc", iterations=1000)
    assert verify_password("abc", h) and not verify_password("abd", h)
    assert not verify_password("abc", None) and not verify_password("abc", "rác") and not verify_password("abc", "md5$1$a$b")


def test_session_cookie_tamper_and_expiry():
    a = AuthConfig(password_hash=None, session_key=b"s" * 32)
    c = a.make_auth_cookie("sid1", "operator", now=1000)
    assert a.read_auth_cookie(c, "sid1", now=1001) == "operator"
    assert a.read_auth_cookie(c, "sid2", now=1001) is None  # gắn với sid
    assert a.read_auth_cookie(c, "sid1", now=1000 + a.session_ttl_s + 1) is None
    assert a.read_auth_cookie(c[:-2] + "00", "sid1", now=1001) is None
    assert AuthConfig(session_key=b"x" * 32).read_auth_cookie(c, "sid1", now=1001) is None  # khoá khác


# ------------------------------------------------------------------ các trang


@pytest.mark.parametrize("path", PAGES)
def test_pages_return_200_in_vietnamese(authed: World, path: str):
    r = authed.client.get(f"/wb{path}")
    assert r.status_code == 200
    assert '<html lang="vi">' in r.text
    assert "Tổng quan" in r.text  # menu tiếng Việt
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"


def test_overview_numbers(authed: World):
    t = authed.client.get("/wb").text
    assert "Chờ duyệt" in t and "Chi phí hôm nay" in t and "$0.4400" in t  # 0.12 + 0.30 + 0.02 + 0
    assert "3/4" in t  # 3 worker còn heartbeat, w-stale mất tín hiệu


def test_workers_page_flags_stale(authed: World):
    t = authed.client.get("/wb/workers").text
    assert "w-code-1" in t and "Mất tín hiệu" in t


def test_workflow_detail_draws_svg_dag(authed: World):
    t = authed.client.get(f"/wb/workflows/{authed.ids['running']}").text
    assert "<svg" in t and t.count('class="edge"') == 5 and t.count('class="node ') == 5
    assert "Deploy staging" in t


def test_workflow_unknown_404_and_status_filter(authed: World):
    assert authed.client.get("/wb/workflows/tsk_none").status_code == 404
    t = authed.client.get("/wb/workflows?status=FAILED").text
    assert "Migrate bảng đơn hàng" in t and "Đổi banner" not in t


def test_agents_and_learning_use_cost_per_verified_success(authed: World):
    t = authed.client.get("/wb/agents").text
    assert "sonnet-5-5" in t and "$0.1200" in t
    assert "chưa đủ dữ liệu" in authed.client.get("/wb/learning").text  # mẫu < ngưỡng champion


def test_brain_search_and_ledger(authed: World):
    import asyncio

    t = authed.client.get("/wb/brain?q=Workbench").text
    assert "ADR-006" in t and "&lt;b&gt;offline&lt;/b&gt;" in t  # nội dung bộ nhớ được escape
    authed.ctx.ledger_path = ROOT / "docs/state/DECISION_LEDGER.md"
    l = authed.client.get("/wb/ledger").text
    assert "ADR-001" in l and "ADR-006" in l
    _ = asyncio


def test_evidence_detail(authed: World):
    t = authed.client.get(f"/wb/evidence/{authed.ids['ok']}").text
    assert "pytest 12 passed" in t and "Đã xác minh: đạt" in t


# ------------------------------------------------------------------ escape đầu ra


def test_output_is_escaped_everywhere(authed: World):
    for p in ("/workflows", "/evidence", "/", "/approvals", f"/workflows/{authed.ids['xss']}"):
        t = authed.client.get(f"/wb{p}").text
        assert "<script>alert" not in t and "<img src=x" not in t, p
    assert "&lt;script&gt;alert" in authed.client.get("/wb/workflows").text
    assert "&lt;img src=x onerror" in authed.client.get("/wb/approvals").text


def test_reflected_query_is_escaped(authed: World):
    t = authed.client.get("/wb/brain", params={"q": '"><script>alert(1)</script>'}).text
    assert "<script>alert(1)" not in t and "&lt;script&gt;" in t


# ------------------------------------------------------------------ CSRF


def test_csrf_required_for_every_post(authed: World):
    c = authed.client
    aid = authed.ids["approval"]
    assert c.post("/wb/command", data={"text": "x"}).status_code == 403
    assert c.post("/wb/command", data={"text": "x", "csrf_token": "sai"}).status_code == 403
    assert c.post(f"/wb/approvals/{aid}/decision", data={"decision": "approve"}).status_code == 403
    assert c.post("/wb/logout", data={}).status_code == 403
    # token của phiên khác không dùng được
    other = AuthConfig(session_key=b"k" * 32).csrf_token("sid-khac")
    assert c.post("/wb/command", data={"text": "x", "csrf_token": other}).status_code == 403
    assert authed.ctx.demo["ingested"] == []  # type: ignore[attr-defined]


def test_csrf_blocks_cross_origin_even_with_valid_token(authed: World):
    t = authed.csrf()
    r = authed.client.post("/wb/command", data={"text": "x", "csrf_token": t}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_login_post_needs_csrf(world: World):
    world.client.get("/wb/login")
    r = world.client.post("/wb/login", data={"username": "operator", "password": PASSWORD})
    assert r.status_code == 403
    r = world.client.post("/wb/login", data={"username": "operator", "password": PASSWORD, "csrf_token": ""})
    assert r.status_code == 403


def test_post_without_session_cookie_forbidden(world: World):
    assert world.client.post("/wb/login", data={"username": "operator", "password": PASSWORD, "csrf_token": "x"}).status_code == 403


# ------------------------------------------------------------------ hành động


def test_command_ingests_untrusted_free_workbench_event(authed: World):
    r = authed.client.post("/wb/command", data={"text": "Sửa lỗi in hoá đơn", "family": "erp_bug", "csrf_token": authed.csrf()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/wb/command?sent=tsk_demo_new"  # Post/Redirect/Get (UX-03)
    assert "tsk_demo_new" in authed.client.get(r.headers["location"]).text
    (ev,) = authed.ctx.demo["ingested"]  # type: ignore[attr-defined]
    assert ev.channel is Channel.WORKBENCH and ev.kind is EventKind.COMMAND and ev.text == "Sửa lỗi in hoá đơn"
    assert ev.metadata["requested_family"] == "erp_bug" and ev.sender.channel_user_id == "operator"


def test_command_validation(authed: World):
    t = authed.csrf()
    assert authed.client.post("/wb/command", data={"text": "  ", "csrf_token": t}).status_code == 400
    assert authed.client.post("/wb/command", data={"text": "x" * 4001, "csrf_token": t}).status_code == 400
    assert authed.client.post("/wb/command", data={"text": "x", "family": "nope", "csrf_token": t}).status_code == 400
    assert authed.ctx.demo["ingested"] == []  # type: ignore[attr-defined]


async def test_approve_flow_records_decider(authed: World):
    aid = authed.ids["approval"]
    r = authed.client.post(f"/wb/approvals/{aid}/decision", data={"decision": "approve", "comment": "ok", "csrf_token": authed.csrf("/wb/approvals")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=approved")
    a = await authed.ctx.demo["approvals"].get(aid)  # type: ignore[attr-defined]
    assert a.status is ApprovalStatus.APPROVED and a.decided_by == "human:operator" and a.comment == "ok"
    # quyết định lần 2 không ghi đè
    r = authed.client.post(f"/wb/approvals/{aid}/decision", data={"decision": "reject", "comment": "muộn", "csrf_token": authed.csrf("/wb/approvals")}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=error")
    assert (await authed.ctx.demo["approvals"].get(aid)).status is ApprovalStatus.APPROVED  # type: ignore[attr-defined]


async def test_reject_requires_reason(authed: World):
    aid = authed.ids["approval"]
    t = authed.csrf("/wb/approvals")
    assert authed.client.post(f"/wb/approvals/{aid}/decision", data={"decision": "reject", "comment": " ", "csrf_token": t}).status_code == 400
    r = authed.client.post(f"/wb/approvals/{aid}/decision", data={"decision": "reject", "comment": "Không đúng khách", "csrf_token": t}, follow_redirects=False)
    assert r.status_code == 303
    a = await authed.ctx.demo["approvals"].get(aid)  # type: ignore[attr-defined]
    assert a.status is ApprovalStatus.REJECTED and a.comment == "Không đúng khách"


def test_decision_requires_login(world: World):
    world.client.get("/wb/login")
    t = world.csrf("/wb/login")
    r = world.client.post(f"/wb/approvals/{world.ids['approval']}/decision", data={"decision": "approve", "csrf_token": t}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/wb/login"


# ------------------------------------------------------------------ tài sản tĩnh / không CDN


def test_static_assets_served_and_whitelisted(world: World):
    assert world.client.get("/wb/static/app.css").status_code == 200
    assert "javascript" in world.client.get("/wb/static/app.js").headers["content-type"]
    for bad in ("../router.py", "..%2frouter.py", "app.py"):
        assert world.client.get(f"/wb/static/{bad}").status_code == 404


def test_no_external_resources_in_templates_and_static():
    base = ROOT / "zeus/workbench"
    pat = re.compile(r"""(?:src|href|action)\s*=\s*["']\s*(?:https?:)?//|@import|url\(\s*["']?https?:|\bfetch\(["']https?:""", re.I)
    files = [*base.glob("templates/*.html"), *base.glob("static/*")]
    assert len(files) >= 17
    assert [str(f) for f in files if pat.search(f.read_text(encoding="utf-8"))] == []
    assert not any("<script>" in f.read_text(encoding="utf-8") for f in base.glob("templates/*.html")), "không inline script (CSP)"


def test_responsive_and_theme_hooks_present(world: World):
    css = (ROOT / "zeus/workbench/static/app.css").read_text(encoding="utf-8")
    assert "@media (max-width" in css and "prefers-color-scheme:dark" in css and 'data-theme="dark"' in css
    assert 'name="viewport"' in world.client.get("/wb/login").text


# ------------------------------------------------------------------ views thuần + client


def test_dag_layout_layers():
    from zeus.contracts.models import TaskGraph, TaskNode

    g = TaskGraph(task_id="t", nodes=[TaskNode(node_id="a", title="A"), TaskNode(node_id="b", title="B", depends_on=["a"]), TaskNode(node_id="c", title="C", depends_on=["a"]), TaskNode(node_id="d", title="D", depends_on=["b", "c"])])
    lay = views.layout_dag(g)
    x = {n["id"]: n["x"] for n in lay["nodes"]}
    y = {n["id"]: n["y"] for n in lay["nodes"]}
    assert x["a"] < x["b"] == x["c"] < x["d"] and y["b"] != y["c"] and len(lay["edges"]) == 4


def test_champion_requires_min_samples():
    from zeus.contracts.models import ProviderKind, RouterStat, TaskFamily

    mk = lambda m, n, ok, cost: RouterStat(task_family=TaskFamily.BACKEND, provider=ProviderKind.FAKE, model=m, n=n, verified_success=ok, total_cost_usd=cost)  # noqa: E731
    rows = views.champions([mk("cheap", 10, 5, 1.0), mk("pricey", 10, 9, 9.0), mk("new", 2, 2, 0.01)])
    roles = {r["stat"].model: r["role"] for r in rows}  # type: ignore[attr-defined]
    assert roles == {"cheap": "champion", "pricey": "challenger", "new": "challenger"}


def test_ledger_parser_on_real_file():
    items = views.parse_ledger(ROOT / "docs/state/DECISION_LEDGER.md")
    assert items[0]["id"] == "ADR-001" and all(i["status"] for i in items[:5])


async def test_control_api_client_with_mock_transport():
    from zeus.contracts.models import ApprovalDecision, Event, Task, TaskFamily
    from zeus.contracts.api import EventIngestResponse

    t = Task(family=TaskFamily.GENERAL, goal="g")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        p = req.url.path
        if p == Paths.TASKS:
            return httpx.Response(200, json={"items": [{"task_id": t.task_id, "tenant_id": t.tenant_id, "family": "general", "goal": "g", "risk": "R0", "status": "RUNNING", "created_at": t.created_at.isoformat()}]})
        if p == Paths.EVENTS:
            return httpx.Response(200, json=EventIngestResponse(event_id="e", accepted=True).model_dump(mode="json"))
        if p == Paths.TASK.format(task_id="none"):
            return httpx.Response(404)
        if p == Paths.WORKERS:
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    c = ControlApiClient(httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://control"))
    (task,) = await c.list_tasks("zeusvn", TaskStatus.RUNNING)
    assert task.goal == "g" and task.status is TaskStatus.RUNNING
    assert seen[0].headers["x-zeus-tenant"] == "zeusvn" and seen[0].url.params["status"] == "RUNNING"
    assert (await c.ingest(Event(channel=Channel.WORKBENCH))).accepted
    assert await c.get_task("zeusvn", "none") is None and await c.list_workers() == []
    _ = ApprovalDecision


def test_real_uvicorn_login_flow():
    """Chạy app thật bằng uvicorn (socket thật, cookie thật), đăng nhập qua form + CSRF, rồi mở vài trang."""
    import socket
    import threading
    import time

    import uvicorn

    from zeus.workbench.demo import build_demo_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_demo_app("pw-that-su"), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{port}/wb"
        with httpx.Client(timeout=5) as c:
            page = None
            for _ in range(100):
                try:
                    page = c.get(base + "/login")
                    break
                except httpx.HTTPError:
                    time.sleep(0.05)
            assert page is not None
            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
            assert c.post(base + "/login", data={"username": "operator", "password": "sai", "csrf_token": token}).status_code == 401
            r = c.post(base + "/login", data={"username": "operator", "password": "pw-that-su", "csrf_token": token}, follow_redirects=True)
            assert r.status_code == 200 and "Tổng quan" in r.text
            assert "httponly" in r.history[0].headers.get_list("set-cookie")[0].lower()
            for p in ("/approvals", "/workers", "/costs"):
                assert c.get(base + p).status_code == 200
    finally:
        server.should_exit = True
        th.join(5)
