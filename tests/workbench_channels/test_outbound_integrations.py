"""Outbound kênh, Odoo JSON-2, SaaS/site, domain: mọi thứ qua ToolGateway + approval; không mạng thật (MockTransport)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from zeus.channels.config import build_adapters, build_outbound, load_yaml
from zeus.channels.outbound import ChannelOutboundProvider, check_messenger_window
from zeus.channels.zalo_bot import ZaloBotAdapter
from zeus.channels.messenger import MessengerAdapter
from zeus.contracts.interfaces import ApprovalRequired, PolicyDenied, ToolProvider
from zeus.contracts.models import ApprovalDecision, ApprovalStatus, Channel, TypedAction
from zeus.integrations.domain import Availability, DomainToolProvider, Registration
from zeus.integrations.odoo import OdooAccess, OdooError, OdooJson2Client, OdooToolProvider, action_name
from zeus.integrations.saas import SiteToolProvider
from zeus.testing.fakes import FakeToolGateway, FakePolicyEngine, FakeToolProvider, InMemoryApprovalStore

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


async def approve(store: InMemoryApprovalStore, action: TypedAction) -> None:
    req = (await store.list(action.tenant_id, ApprovalStatus.PENDING))[-1]
    assert req.action.action_id == action.action_id
    await store.decide(ApprovalDecision(approval_id=req.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:operator"))


# ------------------------------------------------------------------ kênh outbound


def make_outbound(handler, tokens=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapters = {Channel.ZALO_BOT: ZaloBotAdapter("s"), Channel.MESSENGER: MessengerAdapter("a", "v")}
    specs = [s for a in adapters.values() for s in a.outbound_specs()]
    tokens = tokens if tokens is not None else {Channel.ZALO_BOT: lambda: "BOTTOKEN", Channel.MESSENGER: lambda: "PAGETOKEN"}
    return ChannelOutboundProvider(specs, tokens, http, now=lambda: NOW)


def gateway(provider: ToolProvider):
    store = InMemoryApprovalStore()
    gw = FakeToolGateway([provider], FakePolicyEngine(), store)  # type: ignore[list-item]
    return gw, store


async def test_send_requires_approval_then_splits_long_text():
    calls: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": f"m{len(calls)}"}})

    gw, store = gateway(make_outbound(h))
    text = "Chào bạn. " * 500  # 5000 ký tự
    act = TypedAction(name="channel.zalo_bot.send_message", args={"to": "c1", "text": text})
    with pytest.raises(ApprovalRequired):
        await gw.execute(act)
    assert calls == []  # chưa duyệt => KHÔNG có request ra ngoài
    await approve(store, act)
    res = await gw.execute(act)
    assert res.ok and res.output["sent_chunks"] == len(calls) >= 3
    assert all(c.url.path == "/botBOTTOKEN/sendMessage" and len(json.loads(c.content)["text"]) <= 2000 for c in calls)
    assert res.output["message_ids"] == [f"m{i}" for i in range(1, len(calls) + 1)]


async def test_send_missing_token_or_http_error_is_clean_failure_without_leaking_token():
    gw, store = gateway(make_outbound(lambda r: httpx.Response(500, text="PAGETOKEN boom")))
    a = TypedAction(name="channel.zalo_bot.send_message", args={"to": "c", "text": "hi"})
    with pytest.raises(ApprovalRequired):
        await gw.execute(a)
    await approve(store, a)
    r = await gw.execute(a)
    assert not r.ok and "500" in r.error and "BOTTOKEN" not in r.error
    gw2, store2 = gateway(make_outbound(lambda r: httpx.Response(200, json={}), tokens={}))
    b = TypedAction(name="channel.zalo_bot.send_message", args={"to": "c", "text": "hi"})
    with pytest.raises(ApprovalRequired):
        await gw2.execute(b)
    await approve(store2, b)
    assert "chưa cấu hình" in (await gw2.execute(b)).error


def test_messenger_24h_window():
    ok = {"last_inbound_at": (NOW - timedelta(hours=23)).isoformat()}
    assert check_messenger_window(ok, NOW) is True
    late = {"last_inbound_at": (NOW - timedelta(hours=30)).isoformat()}
    with pytest.raises(PolicyDenied):
        check_messenger_window(late, NOW)
    with pytest.raises(PolicyDenied):
        check_messenger_window({**late, "messaging_tag": "HUMAN_AGENT"}, NOW)  # thiếu người vận hành
    assert check_messenger_window({**late, "messaging_tag": "HUMAN_AGENT", "human_operator": "an"}, NOW) is False
    with pytest.raises(PolicyDenied):  # quá 7 ngày: không còn đường nào
        check_messenger_window({"last_inbound_at": (NOW - timedelta(days=8)).isoformat(), "messaging_tag": "HUMAN_AGENT", "human_operator": "an"}, NOW)
    with pytest.raises(PolicyDenied):
        check_messenger_window({}, NOW)


async def test_messenger_send_blocked_after_24h_even_when_approved():
    calls: list[httpx.Request] = []
    gw, store = gateway(make_outbound(lambda r: calls.append(r) or httpx.Response(200, json={"message_id": "x"})))
    late = (NOW - timedelta(hours=40)).isoformat()
    a = TypedAction(name="channel.messenger.send_message", args={"to": "psid", "text": "Em chào anh", "last_inbound_at": late})
    with pytest.raises(ApprovalRequired):
        await gw.execute(a)
    await approve(store, a)
    with pytest.raises(PolicyDenied):
        await gw.execute(a)
    assert calls == []
    h = TypedAction(name="channel.messenger.send_message", args={"to": "psid", "text": "Em chào anh", "last_inbound_at": late, "messaging_tag": "HUMAN_AGENT", "human_operator": "an"})
    with pytest.raises(ApprovalRequired):
        await gw.execute(h)
    await approve(store, h)
    assert (await gw.execute(h)).ok
    body = json.loads(calls[0].content)
    assert body["messaging_type"] == "MESSAGE_TAG" and body["tag"] == "HUMAN_AGENT" and calls[0].headers["authorization"] == "Bearer PAGETOKEN"


def test_no_direct_send_path_outside_tool_provider():
    """Acceptance 4: chỉ outbound.py (và client Odoo/site/registrar có approval) được dùng HTTP client trong khu vực D."""
    http_users = {p.relative_to(ROOT).as_posix() for d in ("zeus/channels", "zeus/integrations", "zeus/workbench") for p in (ROOT / d).rglob("*.py") if re.search(r"\bhttpx\b|\burllib\.request\b|\brequests\b|\bsocket\b|\baiohttp\b", p.read_text(encoding="utf-8"))}
    assert http_users == {
        "zeus/channels/outbound.py",
        "zeus/channels/config.py",  # chỉ gõ kiểu httpx.AsyncClient để truyền vào outbound
        "zeus/integrations/odoo/client.py",
        "zeus/integrations/saas/provider.py",
        "zeus/workbench/control_client.py",  # gọi Control API nội bộ, không phải bên thứ ba
    }
    for ad in ("zalo_bot", "zalo_oa", "messenger", "shopee"):  # adapter thuần verify+normalize
        src = (ROOT / f"zeus/channels/{ad}.py").read_text(encoding="utf-8")
        assert "import httpx" not in src and ".post(" not in src


def test_build_outbound_from_config():
    cfg = load_yaml(ROOT / "config/channels.yaml")
    ad = build_adapters(cfg, {"ZEUS_ZALO_BOT_SECRET_TOKEN": "s"})
    prov = build_outbound(cfg, ad, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))), {"ZEUS_ZALO_BOT_TOKEN": "T"})
    assert [s.name for s in prov.specs()] == ["channel.zalo_bot.send_message"]


# ------------------------------------------------------------------ Odoo JSON-2


def odoo(handler, write=None, approvals=None, key="ODOOKEY"):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OdooJson2Client("https://erp-stg.local/", "stgdb", lambda: key, OdooAccess(frozenset({"sale.order", "res.partner"}), write or {}), http)
    return client, OdooToolProvider(client, approvals)


async def test_odoo_read_request_shape():
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=[{"id": 7, "name": "SO007"}])

    client, prov = odoo(h)
    out = await client.call("sale.order", "search_read", params={"domain": [["state", "=", "sale"]], "fields": ["name"], "limit": 5})
    assert out == [{"id": 7, "name": "SO007"}]
    r = seen[0]
    assert r.method == "POST" and str(r.url) == "https://erp-stg.local/json/2/sale.order/search_read"
    assert r.headers["authorization"] == "bearer ODOOKEY" and r.headers["x-odoo-database"] == "stgdb"
    body = json.loads(r.content)
    assert body["domain"] == [["state", "=", "sale"]] and body["limit"] == 5 and "context" in body


async def test_odoo_default_is_read_only_and_blocks_raw_crud():
    client, prov = odoo(lambda r: httpx.Response(200, json=True))
    for model, method in (("sale.order", "write"), ("sale.order", "create"), ("sale.order", "unlink"), ("sale.order", "action_confirm"), ("account.move", "search_read"), ("sale.order", "_private"), ("sale.order", "execute_kw"), ("bad model", "read")):
        with pytest.raises(PolicyDenied):
            await client.call(model, method, ids=[1])
    assert all(s.risk.value == "R0" and not s.external for s in prov.specs())  # chỉ spec đọc
    with pytest.raises(ValueError):  # không thể "hợp pháp hoá" CRUD thô vào allowlist
        OdooAccess(frozenset(), {"sale.order": frozenset({"write"})})
    with pytest.raises(ValueError):
        OdooAccess(frozenset(), {"sale.order": frozenset({"search_read"})})


async def test_odoo_write_needs_approval_context_and_store():
    calls: list[httpx.Request] = []
    approvals = InMemoryApprovalStore()
    client, prov = odoo(lambda r: calls.append(r) or httpx.Response(200, json=True), {"sale.order": frozenset({"action_confirm"})}, approvals)
    with pytest.raises(PolicyDenied):  # client trực tiếp: thiếu ApprovalProof
        await client.call("sale.order", "action_confirm", ids=[7])
    spec = next(s for s in prov.specs() if s.name == "erp.sale_order.action_confirm")
    assert spec.risk.value == "R2" and spec.external and not spec.reversible
    act = TypedAction(name=action_name("sale.order", "action_confirm"), args={"ids": [7]})
    with pytest.raises(PolicyDenied):  # provider tự từ chối khi chưa có approval
        await prov.execute(act, spec)
    gw, store = gateway(OdooToolProvider(client, approvals))
    gw.approvals = approvals  # dùng chung store
    with pytest.raises(ApprovalRequired):
        await gw.execute(act)
    assert calls == []
    await approve(approvals, act)
    res = await gw.execute(act)
    assert res.ok and calls[0].url.path == "/json/2/sale.order/action_confirm" and json.loads(calls[0].content)["ids"] == [7]
    # approval của action khác/args khác không dùng được
    other = TypedAction(name=act.name, args={"ids": [8]})
    with pytest.raises(PolicyDenied):
        await prov.execute(other, spec)
    tampered = act.model_copy(update={"args": {"ids": [9]}})
    with pytest.raises(PolicyDenied):
        await prov.execute(tampered, spec)


async def test_odoo_write_without_approval_store_publishes_no_write_specs():
    _, prov = odoo(lambda r: httpx.Response(200), {"sale.order": frozenset({"action_confirm"})}, approvals=None)
    assert not any("action_confirm" in s.name for s in prov.specs())


async def test_odoo_errors_do_not_leak_key():
    client, prov = odoo(lambda r: httpx.Response(401, json={"message": "bad key ODOOKEY"}))
    with pytest.raises(OdooError) as ei:
        await client.call("res.partner", "read", ids=[1])
    assert ei.value.status == 401 and "ODOOKEY" not in str(ei.value)
    spec = next(s for s in prov.specs() if s.name == "erp.res_partner.read")
    res = await prov.execute(TypedAction(name=spec.name, args={"ids": [1]}), spec)
    assert not res.ok and "ODOOKEY" not in res.error
    no_key, _ = odoo(lambda r: httpx.Response(200), key=None)
    with pytest.raises(PolicyDenied):
        await no_key.call("res.partner", "read", ids=[1])


# ------------------------------------------------------------------ domain


class Registrar:
    def __init__(self):
        self.registered: list[str] = []

    async def check(self, domain):
        return Availability(domain, True, 250_000)

    async def register(self, domain, years, customer_ref):
        self.registered.append(domain)
        return Registration(domain, "reg-1", "2027-10-04")


class Pay:
    def __init__(self, ok):
        self.ok = ok

    async def is_payment_confirmed(self, tenant_id, payment_ref, domain):
        return self.ok


class Ekyc:
    def __init__(self, ok):
        self.ok = ok

    async def customer_ekyc_done(self, tenant_id, customer_ref, domain):
        return self.ok


def domain_world(paid=True, ekyc=True):
    store, reg = InMemoryApprovalStore(), Registrar()
    prov = DomainToolProvider(reg, Pay(paid), Ekyc(ekyc), store)
    gw = FakeToolGateway([prov], FakePolicyEngine(), store)  # type: ignore[list-item]
    return gw, prov, store, reg


def reg_action(domain="shop-abc.com", **kw):
    return TypedAction(name="domain.register", args={"domain": domain, "years": 1, "customer_ref": "kh1", "payment_ref": "pay1", **kw})


async def test_domain_register_blocked_without_payment_even_if_approved():
    gw, prov, store, reg = domain_world(paid=False)
    a = reg_action()
    with pytest.raises(ApprovalRequired):
        await gw.execute(a)
    await approve(store, a)
    with pytest.raises(PolicyDenied, match="chưa thu tiền"):
        await gw.execute(a)
    # LLM tự khai payment_confirmed=true trong args cũng vô hiệu
    b = reg_action(payment_confirmed=True)
    with pytest.raises(ApprovalRequired):
        await gw.execute(b)
    await approve(store, b)
    with pytest.raises(PolicyDenied):
        await gw.execute(b)
    assert reg.registered == []


async def test_domain_register_needs_approval_and_vn_needs_ekyc():
    gw, prov, store, reg = domain_world()
    spec = next(s for s in prov.specs() if s.name == "domain.register")
    assert spec.risk.value == "R3" and not spec.reversible
    a = reg_action()
    with pytest.raises(PolicyDenied, match="approval"):
        await prov.execute(a, spec)  # gọi thẳng provider không qua duyệt: bị chặn
    with pytest.raises(ApprovalRequired):
        await gw.execute(a)
    await approve(store, a)
    assert (await gw.execute(a)).output["registrar_ref"] == "reg-1" and reg.registered == ["shop-abc.com"]
    gw2, prov2, store2, reg2 = domain_world(ekyc=False)
    v = reg_action("cua-hang.vn")
    with pytest.raises(ApprovalRequired):
        await gw2.execute(v)
    await approve(store2, v)
    with pytest.raises(PolicyDenied, match="eKYC"):
        await gw2.execute(v)
    assert reg2.registered == []
    c = reg_action("cua-hang.com")  # .com không cần eKYC của khách
    with pytest.raises(ApprovalRequired):
        await gw2.execute(c)
    await approve(store2, c)
    assert (await gw2.execute(c)).ok


async def test_domain_input_validation_and_check():
    gw, prov, store, _ = domain_world()
    assert (await gw.execute(TypedAction(name="domain.check", args={"domain": "Abc.com"}))).output["available"] is True
    for bad in ("not a domain", "a..com", "-x.com", "x" * 300 + ".com"):
        with pytest.raises(PolicyDenied):
            await gw.execute(TypedAction(name="domain.check", args={"domain": bad}))


# ------------------------------------------------------------------ site


class Deployer:
    def __init__(self):
        self.calls = []

    async def deploy(self, site_id, artifact_ref, host):
        self.calls.append((site_id, artifact_ref, host))
        return {"release": "r1"}


async def test_site_deploy_allowlist_forbidden_host_and_approval():
    store, dep = InMemoryApprovalStore(), Deployer()
    prov = SiteToolProvider(dep, ["vps-khach-1.example.vn"], forbidden_hosts=["erp-prod.example.vn"], approvals=store)
    gw = FakeToolGateway([prov], FakePolicyEngine(), store)  # type: ignore[list-item]
    assert next(s for s in prov.specs() if s.name == "site.deploy").risk.value == "R2"
    for host in ("erp-prod.example.vn", "random.example.com"):
        bad = TypedAction(name="site.deploy", args={"site_id": "s1", "artifact_ref": "artifact://x", "host": host})
        with pytest.raises(ApprovalRequired):
            await gw.execute(bad)
        await approve(store, bad)
        with pytest.raises(PolicyDenied):
            await gw.execute(bad)
    ok = TypedAction(name="site.deploy", args={"site_id": "s1", "artifact_ref": "artifact://x", "host": "vps-khach-1.example.vn"})
    with pytest.raises(ApprovalRequired):
        await gw.execute(ok)
    assert dep.calls == []
    await approve(store, ok)
    assert (await gw.execute(ok)).output["release"] == "r1" and dep.calls == [("s1", "artifact://x", "vps-khach-1.example.vn")]


async def test_site_http_check_is_evidence_and_ssrf_guarded():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<h1>Cửa hàng</h1>")))
    prov = SiteToolProvider(Deployer(), ["khach.example.vn"], approvals=None, http=http)
    gw = FakeToolGateway([prov], FakePolicyEngine(), InMemoryApprovalStore())  # type: ignore[list-item]
    ok = await gw.execute(TypedAction(name="site.http_check", args={"url": "https://khach.example.vn/", "expect_text": "Cửa hàng"}))
    assert ok.ok and ok.output["status"] == 200
    miss = await gw.execute(TypedAction(name="site.http_check", args={"url": "https://khach.example.vn/", "expect_text": "không có"}))
    assert not miss.ok
    for url in ("http://127.0.0.1:8080/", "http://169.254.169.254/latest", "file:///etc/passwd", "https://khach.example.vn.evil.com/"):
        with pytest.raises(PolicyDenied):
            await gw.execute(TypedAction(name="site.http_check", args={"url": url}))


def test_providers_satisfy_protocol():
    store = InMemoryApprovalStore()
    c = OdooJson2Client("http://x", "d", lambda: "k", OdooAccess(frozenset({"res.partner"})))
    for p in (OdooToolProvider(c, store), DomainToolProvider(Registrar(), Pay(True), Ekyc(True), store), SiteToolProvider(Deployer(), [], approvals=store), FakeToolProvider([])):
        assert isinstance(p, ToolProvider)
