-- 101_a_control_plane.sql — Workstream A. Bất biến sau khi áp dụng.
-- events (idempotency), tasks, task_nodes, approvals, route_decisions, budget_ledger.

CREATE TABLE IF NOT EXISTS events (
    event_id     text PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants(tenant_id),
    dedupe_key   text        NOT NULL,
    channel      text        NOT NULL,
    kind         text        NOT NULL,
    untrusted    boolean     NOT NULL DEFAULT true,
    received_at  timestamptz NOT NULL DEFAULT now(),
    payload      jsonb       NOT NULL,
    task_id      text,
    UNIQUE (tenant_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id      text PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants(tenant_id),
    family       text        NOT NULL,
    goal         text        NOT NULL,
    risk         text        NOT NULL CHECK (risk IN ('R0','R1','R2','R3')),
    status       text        NOT NULL,
    event_id     text,
    workflow_id  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    data         jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_tenant_status_idx ON tasks (tenant_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS task_nodes (
    task_id      text NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    node_id      text NOT NULL,
    status       text NOT NULL,
    attempts     integer NOT NULL DEFAULT 0,
    started_at   timestamptz,
    finished_at  timestamptz,
    data         jsonb NOT NULL DEFAULT '{}'::jsonb,
    result       jsonb,
    PRIMARY KEY (task_id, node_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id  text PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants(tenant_id),
    task_id      text,
    action_id    text        NOT NULL,
    risk         text        NOT NULL CHECK (risk IN ('R0','R1','R2','R3')),
    status       text        NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    decided_by   text,
    decided_at   timestamptz,
    comment      text,
    data         jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_tenant_status_idx ON approvals (tenant_id, status);
CREATE INDEX IF NOT EXISTS approvals_action_idx ON approvals (action_id);

CREATE TABLE IF NOT EXISTS route_decisions (
    route_id     text PRIMARY KEY,
    tenant_id    text        NOT NULL DEFAULT 'zeusvn',
    task_id      text,
    request_id   text,
    task_family  text        NOT NULL,
    provider     text        NOT NULL,
    model        text        NOT NULL,
    strategy     text        NOT NULL,
    decided_at   timestamptz NOT NULL DEFAULT now(),
    data         jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS route_decisions_task_idx ON route_decisions (task_id);

CREATE TABLE IF NOT EXISTS budget_ledger (
    entry_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     text        NOT NULL REFERENCES tenants(tenant_id),
    task_id       text,
    request_id    text,
    provider      text        NOT NULL,
    model         text        NOT NULL,
    input_tokens  integer     NOT NULL DEFAULT 0,
    output_tokens integer     NOT NULL DEFAULT 0,
    usd           numeric(14,8) NOT NULL CHECK (usd >= 0),
    occurred_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS budget_ledger_tenant_time_idx ON budget_ledger (tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS budget_ledger_task_idx ON budget_ledger (task_id);
