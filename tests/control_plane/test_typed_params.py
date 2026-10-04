"""Phase 2 (1): planner sinh tham số typed action từ entity của Intent — chỉ khi qua allowlist + schema, fail closed."""

from __future__ import annotations

import json

import pytest

from zeus.app.system import worker_action_specs
from zeus.contracts.models import ActionSpec, Channel, Event, RiskLevel, Task, TaskFamily, TypedAction
from zeus.intent.engine import DefaultIntentEngine, _entities
from zeus.planning.planner import DefaultPlanner, typed_args
from zeus.policy.engine import DefaultPolicyEngine, PolicyConfig
from zeus.policy.jsonschema import validate
from zeus.policy.params import ParamPolicy

PP = ParamPolicy.from_dict({"http_allow_domains": ["shop.vn", ".staging.example.com"], "file_roots": ["/srv/releases"], "repo_roots": ["/srv/repos"]})


def task_with(family: TaskFamily = TaskFamily.BACKEND, **entities: str) -> Task:
    return Task(family=family, goal="mục tiêu", tenant_id="zeusvn", entities=entities)


def planner(extra: list[ActionSpec] | None = None, params: ParamPolicy | None = PP) -> DefaultPlanner:
    specs = [*worker_action_specs(), *(extra or [])]
    return DefaultPlanner(None, lambda: specs, params=params)


# ------------------------------------------------------------------ ParamPolicy
@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://shop.vn/health", True), ("http://www.shop.vn/a?b=1#frag", True), ("https://x.staging.example.com/", True),
        ("https://staging.example.com/", False),  # ".staging.example.com" chỉ cho miền con
        ("https://evil.com/", False), ("https://shop.vn.evil.com/", False), ("https://notshop.vn/", False),
        ("https://user:pw@shop.vn/", False), ("https://shop.vn:8443/", False), ("ftp://shop.vn/", False), ("javascript:alert(1)", False),
        ("https://127.0.0.1/", False), ("https://[::1]/", False), ("http://localhost/", False), ("https://shop.vn/a b", False), ("", False), (None, False),
        ("https://shop.vn/" + "a" * 2100, False),
    ],
)
def test_url_allowlist(url, ok):
    assert (PP.url(url) is not None) is ok


def test_url_strips_fragment_and_empty_allowlist_denies_all():
    assert PP.url("https://shop.vn/x#frag") == "https://shop.vn/x"
    assert ParamPolicy().url("https://shop.vn/") is None and ParamPolicy().domain("shop.vn") is None


def test_paths_roots_and_traversal():
    assert PP.file_path("/srv/releases/app-1.2.tar.gz") == "/srv/releases/app-1.2.tar.gz" and PP.file_path("/srv/releases") == "/srv/releases"
    for bad in ("/srv/releases/../etc/passwd", "/srv/releasesX/a", "/etc/passwd", "srv/releases/a", "/srv/releases/a\x00b", None, 5):
        assert PP.file_path(bad) is None, bad
    assert PP.repo_path("/srv/repos/shop//./tests") == "/srv/repos/shop/tests" and PP.repo_path("/srv/releases/x") is None
    assert PP.sha256("A" * 64) == "a" * 64 and PP.sha256("xyz") is None
    assert PP.target("tests/unit") == "tests/unit" and PP.target("--collect-only") is None and PP.target("../x") is None and PP.target("/abs") is None


def test_violations_per_action():
    assert PP.violations("http.check", {"url": "https://evil.com/"}) and not PP.violations("http.check", {"url": "https://shop.vn/"})
    assert PP.violations("file.checksum", {"path": "/etc/shadow"}) and PP.violations("file.checksum", {"path": "/srv/releases/a", "expected_sha256": "zz"})
    assert PP.violations("repo.tests.run", {"repo": "/srv/repos/a", "target": "-x"}) and not PP.violations("repo.tests.run", {"repo": "/srv/repos/a"})
    assert PP.violations("test.run", {"goal": "x"}) == []  # action không thuộc nhóm có allowlist


# ------------------------------------------------------------------ entity của Intent
def test_intent_entities_path_sha_and_url_cleanup():
    sha = "ab" * 32
    e = _entities(f"Kiểm tra tệp /srv/releases/app.tar.gz (sha256 {sha}) và trang https://shop.vn/health, rồi báo lại.")
    assert e["path"] == "/srv/releases/app.tar.gz" and e["sha256"] == sha and e["url"] == "https://shop.vn/health" and e["domain"] == "shop.vn"
    assert "path" not in _entities("họp ngày 04/10, ci/cd và https://shop.vn/a/b")  # không nhầm ngày/URL/ci/cd thành đường dẫn
    assert _entities("xem /srv/repos/shop.")["path"] == "/srv/repos/shop"
    assert _entities("đơn hàng #12345 lỗi")["order_id"] == "12345"


async def test_intent_engine_returns_entities_for_planner():
    intent = await DefaultIntentEngine().classify(Event(channel=Channel.INTERNAL, text="sửa backend API rồi kiểm tra https://shop.vn/health"))
    assert intent.entities["url"] == "https://shop.vn/health"


# ------------------------------------------------------------------ planner
def test_typed_args_from_entities_only_when_allowlisted():
    assert typed_args("http.check", task_with(url="https://shop.vn/health"), PP) == {"url": "https://shop.vn/health", "expect_status": 200}
    assert typed_args("http.check", task_with(domain="shop.vn"), PP) == {"url": "https://shop.vn/", "expect_status": 200}
    assert typed_args("http.check", task_with(url="https://evil.com/x", domain="evil.com"), PP) is None
    assert typed_args("http.check", task_with(url="https://shop.vn/x"), ParamPolicy()) is None  # allowlist rỗng
    sha = "c" * 64
    assert typed_args("file.checksum", task_with(path="/srv/releases/a.tgz", sha256=sha), PP) == {"path": "/srv/releases/a.tgz", "expected_sha256": sha}
    assert typed_args("file.checksum", task_with(path="/etc/passwd"), PP) is None
    assert typed_args("repo.tests.run", task_with(path="/srv/repos/shop"), PP) == {"repo": "/srv/repos/shop"}
    assert typed_args("erp.sale_order.read", task_with(order_id="12345"), PP) == {"ids": [12345]}
    assert typed_args("erp.sale_order.read", task_with(order_id="SO00012"), PP) is None


def test_playbook_enables_http_check_with_allowlisted_url_and_schema_valid():
    plan = planner().playbook_plan(task_with(url="https://shop.vn/health"))
    node = plan.graph.node("check")
    assert node.action and node.action.name == "http.check" and node.action.args == {"url": "https://shop.vn/health", "expect_status": 200}
    spec = next(s for s in worker_action_specs() if s.name == "http.check")
    assert validate(node.action.args, spec.input_schema) == [] and "action:http.check" in spec.required_capabilities
    assert plan.graph.node("report").depends_on == ["test", "check"]


def test_playbook_without_entities_or_allowlist_keeps_old_shape_and_rewires():
    for p, t in ((planner(), task_with()), (planner(params=ParamPolicy()), task_with(url="https://shop.vn/x"))):
        plan = p.playbook_plan(t)
        assert [n.node_id for n in plan.graph.nodes] == ["analyze", "implement", "test", "report"]
        assert plan.graph.node("report").depends_on == ["test"]  # bước optional bị bỏ => nối thẳng, DAG hợp lệ
        assert plan.graph.ready_nodes(set())


def test_deployment_verify_http_check_manual_when_no_url_and_checksum_optional():
    p = planner()
    no_url = p.playbook_plan(task_with(TaskFamily.DEPLOYMENT))
    verify = no_url.graph.node("verify")
    assert verify.action is None and any("http.check" in a and "allowlist" in a for a in no_url.assumptions)  # trước đây: bước http.check không có tham số
    assert verify.depends_on == ["deploy"] and "checksum" not in {n.node_id for n in no_url.graph.nodes}
    full = p.playbook_plan(task_with(TaskFamily.DEPLOYMENT, url="https://shop.vn/", path="/srv/releases/app.tgz", sha256="d" * 64))
    assert full.graph.node("checksum").action.args["expected_sha256"] == "d" * 64  # type: ignore[union-attr]
    assert full.graph.node("verify").depends_on == ["deploy", "checksum"] and full.graph.node("verify").action.name == "http.check"  # type: ignore[union-attr]


def test_test_run_becomes_repo_tests_run_for_named_repo():
    plan = planner().playbook_plan(task_with(path="/srv/repos/shop"))
    act = plan.graph.node("test").action
    assert act and act.name == "repo.tests.run" and act.args == {"repo": "/srv/repos/shop"}
    spec = next(s for s in worker_action_specs() if s.name == "repo.tests.run")
    assert validate(act.args, spec.input_schema) == []
    other = planner().playbook_plan(task_with(path="/etc"))  # ngoài repo_roots => vẫn test.run cấu hình sẵn
    assert other.graph.node("test").action.name == "test.run"  # type: ignore[union-attr]


def test_erp_read_alias_uses_order_id_when_odoo_action_published():
    odoo = ActionSpec(name="erp.sale_order.read", risk=RiskLevel.R0, required_capabilities=["odoo_client"], idempotent=True)
    plan = planner([odoo]).playbook_plan(task_with(TaskFamily.CUSTOMER_SUPPORT, order_id="12345"))
    assert plan.graph.node("lookup").action.name == "erp.sale_order.read" and plan.graph.node("lookup").action.args == {"ids": [12345]}  # type: ignore[union-attr]
    assert planner([odoo]).playbook_plan(task_with(TaskFamily.CUSTOMER_SUPPORT)).graph.node("lookup").action is None


async def test_model_plan_cannot_invent_params(models_cfg, policy_cfg):
    from zeus.broker.providers import FakeProvider
    from zeus.contracts.models import ProviderKind
    from tests.control_plane.test_planning import broker_for, ctx

    nodes = [{"node_id": "c", "title": "Check", "depends_on": [], "action": "http.check", "risk": "R0", "acceptance": ["200"]}]
    f = FakeProvider(ProviderKind.LOCAL, models=models_cfg.models, script=lambda r: json.dumps({"rationale": "x", "nodes": nodes}))
    specs = worker_action_specs()
    p = DefaultPlanner(broker_for(models_cfg, policy_cfg, {ProviderKind.LOCAL: f}), lambda: specs, params=PP)
    t = Task(family=TaskFamily.BACKEND, goal="g", tenant_id="zeusvn")  # không có entity
    plan = await p.plan(t, ctx(t))
    assert plan.planner == "playbook-2" and any("model plan bị loại" in a for a in plan.assumptions)  # model đòi http.check mà không có url hợp lệ
    t2 = Task(family=TaskFamily.BACKEND, goal="g", tenant_id="zeusvn", entities={"url": "https://shop.vn/health"})
    plan2 = await p.plan(t2, ctx(t2))
    assert plan2.planner.startswith("local:") and plan2.graph.node("c").action.args["url"] == "https://shop.vn/health"  # type: ignore[union-attr]


# ------------------------------------------------------------------ Policy Engine (phòng thủ lớp 2)
def test_policy_engine_denies_params_outside_allowlist_even_if_planner_bypassed(policy_cfg):
    cfg = policy_cfg.model_copy(update={"params": PP})
    eng = DefaultPolicyEngine(cfg)
    specs = {s.name: s for s in worker_action_specs()}
    bad = eng.evaluate(TypedAction(name="http.check", tenant_id="zeusvn", args={"url": "http://169.254.169.254/latest/meta-data"}), specs["http.check"])
    assert bad.effect.value == "DENY" and bad.rule_ids == ["P-PARAM-ALLOWLIST"]
    ok = eng.evaluate(TypedAction(name="http.check", tenant_id="zeusvn", args={"url": "https://shop.vn/"}), specs["http.check"])
    assert ok.effect.value == "ALLOW"
    assert eng.evaluate(TypedAction(name="file.checksum", tenant_id="zeusvn", args={"path": "/etc/shadow"}), specs["file.checksum"]).rule_ids == ["P-PARAM-ALLOWLIST"]
    # allowlist rỗng (cấu hình mặc định) => fail closed
    assert DefaultPolicyEngine(policy_cfg).evaluate(TypedAction(name="http.check", tenant_id="zeusvn", args={"url": "https://shop.vn/"}), specs["http.check"]).effect.value == "DENY"


def test_untrusted_task_may_auto_http_check_but_file_and_repo_need_approval(policy_cfg):
    eng = DefaultPolicyEngine(policy_cfg.model_copy(update={"params": PP}))
    specs = {s.name: s for s in worker_action_specs()}
    t = Task(family=TaskFamily.BACKEND, goal="g", tenant_id="zeusvn", untrusted=True)
    assert eng.evaluate(TypedAction(name="http.check", tenant_id="zeusvn", args={"url": "https://shop.vn/"}), specs["http.check"], t).effect.value == "ALLOW"
    fc = eng.evaluate(TypedAction(name="file.checksum", tenant_id="zeusvn", args={"path": "/srv/releases/a"}), specs["file.checksum"], t)
    rt = eng.evaluate(TypedAction(name="repo.tests.run", tenant_id="zeusvn", args={"repo": "/srv/repos/a"}), specs["repo.tests.run"], t)
    assert fc.rule_ids == ["P-UNTRUSTED-ORIGIN"] and rt.rule_ids == ["P-UNTRUSTED-ORIGIN"]


def test_policy_yaml_params_section_loads(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("params:\n  http_allow_domains: [Shop.VN]\n  file_roots: [/srv/releases/]\n  repo_roots: [relative, /srv/repos]\n", encoding="utf-8")
    cfg = PolicyConfig.load(f)
    assert cfg.params.http_allow_domains == ["shop.vn"] and cfg.params.file_roots == ["/srv/releases"] and cfg.params.repo_roots == ["/srv/repos"]


async def test_event_gateway_puts_intent_entities_on_task():
    from zeus.gateway.gateway import EventGateway
    from zeus.gateway.store import InMemoryControlStore
    from zeus.risk.engine import DefaultRiskEngine

    gw = EventGateway(InMemoryControlStore(), DefaultIntentEngine(), DefaultRiskEngine())
    ev = Event(channel=Channel.INTERNAL, text="Kiểm tra backend API: https://shop.vn/health và tệp /srv/releases/a.tgz", external_id="ent-1")
    res = await gw.ingest(ev)
    assert res.task is not None and res.task.entities["url"] == "https://shop.vn/health" and res.task.entities["path"] == "/srv/releases/a.tgz"
    assert all(isinstance(v, str) and len(v) <= 2000 for v in res.task.entities.values())
