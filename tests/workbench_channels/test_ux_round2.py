"""Workbench UX round 2 (UX-01..08): duyệt hết hạn, audit quyết định, PRG + nonce, thẻ duyệt rủi ro cao, bảng trên điện thoại,
trạng thái worker, tiếng Việt nhất quán, tương phản màu."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from tests.workbench_channels.conftest import World
from zeus.app.system import build_system
from zeus.config import Settings
from zeus.contracts.models import ApprovalRequest, ApprovalStatus, RiskLevel, TypedAction, utcnow
from zeus.storage.migrate import apply_migrations
from zeus.workbench import views
from zeus.workbench.security import AuthConfig, hash_password

ROOT = Path(__file__).resolve().parents[2]
WB = ROOT / "zeus/workbench"
PW = "ux-round2-mat-khau"


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m
    return m.group(1)


# ------------------------------------------------------------------ UX-01
async def _expired_request(w: World, risk: RiskLevel = RiskLevel.R2) -> ApprovalRequest:
    act = TypedAction(name="deploy.release", args={"target": "staging"})
    return await w.ctx.demo["approvals"].request(  # type: ignore[attr-defined]
        ApprovalRequest(action=act, risk=risk, summary_vi="Deploy bản sửa lên staging", requested_at=utcnow() - timedelta(hours=3), expires_at=utcnow() - timedelta(hours=1))
    )


async def test_ux01_deciding_expired_request_is_not_reported_as_approved(authed: World):
    apr = await _expired_request(authed)
    r = authed.client.post(f"/wb/approvals/{apr.approval_id}/decision", data={"decision": "approve", "csrf_token": authed.csrf("/wb/approvals")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=expired")
    page = authed.client.get(r.headers["location"]).text
    assert "Yêu cầu đã hết hạn, không được duyệt." in page and 'class="card bad"' in page
    assert "Đã duyệt." not in page
    got = await authed.ctx.demo["approvals"].get(apr.approval_id)  # type: ignore[attr-defined]
    assert got.status is ApprovalStatus.EXPIRED and got.decided_by is None


async def test_ux01_expired_pending_listed_without_buttons_and_not_counted(authed: World):
    apr = await _expired_request(authed)
    html = authed.client.get("/wb/approvals").text
    assert "Đã quá hạn, chưa xử lý (1)" in html
    assert f"/approvals/{apr.approval_id}/decision" not in html  # không còn form Duyệt/Từ chối cho yêu cầu quá hạn
    live_id = authed.ids["approval"]
    assert f"/approvals/{live_id}/decision" in html and "Đang chờ duyệt (1)" in html
    overview = authed.client.get("/wb").text
    assert re.search(r'Chờ duyệt</div><div class="v">1</div>', overview)


# ------------------------------------------------------------------ PG thật: UX-01 (store), UX-02, UX-03
@pytest.fixture()
async def live(pg_dsn: str, tmp_path: Path):
    from zeus.app.main import create_app

    apply_migrations(pg_dsn, ROOT / "migrations")
    settings = Settings(
        env="test", db_dsn=pg_dsn, data_dir=tmp_path, models_config=ROOT / "config/models.yaml", policy_config=ROOT / "config/policy.yaml",
        brain_config=ROOT / "config/brain.yaml", workers_config=ROOT / "config/workers.yaml", channels_config=ROOT / "config/channels.yaml",
    )
    system = build_system(settings, auth=AuthConfig(password_hash=hash_password(PW, iterations=1000)), env={})
    app = create_app(settings, system=system)
    wb = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://zeus")
    page = await wb.get("/wb/login")
    r = await wb.post("/wb/login", data={"csrf_token": _csrf(page.text), "username": "operator", "password": PW})
    assert r.status_code == 303
    try:
        yield system, wb, pg_dsn
    finally:
        await wb.aclose()


def _q(dsn: str, sql: str, *args):  # noqa: ANN001, ANN202
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return c.execute(sql, args).fetchall()


@pytest.mark.pg
async def test_ux01_pg_expired_decision_stays_expired(live):
    system, wb, dsn = live
    act = TypedAction(name="deploy.release", args={"target": "staging"})
    apr = await system.control.approvals.request(ApprovalRequest(action=act, risk=RiskLevel.R2, summary_vi="Deploy staging", expires_at=utcnow() - timedelta(minutes=5)))
    csrf = _csrf((await wb.get("/wb/approvals")).text)
    r = await wb.post(f"/wb/approvals/{apr.approval_id}/decision", data={"decision": "approve", "csrf_token": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("msg=expired")
    assert _q(dsn, "SELECT status, decided_by FROM approvals WHERE approval_id=%s", apr.approval_id) == [{"status": "EXPIRED", "decided_by": None}]


@pytest.mark.pg
async def test_ux02_workbench_decision_is_audited_and_visible(live):
    system, wb, dsn = live
    act = TypedAction(name="erp.orders.delete", args={"count": 18234, "confirm": True})
    apr = await system.control.approvals.request(ApprovalRequest(action=act, risk=RiskLevel.R3, summary_vi="Xoá 18.234 đơn hàng cũ trên ERP production"))
    assert _q(dsn, "SELECT count(*) AS n FROM audit_log WHERE action='approval.decide'") == [{"n": 0}]
    empty = (await wb.get("/wb/audit")).text
    assert "Chưa có bản ghi kiểm toán." in empty and "audit_log" not in empty
    csrf = _csrf((await wb.get("/wb/approvals")).text)
    r = await wb.post(f"/wb/approvals/{apr.approval_id}/decision", data={"decision": "approve", "comment": "đã đối chiếu", "csrf_token": csrf})
    assert r.headers["location"].endswith("msg=approved")
    rows = _q(dsn, "SELECT actor, subject_type, subject_id, risk, details FROM audit_log WHERE action='approval.decide'")
    assert len(rows) == 1 and rows[0]["actor"] == "human:operator" and rows[0]["subject_id"] == apr.approval_id and rows[0]["risk"] == "R3"
    assert rows[0]["details"]["status"] == "APPROVED" and rows[0]["details"]["comment"] == "đã đối chiếu"
    page = (await wb.get("/wb/audit")).text
    assert "approval.decide" in page and "human:operator" in page and apr.approval_id in page


@pytest.mark.pg
async def test_ux02_control_api_decision_is_audited_too(live):
    system, _wb, dsn = live
    from zeus.api.router import audit_decision
    from zeus.contracts.models import ApprovalDecision

    act = TypedAction(name="x.y", args={})
    apr = await system.control.approvals.request(ApprovalRequest(action=act, risk=RiskLevel.R2, summary_vi="s"))
    dec = ApprovalDecision(approval_id=apr.approval_id, status=ApprovalStatus.REJECTED, decided_by="human:a", comment="không")
    await audit_decision(system.control.audit, apr, dec, await system.control.approvals.decide(dec))
    assert _q(dsn, "SELECT details->>'status' AS s FROM audit_log WHERE action='approval.decide'") == [{"s": "REJECTED"}]


@pytest.mark.pg
async def test_ux03_command_resubmit_does_not_create_second_task(live):
    system, wb, dsn = live
    form = await wb.get("/wb/command")
    csrf, nonce = _csrf(form.text), re.search(r'name="nonce" value="([^"]+)"', form.text).group(1)  # type: ignore[union-attr]
    data = {"csrf_token": csrf, "nonce": nonce, "text": "kiểm tra api backend lần hai"}
    r1 = await wb.post("/wb/command", data=data)
    assert r1.status_code == 303 and "/wb/command?sent=tsk_" in r1.headers["location"] and "dup=1" not in r1.headers["location"]
    r2 = await wb.post("/wb/command", data=data)  # F5 + xác nhận gửi lại
    assert r2.status_code == 303 and "dup=1" in r2.headers["location"]
    assert r2.headers["location"].split("sent=")[1].split("&")[0] == r1.headers["location"].split("sent=")[1]
    assert _q(dsn, "SELECT count(*) AS n FROM tasks") == [{"n": 1}]
    done = (await wb.get(r2.headers["location"])).text
    assert "đã được gửi trước đó" in done
    # F5 trên trang kết quả chỉ tải lại GET: vẫn một task
    assert (await wb.get(r1.headers["location"])).status_code == 200
    assert _q(dsn, "SELECT count(*) AS n FROM tasks") == [{"n": 1}]
    # mở form lần nữa => nonce mới => lệnh mới hợp lệ
    form2 = await wb.get("/wb/command")
    nonce2 = re.search(r'name="nonce" value="([^"]+)"', form2.text).group(1)  # type: ignore[union-attr]
    assert nonce2 != nonce


# ------------------------------------------------------------------ UX-03 (demo world)
def test_ux03_prg_and_nonce_external_id(authed: World):
    form = authed.client.get("/wb/command").text
    nonce = re.search(r'name="nonce" value="([^"]+)"', form).group(1)  # type: ignore[union-attr]
    r = authed.client.post("/wb/command", data={"text": "Sửa lỗi in hoá đơn", "nonce": nonce, "csrf_token": _csrf(form)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/wb/command?sent=tsk_demo_new"
    (ev,) = authed.ctx.demo["ingested"]  # type: ignore[attr-defined]
    assert ev.external_id == f"wb:{nonce}"
    shown = authed.client.get(r.headers["location"]).text
    assert "Đã gửi lệnh." in shown and "/wb/workflows/tsk_demo_new" in shown
    # tham số sent giả mạo bị loại, không phản chiếu HTML
    evil = authed.client.get("/wb/command", params={"sent": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)" not in evil and "Đã gửi lệnh." not in evil
    # nonce bậy bị bỏ (không dùng làm khoá trùng)
    authed.client.post("/wb/command", data={"text": "x", "nonce": "a b<", "csrf_token": _csrf(form)})
    assert authed.ctx.demo["ingested"][-1].external_id is None  # type: ignore[attr-defined]


def test_ux03_validation_error_keeps_text_and_nonce(authed: World):
    form = authed.client.get("/wb/command").text
    r = authed.client.post("/wb/command", data={"text": "x", "family": "khong-co", "nonce": "abcdefgh1234", "csrf_token": _csrf(form)})
    assert r.status_code == 400 and 'name="nonce" value="abcdefgh1234"' in r.text and ">x</textarea>" in r.text


# ------------------------------------------------------------------ UX-04
async def test_ux04_high_risk_card_is_prominent(authed: World):
    act = TypedAction(name="erp.orders.delete", args={"count": 18234, "filter": {"before": "2023-01-01"}, "confirm": True})
    r3 = await authed.ctx.demo["approvals"].request(ApprovalRequest(action=act, risk=RiskLevel.R3, summary_vi="Xoá 18.234 đơn hàng cũ"))  # type: ignore[attr-defined]
    html = authed.client.get("/wb/approvals").text
    block = next(c for c in html.split('<div class="card approval ') if r3.approval_id in c)
    assert "approval-R3" in block and "Cảnh báo" in block and "R3 · Tiền / xoá / ra ngoài" in html
    assert '<details open>' in block and '&#34;count&#34;: 18234' in block and "&#39;count&#39;" not in html  # JSON đọc được, không phải repr Python
    assert 'data-confirm="Bạn chắc chắn muốn DUYỆT thao tác R3' in block
    # R2 cũng hỏi lại; R1 thì không
    r2 = next(c for c in html.split('<div class="card approval ') if authed.ids["approval"] in c)
    assert r2.startswith("approval-R2") and "data-confirm=" in r2
    css = (WB / "static/app.css").read_text(encoding="utf-8")
    assert re.search(r"\.row\.decide\{[^}]*gap:16px", css)
    assert "data.confirm" in (WB / "static/app.js").read_text(encoding="utf-8") or "dataset.confirm" in (WB / "static/app.js").read_text(encoding="utf-8")


# ------------------------------------------------------------------ UX-05
def test_ux05_every_data_cell_has_label_and_css_makes_cards():
    import glob

    for f in glob.glob(str(WB / "templates/*.html")):
        text = Path(f).read_text(encoding="utf-8")
        assert not re.search(r"<td>", text), f"{f}: <td> thiếu data-label (bảng không đọc được trên điện thoại)"
    css = (WB / "static/app.css").read_text(encoding="utf-8")
    mobile = css[css.index("@media (max-width:820px)") :]
    assert "attr(data-label)" in mobile and "table,tbody,tr,td{display:block" in mobile and "table{min-width:0}" in mobile
    assert "linear-gradient" in css.split("@media (max-width:820px)")[0]  # gợi ý cuộn khi bảng rộng trên máy tính


def test_ux05_rendered_overview_labels_risk_column(authed: World):
    html = authed.client.get("/wb").text
    assert 'data-label="Rủi ro"' in html and 'data-label="Trạng thái"' in html


# ------------------------------------------------------------------ UX-06
def test_ux06_stale_worker_not_shown_online(authed: World):
    html = authed.client.get("/wb/workers").text
    row = re.search(r'<tr class="stale">.*?</tr>', html, re.S).group(0)  # type: ignore[union-attr]
    assert "Mất tín hiệu" in row and "Trực tuyến" not in row and "10 phút trước" in row
    assert "100%" not in row and "0 MB" not in row  # không hiện số liệu mặc định của heartbeat cũ như số thật
    live = re.search(r'<tr class="">.*?</tr>', html, re.S).group(0)  # type: ignore[union-attr]
    assert "Trực tuyến" in live and "MB" in live


def test_ux06_ago_formatting():
    from zeus.workbench.router import _fmt_ago

    assert _fmt_ago(5) == "5 giây trước" and _fmt_ago(627) == "10 phút trước" and _fmt_ago(7300) == "2 giờ trước" and _fmt_ago(200000) == "2 ngày trước" and _fmt_ago(None) == "chưa có"


# ------------------------------------------------------------------ UX-07
def test_ux07_vietnamese_labels_everywhere(authed: World):
    pages = {p: authed.client.get(f"/wb{p}").text for p in ("", "/workflows", "/workers", "/agents", "/learning", "/brain?q=Workbench", "/ledger", "/costs", "/errors")}
    nav = pages[""]
    for label in ("Luồng việc", "Tác tử / Mô hình", "Máy chạy (Worker)", "Bộ nhớ dự án", "Sổ quyết định"):
        assert label in nav
    for old in ("Workers / VM", "Agents / Models", "Project Brain", "Decision Ledger", ">Workflows<"):
        assert old not in nav, old
    for raw in ("erp_bug", "website_edit", "linux_vm", "windows_vm", "gpu_container"):
        assert not any(f">{raw}<" in t or f"{raw}</td>" in t for t in pages.values()), raw
    assert "Lỗi ERP" in pages["/workflows"] and "Máy ảo Linux" in pages["/workers"]
    brain = pages["/brain?q=Workbench"]
    assert "Quyết định" in brain and not re.search(r'badge">(verified|unverified|untrusted)<', brain)
    assert "Cost / verified success" not in pages["/agents"]
    authed.ctx.ledger_path = ROOT / "docs/state/DECISION_LEDGER.md"
    ledger = authed.client.get("/wb/ledger").text
    assert "Đã chấp nhận" in ledger and "ACCEPTED" not in ledger and "`" not in ledger


def test_ux07_workflow_detail_title_and_heading(authed: World):
    html = authed.client.get(f"/wb/workflows/{authed.ids['running']}").text
    assert "<h1>Sửa lỗi in hoá đơn trên ERP staging</h1>" in html and "<h2>Sơ đồ công việc</h2>" in html and "Task Graph" not in html
    assert html.count("Sửa lỗi in hoá đơn trên ERP staging") == 2  # <title> + h1, không lặp thêm


def test_ux07_ledger_strips_backticks_and_maps_status():
    items = views.parse_ledger(ROOT / "docs/state/DECISION_LEDGER.md")
    assert all("`" not in (i["decision"] + i["status"] + i["title"]) for i in items)
    from zeus.workbench.router import _adr_status

    assert _adr_status("ACCEPTED (cài trên host: GATED)") == "Đã chấp nhận (cài trên host: GATED)" and _adr_status("LẠ") == "LẠ"


# ------------------------------------------------------------------ UX-08
def _lum(h: str) -> float:
    h = h.lstrip("#")
    c = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    f = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]


def _cr(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens(css: str, selector: str) -> dict[str, str]:
    body = re.search(re.escape(selector) + r"\{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    return dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b", body))


def test_ux08_contrast_meets_wcag_aa_in_both_themes():
    css = (WB / "static/app.css").read_text(encoding="utf-8")
    themes = {"light": _tokens(css, ":root"), "dark": _tokens(css, ':root[data-theme="dark"]')}
    themes["light"].update({"panel": "#ffffff"} if "panel" not in themes["light"] else {})
    for name, t in themes.items():
        t = {k: (v if len(v) == 7 else "#" + "".join(ch * 2 for ch in v[1:])) for k, v in t.items()}
        for fg in ("muted", "ok", "warn", "bad", "accent", "text"):
            for bg in ("code", "panel", "bg"):
                assert _cr(t[fg], t[bg]) >= 4.5, (name, fg, bg, _cr(t[fg], t[bg]))
        assert _cr("#ffffff", t["bad-btn"]) >= 4.5, name  # nút "Từ chối": chữ trắng trên nền đỏ đậm ở cả hai theme
    assert "background:var(--bad-btn)" in css and "font-size:.8rem" in css
