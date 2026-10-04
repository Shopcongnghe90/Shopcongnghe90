"""Pool kết nối PostgreSQL async tối giản (psycopg 3), dùng chung cho mọi store qua ``zeus.storage.db.aconnect``.

``psycopg_pool`` không có trong venv/deps của dự án nên tự viết phần tối thiểu cần thiết (ADR-022):
- tối đa ``max_size`` kết nối đang dùng + nhàn rỗi (semaphore); hết chỗ thì chờ ``acquire_timeout_s`` rồi ném ``PoolTimeout``;
- kết nối nhàn rỗi quá ``max_idle_s`` hoặc sống quá ``max_lifetime_s`` bị đóng; nghỉ lâu (> ``check_after_s``) được ``SELECT 1`` trước khi cấp;
- trả về pool: giao dịch dở bị rollback, kết nối hỏng/bị huỷ giữa chừng (CancelledError) bị đóng thay vì tái dùng;
- gắn với MỘT event loop (kết nối psycopg async không dùng chéo loop): ngoài loop đó ``aconnect`` quay về mở kết nối riêng.

Lease trả từ ``acquire`` dùng như ``async with await aconnect(dsn) as conn``: thoát khối ``with`` thì commit (nếu không autocommit)
hoặc rollback (nếu có exception) rồi trả kết nối về pool — cùng ngữ nghĩa với ``async with AsyncConnection``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


class PoolTimeout(TimeoutError):
    """Không lấy được kết nối trong thời hạn (pool cạn): thường là lỗi giữ kết nối quá lâu hoặc max_size quá nhỏ."""


class PoolClosed(RuntimeError):
    pass


class _Entry:
    __slots__ = ("conn", "created", "last_used")

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self.conn = conn
        self.created = self.last_used = time.monotonic()


class PooledConnection:
    """Lease một kết nối. ``async with`` trả về ``psycopg.AsyncConnection`` thật; thoát khối thì trả về pool."""

    def __init__(self, pool: "AsyncPool", entry: _Entry, autocommit: bool) -> None:
        self._pool, self._entry, self._autocommit = pool, entry, autocommit
        self._released = False

    @property
    def connection(self) -> psycopg.AsyncConnection:
        return self._entry.conn

    async def __aenter__(self) -> psycopg.AsyncConnection:
        return self._entry.conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.release(exc_type, exc)

    async def release(self, exc_type: Any = None, exc: Any = None) -> None:
        if self._released:
            return
        self._released = True
        await self._pool._release(self._entry, self._autocommit, exc_type, exc)

    async def close(self) -> None:  # tương thích code cũ gọi conn.close()
        await self.release()

    def __getattr__(self, name: str) -> Any:  # dùng lease như kết nối (execute/cursor/...) nếu chưa vào khối with
        return getattr(self._entry.conn, name)


class AsyncPool:
    def __init__(
        self, dsn: str, *, max_size: int = 10, max_idle_s: float = 300.0, max_lifetime_s: float = 1800.0,
        acquire_timeout_s: float = 30.0, check_after_s: float = 30.0, connect_timeout: int | None = None,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size phải >= 1")
        self.dsn, self.max_size = dsn, max_size
        self.max_idle_s, self.max_lifetime_s, self.acquire_timeout_s, self.check_after_s = max_idle_s, max_lifetime_s, acquire_timeout_s, check_after_s
        self.connect_timeout = connect_timeout
        self.loop: asyncio.AbstractEventLoop | None = None
        self._sem: asyncio.Semaphore | None = None
        self._idle: collections.deque[_Entry] = collections.deque()
        self._closed = False
        self.stats_created = self.stats_reused = self.stats_discarded = self.stats_timeouts = 0
        self.in_use = 0

    # ------------------------------------------------------------------ vòng đời
    async def open(self) -> "AsyncPool":
        self.loop = asyncio.get_running_loop()
        self._sem = asyncio.Semaphore(self.max_size)
        self._closed = False
        return self

    @property
    def closed(self) -> bool:
        return self._closed

    def usable_here(self) -> bool:
        """Pool dùng được ở loop hiện tại (kết nối async không dùng chéo loop)."""
        if self._closed or self.loop is None or self.loop.is_closed():
            return False
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    async def close(self) -> None:
        self._closed = True
        while self._idle:
            await self._discard(self._idle.popleft())

    def stats(self) -> dict[str, int]:
        return {
            "max_size": self.max_size, "idle": len(self._idle), "in_use": self.in_use, "created": self.stats_created,
            "reused": self.stats_reused, "discarded": self.stats_discarded, "timeouts": self.stats_timeouts,
        }

    # ------------------------------------------------------------------ cấp / nhận lại
    async def _connect(self) -> _Entry:
        from zeus.storage.db import connect_timeout_s

        conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=self.connect_timeout or connect_timeout_s())
        self.stats_created += 1
        return _Entry(conn)

    async def _discard(self, e: _Entry) -> None:
        self.stats_discarded += 1
        with contextlib.suppress(Exception):
            await e.conn.close()

    async def _healthy(self, e: _Entry, now: float) -> bool:
        c = e.conn
        if c.closed or c.broken or now - e.created > self.max_lifetime_s or now - e.last_used > self.max_idle_s:
            return False
        if now - e.last_used > self.check_after_s:
            try:
                await c.execute("SELECT 1")
                if c.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:  # kết nối nghỉ ở chế độ không-autocommit: ping mở transaction
                    await c.rollback()
            except Exception:  # noqa: BLE001 - server đã đóng kết nối nghỉ lâu
                return False
        return True

    async def acquire(self, autocommit: bool = False) -> PooledConnection:
        if self._closed or self._sem is None:
            raise PoolClosed("pool đã đóng hoặc chưa mở")
        try:
            await asyncio.wait_for(self._sem.acquire(), self.acquire_timeout_s)
        except asyncio.TimeoutError:
            self.stats_timeouts += 1
            raise PoolTimeout(f"hết kết nối trong {self.acquire_timeout_s}s (max_size={self.max_size}, in_use={self.in_use})") from None
        entry: _Entry | None = None
        try:
            while self._idle:
                cand = self._idle.pop()  # LIFO: ưu tiên kết nối mới dùng, để kết nối cũ hết hạn dần
                if await self._healthy(cand, time.monotonic()):
                    entry = cand
                    self.stats_reused += 1
                    break
                await self._discard(cand)
            if entry is None:
                entry = await self._connect()
            if entry.conn.autocommit != autocommit:
                await entry.conn.set_autocommit(autocommit)
        except BaseException:
            if entry is not None:
                await self._discard(entry)
            self._sem.release()
            raise
        self.in_use += 1
        return PooledConnection(self, entry, autocommit)

    async def _release(self, e: _Entry, autocommit: bool, exc_type: Any, exc: Any) -> None:
        assert self._sem is not None
        reuse = True
        try:
            c = e.conn
            if c.closed or c.broken:
                reuse = False
            elif exc_type is not None and not issubclass(exc_type, Exception):
                reuse = False  # bị huỷ (CancelledError/KeyboardInterrupt) giữa truy vấn: trạng thái giao thức không chắc chắn
            else:
                try:
                    if exc_type is None and not autocommit:
                        await c.commit()
                    elif c.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                        await c.rollback()
                except Exception:  # noqa: BLE001
                    if exc_type is None:
                        raise  # lỗi commit phải đến tay caller như khi dùng kết nối trần
                    reuse = False
        except BaseException:
            reuse = False
            raise
        finally:
            self.in_use -= 1
            try:
                if reuse and not self._closed and not e.conn.closed:
                    e.last_used = time.monotonic()
                    self._idle.append(e)
                else:
                    await self._discard(e)
            finally:
                self._sem.release()
