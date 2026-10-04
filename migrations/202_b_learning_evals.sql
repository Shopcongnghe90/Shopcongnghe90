-- 202_b_learning_evals.sql — Workstream B: Learning Plane + Evaluation Engine.

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id         text PRIMARY KEY,
    tenant_id          text        NOT NULL REFERENCES tenants(tenant_id),
    evidence_record_id text        NOT NULL UNIQUE REFERENCES evidence_records(record_id),
    task_id            text        NOT NULL,
    task_family        text        NOT NULL,
    provider           text        NOT NULL,
    model              text        NOT NULL,
    prompt_version     text        NOT NULL DEFAULT '',
    worker_id          text,
    outcome            text        NOT NULL CHECK (outcome IN ('VERIFIED_SUCCESS','VERIFIED_FAILURE','UNVERIFIED')),
    cost_usd           double precision NOT NULL DEFAULT 0,
    latency_ms         integer     NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outcomes_family_idx ON outcomes (tenant_id, task_family);

CREATE TABLE IF NOT EXISTS dataset_records (
    record_id          text PRIMARY KEY,
    tenant_id          text        NOT NULL REFERENCES tenants(tenant_id),
    stage              text        NOT NULL CHECK (stage IN ('RAW','NORMALIZED','VERIFIED','CURATED')),
    task_family        text        NOT NULL,
    input              jsonb       NOT NULL DEFAULT '{}'::jsonb,
    output             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    evidence_record_id text,
    outcome            text        NOT NULL CHECK (outcome IN ('VERIFIED_SUCCESS','VERIFIED_FAILURE','UNVERIFIED')),
    labels             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    pii_redacted       boolean     NOT NULL DEFAULT false,
    source             text        NOT NULL DEFAULT 'runtime',
    promoted_from      text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (stage NOT IN ('VERIFIED','CURATED') OR (outcome <> 'UNVERIFIED' AND evidence_record_id IS NOT NULL)),
    CHECK (stage <> 'CURATED' OR pii_redacted)
);
CREATE INDEX IF NOT EXISTS dataset_records_stage_idx ON dataset_records (tenant_id, stage, task_family);

CREATE TABLE IF NOT EXISTS router_stats (
    tenant_id        text NOT NULL REFERENCES tenants(tenant_id),
    task_family      text NOT NULL,
    provider         text NOT NULL,
    model            text NOT NULL,
    prompt_version   text NOT NULL DEFAULT '',
    n                integer NOT NULL DEFAULT 0,
    verified_success integer NOT NULL DEFAULT 0,
    verified_failure integer NOT NULL DEFAULT 0,
    unverified       integer NOT NULL DEFAULT 0,
    total_cost_usd   double precision NOT NULL DEFAULT 0,
    total_latency_ms bigint NOT NULL DEFAULT 0,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, task_family, provider, model, prompt_version)
);

CREATE TABLE IF NOT EXISTS champion_challenger (
    subject_id   text NOT NULL,
    tenant_id    text NOT NULL REFERENCES tenants(tenant_id),
    subject_kind text NOT NULL CHECK (subject_kind IN ('route','prompt','playbook','scheduler')),
    task_family  text,
    risk         text NOT NULL DEFAULT 'R0' CHECK (risk IN ('R0','R1','R2','R3')),
    champion     text NOT NULL,
    challenger   text,
    state        text NOT NULL,
    history      jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject_id)
);

CREATE TABLE IF NOT EXISTS eval_datasets (
    tenant_id   text NOT NULL REFERENCES tenants(tenant_id),
    family      text NOT NULL,
    version     text NOT NULL,
    sha256      text NOT NULL,
    n_cases     integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, family, version)
);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id      text PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(tenant_id),
    candidate   text NOT NULL,
    n_cases     integer NOT NULL,
    n_passed    integer NOT NULL,
    cost_usd    double precision NOT NULL DEFAULT 0,
    report      jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_results (
    run_id     text NOT NULL REFERENCES eval_runs(run_id),
    case_id    text NOT NULL,
    tenant_id  text NOT NULL REFERENCES tenants(tenant_id),
    family     text NOT NULL,
    passed     boolean NOT NULL,
    detail     text NOT NULL DEFAULT '',
    latency_ms integer NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, case_id)
);
