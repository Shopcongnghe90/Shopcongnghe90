"""Phase 2 (3): pool kết nối Postgres async dùng chung + index pgvector HNSW (migration 203). PostgreSQL 16 + pgvector thật."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import psycopg
import pytest

from zeus.brain.embeddings import HashingEmbedding
from zeus.brain.retrieval import PgBrainRetriever, hnsw_index_name
from zeus.brain.store import PgMemoryStore
from zeus.config import Settings
from zeus.contracts.models import MemoryItem, MemoryKind, RetrievalQuery
from zeus.storage import AsyncPool, PoolTimeout, aconnect, close_pool, get_pool, open_pool
from zeus.storage.migrate import apply_migrations

pytestmark = [pytest.mark.pg]
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def dsn(pg_dsn: str) -> str:
    apply_migrations(pg_dsn, REPO / "migrations")
    return pg_dsn


@pytest.fixture()
async def pool(dsn: str):
    p = await open_pool(dsn, max_size=3, acquire_timeout_s=0.5, check_after_s=0.0)
    try:
        yield p
    finally:
        await close_pool(p)


# ------------------------------------------------------------------ pool
async def test_connections_are_reused_instead_of_opened_per_operation(dsn, pool):
    for _ in range(25):
        async with await aconnect(dsn, autocommit=True) as c:
            assert (await (await c.execute("SELECT 1 AS x")).fetchone()) == {"x": 1}
    s = pool.stats()
    assert s["created"] == 1 and s["reused"] == 24 and s["idle"] == 1 and s["in_use"] == 0


async def test_pool_bounds_concurrency_and_times_out_when_exhausted(dsn, pool):
    held = [await aconnect(dsn, autocommit=True) for _ in range(3)]
    assert pool.stats()["in_use"] == 3
    with pytest.raises(PoolTimeout):
        await aconnect(dsn, autocommit=True)  # max_size=3: không mở kết nối thứ 4
    await held[0].release()
    async with await aconnect(dsn, autocommit=True) as c:  # chỗ trống => lấy được ngay (tái dùng kết nối vừa trả)
        await c.execute("SELECT 1")
    for h in held[1:]:
        await h.release()
    assert pool.stats()["created"] == 3 and pool.stats()["in_use"] == 0 and pool.stats()["timeouts"] == 1


async def test_many_concurrent_store_operations_share_few_connections(dsn):
    from zeus.contracts.models import ApprovalRequest, RiskLevel, TypedAction
    from zeus.policy.approvals import PgApprovalStore

    p = await open_pool(dsn, max_size=4, acquire_timeout_s=20)
    try:
        store = PgApprovalStore(dsn)
        reqs = [ApprovalRequest(action=TypedAction(name="x.y"), risk=RiskLevel.R2, summary_vi=f"s{i}") for i in range(60)]
        await asyncio.gather(*(store.request(r) for r in reqs))
        got = await asyncio.gather(*(store.get(r.approval_id) for r in reqs))
        assert all(g is not None for g in got) and len(await store.list("zeusvn")) == 60
        s = p.stats()
        assert s["created"] <= 4 and s["reused"] >= 100 and s["in_use"] == 0  # 121+ thao tác, tối đa 4 kết nối (trước: mỗi thao tác một kết nối)
    finally:
        await close_pool(p)


async def test_transaction_semantics_match_plain_connection(dsn, pool):
    async with await aconnect(dsn, autocommit=True) as c:
        await c.execute("CREATE TABLE t_pool (id int)")
    async with await aconnect(dsn, autocommit=False) as c:  # thoát bình thường => commit
        await c.execute("INSERT INTO t_pool VALUES (1)")
    with pytest.raises(RuntimeError):
        async with await aconnect(dsn, autocommit=False) as c:  # exception => rollback
            await c.execute("INSERT INTO t_pool VALUES (2)")
            raise RuntimeError("boom")
    with pytest.raises(psycopg.errors.UndefinedTable):  # giao dịch bị huỷ (aborted) không rò sang lần mượn sau
        async with await aconnect(dsn, autocommit=False) as c:
            await c.execute("SELECT * FROM khong_co_bang")
    async with await aconnect(dsn, autocommit=True) as c:
        assert [r["id"] for r in await (await c.execute("SELECT id FROM t_pool ORDER BY id")).fetchall()] == [1]
        assert c.autocommit is True  # chế độ autocommit theo từng lần mượn, không dính lần trước
    async with await aconnect(dsn, autocommit=True) as c:
        await c.execute("BEGIN")  # caller bỏ quên transaction mở
        await c.execute("INSERT INTO t_pool VALUES (3)")
    async with await aconnect(dsn, autocommit=True) as c:
        assert [r["id"] for r in await (await c.execute("SELECT id FROM t_pool ORDER BY id")).fetchall()] == [1]  # đã rollback khi trả về


async def test_dead_connection_is_replaced_transparently(dsn, pool):
    async with await aconnect(dsn, autocommit=True) as c:
        pid = (await (await c.execute("SELECT pg_backend_pid() AS p")).fetchone())["p"]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    await asyncio.sleep(0.2)
    async with await aconnect(dsn, autocommit=True) as c:  # kết nối nghỉ bị server giết: pool phát hiện (SELECT 1) và mở cái mới
        assert (await (await c.execute("SELECT 1 AS x")).fetchone()) == {"x": 1}
    assert pool.stats()["created"] == 2 and pool.stats()["discarded"] >= 1


async def test_cancelled_query_connection_is_discarded_not_reused(dsn, pool):
    async def slow() -> None:
        async with await aconnect(dsn, autocommit=True) as c:
            await c.execute("SELECT pg_sleep(30)")

    t = asyncio.create_task(slow())
    await asyncio.sleep(0.3)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert pool.stats()["in_use"] == 0 and pool.stats()["idle"] == 0  # không trả kết nối đang dở truy vấn về pool
    async with await aconnect(dsn, autocommit=True) as c:
        assert (await (await c.execute("SELECT 1 AS x")).fetchone()) == {"x": 1}


async def test_idle_and_lifetime_expiry(dsn):
    p = await open_pool(dsn, max_size=2, max_idle_s=0.05)
    try:
        async with await aconnect(dsn, autocommit=True) as c:
            await c.execute("SELECT 1")
        await asyncio.sleep(0.15)
        async with await aconnect(dsn, autocommit=True) as c:
            await c.execute("SELECT 1")
        assert p.stats()["created"] == 2 and p.stats()["discarded"] == 1
    finally:
        await close_pool(p)


async def test_pool_only_serves_its_own_event_loop_and_closed_pool_falls_back(dsn):
    p = await open_pool(dsn, max_size=2)
    try:
        assert get_pool(dsn) is p and await open_pool(dsn) is p  # idempotent
        out: list[int] = []

        def other_loop() -> None:  # loop khác (thread khác): kết nối psycopg async không dùng chéo loop => mở kết nối riêng
            async def go() -> None:
                async with await aconnect(dsn, autocommit=True) as c:
                    out.append((await (await c.execute("SELECT 1 AS x")).fetchone())["x"])

            asyncio.run(go())

        th = threading.Thread(target=other_loop)
        th.start()
        th.join(20)
        assert out == [1] and p.stats()["created"] == 0
    finally:
        await close_pool(p)
    async with await aconnect(dsn, autocommit=True) as c:  # đã đóng pool => kết nối trần
        await c.execute("SELECT 1")


async def test_settings_and_system_pool_toggle(dsn, tmp_path):
    from zeus.app.system import build_system

    assert Settings.from_env({"ZEUS_DB_POOL_MAX": "7"}).db_pool_max == 7 and Settings().db_pool_max == 10
    with pytest.raises(ValueError):
        Settings.from_env({"ZEUS_DB_POOL_MAX": "-1"})
    common = dict(env="test", db_dsn=dsn, data_dir=tmp_path, models_config=REPO / "config/models.yaml", policy_config=REPO / "config/policy.yaml",
                  brain_config=REPO / "config/brain.yaml", workers_config=REPO / "config/workers.yaml", channels_config=REPO / "config/channels.yaml")
    off = build_system(Settings(**common, db_pool_max=0), env={})
    assert await off.open_pool() is None and get_pool(dsn) is None
    on = build_system(Settings(**common, db_pool_max=2), env={})
    p = await on.open_pool()
    try:
        assert p is get_pool(dsn) and p.max_size == 2 and await on.open_pool() is p
        assert len(await on.control.approvals.list("zeusvn")) == 0 and p.stats()["created"] >= 1  # store thật đi qua pool
    finally:
        await close_pool(p)


# ------------------------------------------------------------------ HNSW
def _items(n: int) -> list[MemoryItem]:
    topics = ["hoá đơn", "giao hàng", "đổi trả", "bảo hành", "tên miền", "triển khai", "sao lưu", "báo cáo"]
    # mỗi ghi chú có từ khoá riêng (ma{i}k{i*37 % 101}) để điểm vector khác nhau: so sánh thứ tự giữa index và quét chính xác không bị hoà điểm
    return [MemoryItem(kind=MemoryKind.SEMANTIC, title=f"Ghi chú {i}", content=f"Quy trình xử lý {topics[i % len(topics)]} ma{i} k{i * 37 % 101} z{i * i % 89} cho khách hàng") for i in range(n)]


async def test_migration_creates_hnsw_index_and_retrieval_matches_exact_scan(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        idx = c.execute("SELECT indexdef FROM pg_indexes WHERE indexname=%s", (hnsw_index_name(256),)).fetchone()
        assert idx and "USING hnsw" in idx[0] and "vector_cosine_ops" in idx[0] and "vector_dims" in idx[0]
    emb = HashingEmbedding(dim=256)
    store = PgMemoryStore(dsn, emb)
    for it in _items(120):
        await store.put(it)
    retr = PgBrainRetriever(store, candidate_k=10)
    q = RetrievalQuery(tenant_id="zeusvn", text="quy trình sao lưu dữ liệu", top_k=5)
    with_index = [h.memory.memory_id for h in await retr.retrieve(q)]
    assert retr._hnsw_cache is not None and retr._hnsw_cache[1] is True  # dùng dạng truy vấn theo index
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"DROP INDEX {hnsw_index_name(256)}")
    retr2 = PgBrainRetriever(store, candidate_k=10)
    exact = [h.memory.memory_id for h in await retr2.retrieve(q)]
    assert retr2._hnsw_cache is not None and retr2._hnsw_cache[1] is False and with_index == exact and len(exact) == 5


async def test_planner_uses_hnsw_for_the_retriever_query_shape(dsn):
    emb = HashingEmbedding(dim=256)
    store = PgMemoryStore(dsn, emb)
    for it in _items(80):
        await store.put(it)
    qv = "[" + ",".join(str(x) for x in (await emb.embed(["sao lưu"]))[0]) + "]"
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in c.execute(
            "EXPLAIN SELECT m.memory_id FROM memory_items m WHERE vector_dims(m.embedding) = 256 AND m.embedding_model=%s AND m.tenant_id=%s "
            "ORDER BY (m.embedding::vector(256)) <=> %s::vector(256) LIMIT 10", (emb.model_name, "zeusvn", qv)).fetchall())
    assert hnsw_index_name(256) in plan, plan


async def test_other_dimensions_do_not_break_inserts_and_ensure_index_is_idempotent(dsn):
    store384 = PgMemoryStore(dsn, HashingEmbedding(dim=384))
    await store384.put(MemoryItem(kind=MemoryKind.SEMANTIC, title="a", content="vector 384 chiều không được làm hỏng index 256"))  # index một phần: không ép cast
    assert await store384.ensure_hnsw_index(384, concurrently=False) is True and await store384.ensure_hnsw_index(384, concurrently=False) is True
    with psycopg.connect(dsn, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM pg_indexes WHERE indexname=%s", (hnsw_index_name(384),)).fetchone()[0] == 1
    retr = PgBrainRetriever(store384, candidate_k=5)
    hits = await retr.retrieve(RetrievalQuery(tenant_id="zeusvn", text="vector 384 chiều", top_k=3))
    assert hits and retr._hnsw_cache == (384, True, retr._hnsw_cache[2])
    with pytest.raises(ValueError):
        await store384.ensure_hnsw_index(0)
