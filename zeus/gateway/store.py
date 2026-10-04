"""Kho control-plane: events (idempotency), tasks, task_nodes, route_decisions. In-memory và Postgres (migration 101)."""

from __future__ import annotations

import json
from typing import Any, Protocol

from zeus.contracts.models import Event, RouteDecision, Task, TaskNode, TaskStatus, utcnow


class UnknownTenant(ValueError):
    pass


class ControlStore(Protocol):
    async def put_event(self, event: Event) -> tuple[Event, bool]:
        """Trả (event đã lưu, is_new). Trùng dedupe_key => (event cũ, False)."""

    async def get_event(self, tenant_id: str, event_id: str) -> Event | None: ...

    async def event_task_id(self, tenant_id: str, event_id: str) -> str | None: ...

    async def put_task(self, task: Task) -> None: ...

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None: ...

    async def list_tasks(self, tenant_id: str, status: TaskStatus | None = None, limit: int = 50) -> list[Task]: ...

    async def set_task_status(self, task_id: str, status: TaskStatus) -> None: ...

    async def save_node(self, task_id: str, node: TaskNode, attempts: int = 0, result: dict[str, Any] | None = None) -> None: ...

    async def list_nodes(self, task_id: str) -> list[dict[str, Any]]: ...

    async def save_route(self, route: RouteDecision, tenant_id: str, task_id: str | None, request_id: str | None) -> None: ...

    async def list_routes(self, tenant_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...


class InMemoryControlStore:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], Event] = {}
        self.event_ids: dict[str, Event] = {}
        self.event_task: dict[str, str] = {}
        self.tasks: dict[str, Task] = {}
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.routes: list[dict[str, Any]] = []

    async def put_event(self, event: Event) -> tuple[Event, bool]:
        key = (event.tenant_id, event.dedupe_key())
        if key in self.events:
            return self.events[key], False
        self.events[key] = event
        self.event_ids[event.event_id] = event
        return event, True

    async def get_event(self, tenant_id: str, event_id: str) -> Event | None:
        e = self.event_ids.get(event_id)
        return e if e and e.tenant_id == tenant_id else None

    async def event_task_id(self, tenant_id: str, event_id: str) -> str | None:
        return self.event_task.get(event_id)

    async def put_task(self, task: Task) -> None:
        self.tasks[task.task_id] = task
        if task.event_id:
            self.event_task[task.event_id] = task.task_id

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None:
        t = self.tasks.get(task_id)
        return t if t and t.tenant_id == tenant_id else None

    async def list_tasks(self, tenant_id: str, status: TaskStatus | None = None, limit: int = 50) -> list[Task]:
        rows = [t for t in self.tasks.values() if t.tenant_id == tenant_id and (status is None or t.status is status)]
        return sorted(rows, key=lambda t: t.created_at, reverse=True)[:limit]

    async def set_task_status(self, task_id: str, status: TaskStatus) -> None:
        if task_id in self.tasks:
            self.tasks[task_id] = self.tasks[task_id].model_copy(update={"status": status})

    async def save_node(self, task_id: str, node: TaskNode, attempts: int = 0, result: dict[str, Any] | None = None) -> None:
        self.nodes[(task_id, node.node_id)] = {"node_id": node.node_id, "status": node.status.value, "attempts": attempts, "data": node.model_dump(mode="json"), "result": result}

    async def list_nodes(self, task_id: str) -> list[dict[str, Any]]:
        return [v for (t, _), v in self.nodes.items() if t == task_id]

    async def save_route(self, route: RouteDecision, tenant_id: str, task_id: str | None, request_id: str | None) -> None:
        self.routes.append({"route": route, "tenant_id": tenant_id, "task_id": task_id, "request_id": request_id})

    async def list_routes(self, tenant_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self.routes if tenant_id is None or r["tenant_id"] == tenant_id]
        return rows[-limit:]


class PgControlStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def _conn(self):  # noqa: ANN202
        from zeus.storage.db import aconnect

        return await aconnect(self.dsn, autocommit=True)

    async def put_event(self, event: Event) -> tuple[Event, bool]:
        import psycopg

        try:
            async with await self._conn() as conn:
                cur = await conn.execute(
                    "INSERT INTO events (event_id, tenant_id, dedupe_key, channel, kind, untrusted, received_at, payload)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (tenant_id, dedupe_key) DO NOTHING RETURNING event_id",
                    (event.event_id, event.tenant_id, event.dedupe_key(), event.channel.value, event.kind.value, event.untrusted, event.received_at, event.model_dump_json()),
                )
                if await cur.fetchone():
                    return event, True
                cur = await conn.execute("SELECT payload FROM events WHERE tenant_id=%s AND dedupe_key=%s", (event.tenant_id, event.dedupe_key()))
                row = await cur.fetchone()
                return Event.model_validate(row["payload"]), False  # type: ignore[index]
        except psycopg.errors.ForeignKeyViolation as exc:
            raise UnknownTenant(event.tenant_id) from exc

    async def get_event(self, tenant_id: str, event_id: str) -> Event | None:
        async with await self._conn() as conn:
            cur = await conn.execute("SELECT payload FROM events WHERE tenant_id=%s AND event_id=%s", (tenant_id, event_id))
            row = await cur.fetchone()
        return Event.model_validate(row["payload"]) if row else None

    async def event_task_id(self, tenant_id: str, event_id: str) -> str | None:
        async with await self._conn() as conn:
            cur = await conn.execute("SELECT task_id FROM events WHERE tenant_id=%s AND event_id=%s", (tenant_id, event_id))
            row = await cur.fetchone()
        return row["task_id"] if row else None

    async def put_task(self, task: Task) -> None:
        async with await self._conn() as conn:
            await conn.execute(
                "INSERT INTO tasks (task_id, tenant_id, family, goal, risk, status, event_id, workflow_id, created_at, data)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
                " ON CONFLICT (task_id) DO UPDATE SET family=EXCLUDED.family, goal=EXCLUDED.goal, risk=EXCLUDED.risk,"
                " status=EXCLUDED.status, workflow_id=EXCLUDED.workflow_id, data=EXCLUDED.data, updated_at=now()",
                (task.task_id, task.tenant_id, task.family.value, task.goal, task.risk.value, task.status.value, task.event_id, task.workflow_id, task.created_at, task.model_dump_json()),
            )
            if task.event_id:
                await conn.execute("UPDATE events SET task_id=%s WHERE event_id=%s", (task.task_id, task.event_id))

    @staticmethod
    def _task(row: dict) -> Task:
        return Task.model_validate(row["data"]).model_copy(update={"status": TaskStatus(row["status"])})

    async def pending_tasks(self, older_than_s: float, limit: int = 50) -> list[Task]:
        """Task còn PENDING quá lâu (workflow có thể chưa từng được start): để sweeper start lại (idempotent theo workflow id)."""
        async with await self._conn() as conn:
            cur = await conn.execute(
                "SELECT data, status FROM tasks WHERE status='PENDING' AND created_at < now() - make_interval(secs => %s) ORDER BY created_at LIMIT %s",
                (older_than_s, limit),
            )
            return [self._task(r) for r in await cur.fetchall()]

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None:
        async with await self._conn() as conn:
            cur = await conn.execute("SELECT data, status FROM tasks WHERE tenant_id=%s AND task_id=%s", (tenant_id, task_id))
            row = await cur.fetchone()
        return self._task(row) if row else None

    async def list_tasks(self, tenant_id: str, status: TaskStatus | None = None, limit: int = 50) -> list[Task]:
        sql, args = "SELECT data, status FROM tasks WHERE tenant_id=%s", [tenant_id]
        if status is not None:
            sql += " AND status=%s"
            args.append(status.value)
        async with await self._conn() as conn:
            cur = await conn.execute(sql + " ORDER BY created_at DESC LIMIT %s", [*args, limit])
            return [self._task(r) for r in await cur.fetchall()]

    async def set_task_status(self, task_id: str, status: TaskStatus) -> None:
        async with await self._conn() as conn:
            await conn.execute("UPDATE tasks SET status=%s, updated_at=%s WHERE task_id=%s", (status.value, utcnow(), task_id))

    async def save_node(self, task_id: str, node: TaskNode, attempts: int = 0, result: dict[str, Any] | None = None) -> None:
        async with await self._conn() as conn:
            await conn.execute(
                "INSERT INTO task_nodes (task_id, node_id, status, attempts, started_at, finished_at, data, result)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)"
                " ON CONFLICT (task_id, node_id) DO UPDATE SET status=EXCLUDED.status, attempts=EXCLUDED.attempts,"
                " finished_at=EXCLUDED.finished_at, data=EXCLUDED.data, result=EXCLUDED.result",
                (
                    task_id, node.node_id, node.status.value, attempts, utcnow(),
                    utcnow() if node.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.ROLLED_BACK) else None,
                    node.model_dump_json(), json.dumps(result, default=str) if result is not None else None,
                ),
            )

    async def list_nodes(self, task_id: str) -> list[dict[str, Any]]:
        async with await self._conn() as conn:
            cur = await conn.execute("SELECT node_id, status, attempts, data, result FROM task_nodes WHERE task_id=%s ORDER BY node_id", (task_id,))
            return list(await cur.fetchall())

    async def save_route(self, route: RouteDecision, tenant_id: str, task_id: str | None, request_id: str | None) -> None:
        async with await self._conn() as conn:
            await conn.execute(
                "INSERT INTO route_decisions (route_id, tenant_id, task_id, request_id, task_family, provider, model, strategy, decided_at, data)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (route_id) DO NOTHING",
                (route.route_id, tenant_id, task_id, request_id, route.task_family.value, route.provider.value, route.model, route.strategy.value, route.decided_at, route.model_dump_json()),
            )

    async def list_routes(self, tenant_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql, args = "SELECT route_id, tenant_id, task_id, request_id, data FROM route_decisions", []
        if tenant_id:
            sql += " WHERE tenant_id=%s"
            args.append(tenant_id)
        async with await self._conn() as conn:
            cur = await conn.execute(sql + " ORDER BY decided_at DESC LIMIT %s", [*args, limit])
            return [{**r, "route": RouteDecision.model_validate(r["data"])} for r in await cur.fetchall()]
