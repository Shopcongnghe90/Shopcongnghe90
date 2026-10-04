-- 301_c_workers.sql — Workstream C: worker registry, token, heartbeat, inventory, assignment queue, schedule decisions.
-- FK chỉ trỏ vào tenants (0xx); task_id/node_id là id logic (text), không FK sang workstream khác.

CREATE TABLE IF NOT EXISTS workers (
    worker_id        text PRIMARY KEY,
    kind             text   NOT NULL,
    tenant_scope     text[] NOT NULL DEFAULT ARRAY['zeusvn'],
    capabilities     jsonb  NOT NULL DEFAULT '[]'::jsonb,
    status           text   NOT NULL DEFAULT 'ONLINE' CHECK (status IN ('ONLINE','DEGRADED','DRAINING','OFFLINE')),
    network_zone     text   NOT NULL DEFAULT 'agent',
    data_localities  text[] NOT NULL DEFAULT '{}',
    version          text   NOT NULL DEFAULT '0.1.0',
    registered_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now()
);

-- Token riêng mỗi worker; chỉ lưu hash SHA-256. Phát hành trước khi worker register (admin).
CREATE TABLE IF NOT EXISTS worker_tokens (
    token_hash      text PRIMARY KEY,
    worker_id       text   NOT NULL,
    allowed_tenants text[] NOT NULL DEFAULT ARRAY['zeusvn'],
    created_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz
);
CREATE INDEX IF NOT EXISTS worker_tokens_worker_idx ON worker_tokens (worker_id);

CREATE TABLE IF NOT EXISTS worker_inventory (
    worker_id   text PRIMARY KEY REFERENCES workers(worker_id) ON DELETE CASCADE,
    inventory   jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Heartbeat mới nhất của mỗi worker (trạng thái hiện tại; lịch sử nằm ở metrics/trace).
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id   text PRIMARY KEY REFERENCES workers(worker_id) ON DELETE CASCADE,
    at          timestamptz NOT NULL,
    heartbeat   jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_decisions (
    decision_id    text PRIMARY KEY,
    tenant_id      text NOT NULL REFERENCES tenants(tenant_id),
    task_id        text NOT NULL,
    node_id        text,
    task_family    text NOT NULL,
    worker_id      text,
    score          double precision NOT NULL DEFAULT 0,
    features       jsonb,              -- vector 10 yếu tố của worker được chọn (nhãn học)
    weights        jsonb NOT NULL DEFAULT '{}'::jsonb,
    candidates     jsonb NOT NULL DEFAULT '[]'::jsonb,
    policy_version text NOT NULL,
    reason         text NOT NULL DEFAULT '',
    decided_at     timestamptz NOT NULL DEFAULT now(),
    outcome        text CHECK (outcome IS NULL OR outcome IN ('VERIFIED_SUCCESS','VERIFIED_FAILURE','UNVERIFIED'))
);
CREATE INDEX IF NOT EXISTS schedule_decisions_learn_idx ON schedule_decisions (task_family, worker_id) WHERE outcome IS NOT NULL;
CREATE INDEX IF NOT EXISTS schedule_decisions_task_idx ON schedule_decisions (task_id);

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id        text PRIMARY KEY,
    tenant_id            text NOT NULL REFERENCES tenants(tenant_id),
    task_id              text NOT NULL,
    node_id              text,
    worker_id            text,         -- NULL = chưa có worker (đang chờ scheduler)
    status               text NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','LEASED','COMPLETED','CANCELLED','FAILED')),
    payload              jsonb NOT NULL,   -- TaskAssignment
    task_token           bytea,            -- Temporal async activity token (ADR-011)
    attempt              integer NOT NULL DEFAULT 1,
    lease_expires_at     timestamptz,
    cancel_requested     boolean NOT NULL DEFAULT false,
    activity_completed   boolean NOT NULL DEFAULT false,
    schedule_decision_id text,
    sched_context        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {family,urgency,risk,required_capabilities} để xếp lịch lại
    enqueued_at          timestamptz NOT NULL DEFAULT now(),
    leased_at            timestamptz,
    finished_at          timestamptz
);
CREATE INDEX IF NOT EXISTS assignments_poll_idx ON assignments (worker_id, enqueued_at) WHERE status = 'QUEUED';
CREATE INDEX IF NOT EXISTS assignments_lease_idx ON assignments (lease_expires_at) WHERE status = 'LEASED';

CREATE TABLE IF NOT EXISTS assignment_results (
    assignment_id text PRIMARY KEY REFERENCES assignments(assignment_id),
    worker_id     text NOT NULL,
    result        jsonb NOT NULL,      -- AssignmentResult
    received_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS worker_artifacts (
    ref           text PRIMARY KEY,    -- artifact://<tenant>/<sha256>
    tenant_id     text NOT NULL REFERENCES tenants(tenant_id),
    sha256        text NOT NULL,
    size_bytes    bigint NOT NULL,
    mime          text NOT NULL,
    name          text,
    kind          text NOT NULL CHECK (kind IN ('log','artifact','evidence')),
    worker_id     text NOT NULL,
    assignment_id text NOT NULL,
    path          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
