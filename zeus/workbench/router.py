"""Workbench ``/wb`` — giao diện vận hành tiếng Việt (FastAPI + Jinja2, không CDN, không build step).

Dùng: ``app.state.workbench = WorkbenchContext(data=..., ...)``; ``app.include_router(router)``.
Bảo mật: đăng nhập bắt buộc (fail closed khi chưa cấu hình mật khẩu), CSRF cho mọi POST, Jinja autoescape,
CSP chặt (chỉ 'self', không inline script), chỉ bind mạng nội bộ (cấu hình ở uvicorn --host).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from zeus.contracts.api import EventIngestResponse
from zeus.contracts.interfaces import BrainRetriever, OutcomeRecorder, WorkbenchDataSource, WorkerRegistry
from zeus.contracts.models import (
    DEFAULT_TENANT,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Channel,
    ChannelIdentity,
    Event,
    EventKind,
    EvidenceRecord,
    Outcome,
    RetrievalQuery,
    RiskLevel,
    TaskFamily,
    TaskGraph,
    TaskStatus,
    WorkerHeartbeat,
    WorkerStatus,
)
from zeus.workbench import views
from zeus.workbench.security import AUTH_COOKIE, SID_COOKIE, AuthConfig

HERE = Path(__file__).parent
PREFIX = "/wb"
Ingest = Callable[[Event], Awaitable[EventIngestResponse]]
Decide = Callable[[ApprovalDecision], Awaitable[ApprovalRequest]]
GraphProvider = Callable[[str, str], Awaitable[TaskGraph | None]]
AuditProvider = Callable[[str], Awaitable[list[dict[str, Any]]]]
VN = timezone(timedelta(hours=7))

NAV = [
    ("", "Tổng quan"), ("/command", "Ra lệnh"), ("/workflows", "Workflows"), ("/approvals", "Duyệt"), ("/evidence", "Bằng chứng"),
    ("/agents", "Agents / Models"), ("/workers", "Workers / VM"), ("/costs", "Chi phí & độ trễ"), ("/errors", "Lỗi / Retry / Rollback"),
    ("/learning", "Học tập"), ("/audit", "Kiểm toán"), ("/brain", "Project Brain"), ("/ledger", "Decision Ledger"),
]
STATUS_VI = {
    "PENDING": "Chờ", "PLANNED": "Đã lập kế hoạch", "AWAITING_APPROVAL": "Chờ duyệt", "SCHEDULED": "Đã xếp lịch", "RUNNING": "Đang chạy",
    "SUCCEEDED": "Thành công", "FAILED": "Thất bại", "CANCELLED": "Đã huỷ", "ROLLED_BACK": "Đã rollback",
    "ONLINE": "Trực tuyến", "DEGRADED": "Suy giảm", "DRAINING": "Đang rút", "OFFLINE": "Ngoại tuyến",
    "APPROVED": "Đã duyệt", "REJECTED": "Từ chối", "EXPIRED": "Hết hạn",
    "VERIFIED_SUCCESS": "Đã xác minh: đạt", "VERIFIED_FAILURE": "Đã xác minh: không đạt", "UNVERIFIED": "Chưa xác minh",
}


@dataclass
class WorkbenchContext:
    data: WorkbenchDataSource
    auth: AuthConfig = field(default_factory=AuthConfig)
    tenant_id: str = DEFAULT_TENANT
    ingest: Ingest | None = None
    decide: Decide | None = None
    registry: WorkerRegistry | None = None
    retriever: BrainRetriever | None = None
    recorder: OutcomeRecorder | None = None
    graph_provider: GraphProvider | None = None
    audit_provider: AuditProvider | None = None
    ledger_path: Path = Path("docs/state/DECISION_LEDGER.md")
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _fmt_dt(v: datetime | None) -> str:
    return v.astimezone(VN).strftime("%d/%m %H:%M:%S") if v else "—"


def _make_env() -> Environment:
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["dt"] = _fmt_dt
    env.filters["usd"] = lambda v: "—" if v is None else f"${v:,.4f}"
    env.filters["vi"] = lambda v: STATUS_VI.get(getattr(v, "value", v), str(getattr(v, "value", v)))
    env.filters["val"] = lambda v: getattr(v, "value", v)
    env.globals["nav"] = NAV
    env.globals["prefix"] = PREFIX
    return env


ENV = _make_env()
router = APIRouter(prefix=PREFIX)

# --------------------------------------------------------------------------- phiên / CSRF


@dataclass
class Sess:
    sid: str
    user: str | None
    new: bool
    csrf: str


def _ctx(request: Request) -> WorkbenchContext:
    ctx = getattr(request.app.state, "workbench", None)
    if ctx is None:
        raise HTTPException(503, "Workbench chưa được cấu hình")
    return ctx


def get_sess(request: Request) -> Sess:
    ctx = _ctx(request)
    sid = request.cookies.get(SID_COOKIE)
    valid = bool(sid) and len(sid or "") == 43
    new = not valid
    sid = sid if valid else secrets.token_urlsafe(32)
    assert sid
    user = ctx.auth.read_auth_cookie(request.cookies.get(AUTH_COOKIE), sid)
    return Sess(sid, user, new, ctx.auth.csrf_token(sid))


def require_user(request: Request, s: Sess = Depends(get_sess)) -> Sess:
    if not s.user:
        raise HTTPException(303, headers={"Location": f"{PREFIX}/login"})
    return s


async def _form(request: Request) -> dict[str, str]:
    return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc == request.headers.get("host", "")


async def csrf_form(request: Request, s: Sess = Depends(get_sess)) -> tuple[Sess, dict[str, str]]:
    """Dùng cho MỌI POST: kiểm tra token CSRF (gắn sid) + Origin cùng host. Trả (phiên, form)."""
    ctx = _ctx(request)
    f = await _form(request)
    if s.new or not ctx.auth.check_csrf(s.sid, f.get("csrf_token")) or not _same_origin(request):
        raise HTTPException(403, "CSRF token không hợp lệ")
    return s, f


async def auth_csrf_form(request: Request, sf: tuple[Sess, dict[str, str]] = Depends(csrf_form)) -> tuple[Sess, dict[str, str]]:
    if not sf[0].user:
        raise HTTPException(303, headers={"Location": f"{PREFIX}/login"})
    return sf


CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"


def _finish(request: Request, s: Sess, resp: Response) -> Response:
    ctx = _ctx(request)
    if s.new:
        resp.set_cookie(SID_COOKIE, s.sid, httponly=True, samesite="strict", secure=ctx.auth.secure_cookies, path="/")
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _render(request: Request, s: Sess, tpl: str, title: str, active: str = "", status: int = 200, **kw: Any) -> Response:
    html = ENV.get_template(tpl).render(title=title, active=active, user=s.user, csrf=s.csrf, **kw)
    return _finish(request, s, HTMLResponse(html, status_code=status))


def _redirect(request: Request, s: Sess, path: str) -> Response:
    return _finish(request, s, RedirectResponse(f"{PREFIX}{path}", status_code=303))


# --------------------------------------------------------------------------- đăng nhập


@router.get("/login")
async def login_page(request: Request, s: Sess = Depends(get_sess)) -> Response:
    if s.user:
        return _redirect(request, s, "")
    return _render(request, s, "login.html", "Đăng nhập", configured=bool(_ctx(request).auth.password_hash))


@router.post("/login")
async def login_submit(request: Request, sf: tuple[Sess, dict[str, str]] = Depends(csrf_form)) -> Response:
    s, f = sf
    auth = _ctx(request).auth
    client = request.client.host if request.client else "?"
    if not auth.password_hash:
        return _render(request, s, "login.html", "Đăng nhập", status=503, configured=False)
    if auth.locked(client):
        return _render(request, s, "login.html", "Đăng nhập", status=429, configured=True, error="Thử sai quá nhiều lần, vui lòng đợi vài phút.")
    if not auth.login(client, f.get("username", ""), f.get("password", "")):
        return _render(request, s, "login.html", "Đăng nhập", status=401, configured=True, error="Sai tên đăng nhập hoặc mật khẩu.")
    resp = _redirect(request, s, "")
    resp.set_cookie(AUTH_COOKIE, auth.make_auth_cookie(s.sid, auth.username), httponly=True, samesite="strict", secure=auth.secure_cookies, max_age=auth.session_ttl_s, path="/")
    return resp


@router.post("/logout")
async def logout(request: Request, sf: tuple[Sess, dict[str, str]] = Depends(csrf_form)) -> Response:
    resp = _redirect(request, sf[0], "/login")
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


_STATIC = {"app.css": "text/css", "app.js": "application/javascript"}


@router.get("/static/{name}")
async def static_file(name: str) -> Response:
    if name not in _STATIC:
        raise HTTPException(404)
    return FileResponse(HERE / "static" / name, media_type=_STATIC[name], headers={"Cache-Control": "public, max-age=300"})


# --------------------------------------------------------------------------- dữ liệu dùng chung


async def _tasks(ctx: WorkbenchContext, status: TaskStatus | None = None, limit: int = 200):
    return await ctx.data.list_tasks(ctx.tenant_id, status, limit)


async def _all_evidence(ctx: WorkbenchContext, tasks, cap: int = 60) -> list[EvidenceRecord]:
    lists = await asyncio.gather(*(ctx.data.list_evidence(ctx.tenant_id, t.task_id) for t in tasks[:cap]))
    return sorted((e for lst in lists for e in lst), key=lambda e: e.created_at, reverse=True)


async def _workers_view(ctx: WorkbenchContext) -> list[dict[str, Any]]:
    now = ctx.clock()
    workers = await ctx.data.list_workers()
    hbs: list[WorkerHeartbeat | None] = [await ctx.registry.last_heartbeat(w.worker_id) if ctx.registry else None for w in workers]
    out = []
    for w, hb in zip(workers, hbs):
        stale = views.is_stale(hb.at if hb else None, now)
        out.append({"w": w, "hb": hb, "stale": stale, "age_s": int((now - hb.at).total_seconds()) if hb else None})
    return out


# --------------------------------------------------------------------------- trang


@router.get("")
@router.get("/")
async def overview(request: Request, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    tasks = await _tasks(ctx)
    pending = await ctx.data.list_approvals(ctx.tenant_id, ApprovalStatus.PENDING)
    wv = await _workers_view(ctx)
    ev = await _all_evidence(ctx, tasks)
    counts = views.count_by_status(tasks)
    running = [t for t in tasks if t.status in (TaskStatus.RUNNING, TaskStatus.SCHEDULED, TaskStatus.PLANNED)]
    online = sum(1 for x in wv if x["w"].status is WorkerStatus.ONLINE and not x["stale"])
    health = "ok" if (online == len(wv) and not counts["FAILED"]) else ("warn" if online else "bad")
    return _render(
        request, s, "overview.html", "Tổng quan", "",
        counts=counts, running=running[:10], pending=pending[:10], pending_n=len(pending), workers_online=online, workers_total=len(wv),
        cost_today=views.cost_today(ev, ctx.clock()), health=health, tasks_n=len(tasks),
    )


@router.get("/command")
async def command_page(request: Request, s: Sess = Depends(require_user)) -> Response:
    return _render(request, s, "command.html", "Ra lệnh", "/command", families=[f.value for f in TaskFamily])


@router.post("/command")
async def command_submit(request: Request, sf: tuple[Sess, dict[str, str]] = Depends(auth_csrf_form)) -> Response:
    s, f = sf
    ctx = _ctx(request)
    text = f.get("text", "").strip()
    fam = f.get("family", "")
    err = None
    if not text:
        err = "Vui lòng nhập nội dung lệnh."
    elif len(text) > 4000:
        err = "Lệnh quá dài (tối đa 4000 ký tự)."
    elif fam and fam not in {x.value for x in TaskFamily}:
        err = "Nhóm công việc không hợp lệ."
    elif ctx.ingest is None:
        err = "Chưa kết nối Event Gateway."
    result = None
    if not err:
        ev = Event(
            tenant_id=ctx.tenant_id, channel=Channel.WORKBENCH, kind=EventKind.COMMAND, text=text,
            sender=ChannelIdentity(channel_user_id=s.user or "operator", display_name=s.user),
            signature_verified=True, untrusted=False, metadata={"requested_family": fam or None, "source": "workbench_session"},
        )
        try:
            result = await ctx.ingest(ev)  # type: ignore[misc]
        except Exception:
            err = "Event Gateway từ chối hoặc không phản hồi."
    return _render(request, s, "command.html", "Ra lệnh", "/command", status=400 if err else 200, families=[x.value for x in TaskFamily], error=err, result=result, text=text if err else "")


@router.get("/workflows")
async def workflows(request: Request, status: str = "", s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    st = TaskStatus(status) if status in {x.value for x in TaskStatus} else None
    return _render(request, s, "workflows.html", "Workflows", "/workflows", tasks=await _tasks(ctx, st), statuses=[x.value for x in TaskStatus], current=st.value if st else "")


@router.get("/workflows/{task_id}")
async def workflow_detail(request: Request, task_id: str, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    task = await ctx.data.get_task(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(404, "Không tìm thấy task")
    graph = await ctx.graph_provider(ctx.tenant_id, task_id) if ctx.graph_provider else None
    ev = await ctx.data.list_evidence(ctx.tenant_id, task_id)
    return _render(request, s, "workflow_detail.html", "Task Graph", "/workflows", task=task, dag=views.layout_dag(graph) if graph else None, evidence=ev)


@router.get("/approvals")
async def approvals(request: Request, msg: str = "", s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    pending = await ctx.data.list_approvals(ctx.tenant_id, ApprovalStatus.PENDING)
    done = [a for a in await ctx.data.list_approvals(ctx.tenant_id) if a.status is not ApprovalStatus.PENDING]
    done.sort(key=lambda a: a.decided_at or a.requested_at, reverse=True)
    flash = {"approved": "Đã duyệt.", "rejected": "Đã từ chối.", "error": "Không ghi được quyết định (đã xử lý hoặc không tồn tại)."}.get(msg)
    return _render(request, s, "approvals.html", "Duyệt", "/approvals", pending=pending, done=done[:30], flash=flash, flash_bad=msg == "error")


@router.post("/approvals/{approval_id}/decision")
async def approval_decide(request: Request, approval_id: str, sf: tuple[Sess, dict[str, str]] = Depends(auth_csrf_form)) -> Response:
    s, f = sf
    ctx = _ctx(request)
    choice, comment = f.get("decision", ""), f.get("comment", "").strip()
    if choice not in {"approve", "reject"} or ctx.decide is None:
        raise HTTPException(400, "Yêu cầu không hợp lệ")
    if choice == "reject" and len(comment) < 3:
        raise HTTPException(400, "Từ chối phải kèm lý do (tối thiểu 3 ký tự)")
    dec = ApprovalDecision(
        approval_id=approval_id, status=ApprovalStatus.APPROVED if choice == "approve" else ApprovalStatus.REJECTED,  # type: ignore[arg-type]
        decided_by=f"human:{s.user}", comment=comment or None,
    )
    try:
        await ctx.decide(dec)
    except (KeyError, ValueError):
        return _redirect(request, s, "/approvals?msg=error")
    return _redirect(request, s, f"/approvals?msg={'approved' if choice == 'approve' else 'rejected'}")


@router.get("/evidence")
async def evidence_list(request: Request, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    return _render(request, s, "evidence.html", "Bằng chứng", "/evidence", records=await _all_evidence(ctx, await _tasks(ctx)))


@router.get("/evidence/{task_id}")
async def evidence_detail(request: Request, task_id: str, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    task = await ctx.data.get_task(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(404, "Không tìm thấy task")
    return _render(request, s, "evidence_detail.html", "Chi tiết bằng chứng", "/evidence", task=task, records=await ctx.data.list_evidence(ctx.tenant_id, task_id))


@router.get("/workers")
async def workers(request: Request, s: Sess = Depends(require_user)) -> Response:
    return _render(request, s, "workers.html", "Workers / VM", "/workers", rows=await _workers_view(_ctx(request)))


async def _stats(ctx: WorkbenchContext):
    return await ctx.recorder.stats() if ctx.recorder else []


@router.get("/agents")
async def agents(request: Request, s: Sess = Depends(require_user)) -> Response:
    stats = await _stats(_ctx(request))
    return _render(request, s, "agents.html", "Agents / Models", "/agents", stats=sorted(stats, key=lambda x: (x.task_family.value, x.model)))


@router.get("/costs")
async def costs(request: Request, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    ev = await _all_evidence(ctx, await _tasks(ctx))
    return _render(
        request, s, "costs.html", "Chi phí & độ trễ", "/costs", by_model=views.aggregate_costs(ev, "model"), by_day=views.aggregate_costs(ev, "day"),
        by_family=views.aggregate_costs(ev, "family"), total=round(sum(e.cost_usd for e in ev), 6), today=views.cost_today(ev, ctx.clock()),
    )


@router.get("/errors")
async def errors(request: Request, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    tasks = await _tasks(ctx)
    ev = await _all_evidence(ctx, tasks)
    failed = [t for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.ROLLED_BACK)]
    return _render(
        request, s, "errors.html", "Lỗi / Retry / Rollback", "/errors", failed=failed,
        retried=[e for e in ev if e.retries > 0], rolled=[e for e in ev if e.rollback_performed], bad=[e for e in ev if e.final_outcome is Outcome.VERIFIED_FAILURE],
    )


@router.get("/learning")
async def learning(request: Request, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    stats = await _stats(ctx)
    ev = await _all_evidence(ctx, await _tasks(ctx))
    verified = sum(1 for e in ev if e.final_outcome is not Outcome.UNVERIFIED)
    return _render(
        request, s, "learning.html", "Học tập", "/learning", rows=views.champions(stats), min_n=views.MIN_N_FOR_CHAMPION,
        n_ev=len(ev), n_verified=verified, n_success=sum(1 for e in ev if e.final_outcome is Outcome.VERIFIED_SUCCESS),
    )


@router.get("/audit")
async def audit(request: Request, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    return _render(request, s, "audit.html", "Kiểm toán", "/audit", entries=await ctx.audit_provider(ctx.tenant_id) if ctx.audit_provider else [])


@router.get("/brain")
async def brain(request: Request, q: str = "", s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    q = q.strip()[:300]
    hits = await ctx.retriever.retrieve(RetrievalQuery(tenant_id=ctx.tenant_id, text=q, top_k=10)) if (q and ctx.retriever) else []
    return _render(request, s, "brain.html", "Project Brain", "/brain", q=q, hits=hits, enabled=ctx.retriever is not None)


@router.get("/ledger")
async def ledger(request: Request, s: Sess = Depends(require_user)) -> Response:
    ctx = _ctx(request)
    return _render(request, s, "ledger.html", "Decision Ledger", "/ledger", items=views.parse_ledger(ctx.ledger_path))


_ = RiskLevel  # re-export cho template/tests
