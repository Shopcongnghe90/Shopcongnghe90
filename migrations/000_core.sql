-- 000_core.sql — SHARED (Phase 0). Bất biến sau khi áp dụng; thay đổi bằng migration 0xx mới.
-- Nội dung: extension vector (nếu có), tenants, audit_log append-only, trace_spans.

-- pgvector: tạo nếu gói đã cài trên server; không có thì bỏ qua (B kiểm tra lại ở migration 2xx).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'vector extension available but role lacks privilege; ask DBA to CREATE EXTENSION vector';
        END;
    ELSE
        RAISE NOTICE 'pgvector not installed; vector features disabled until installed';
    END IF;
END
$$;

-- Tenants: mọi bảng nghiệp vụ tham chiếu tenants(tenant_id). Cross-workstream FK chỉ được trỏ vào bảng 0xx.
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id     text PRIMARY KEY CHECK (tenant_id ~ '^[a-z0-9][a-z0-9_-]{1,62}$'),
    display_name  text        NOT NULL,
    kind          text        NOT NULL DEFAULT 'internal' CHECK (kind IN ('internal', 'customer')),
    status        text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
    data_region   text        NOT NULL DEFAULT 'vn',
    allow_cloud_llm boolean   NOT NULL DEFAULT false,  -- Luật BVDLCN: tenant phải opt-in, PII luôn che trước khi gửi
    created_at    timestamptz NOT NULL DEFAULT now(),
    metadata      jsonb       NOT NULL DEFAULT '{}'::jsonb
);

INSERT INTO tenants (tenant_id, display_name, kind, allow_cloud_llm)
VALUES ('zeusvn', 'ZEUS VN (nội bộ)', 'internal', true)
ON CONFLICT (tenant_id) DO NOTHING;

-- Audit log append-only: UPDATE/DELETE/TRUNCATE bị chặn bằng trigger.
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    tenant_id    text        NOT NULL REFERENCES tenants(tenant_id),
    actor        text        NOT NULL,           -- human:<id> | model:<provider/model> | worker:<id> | system
    action       text        NOT NULL,           -- vd 'approval.decided', 'tool.executed', 'policy.denied'
    subject_type text,
    subject_id   text,
    risk         text CHECK (risk IS NULL OR risk IN ('R0', 'R1', 'R2', 'R3')),
    trace_id     text,
    task_id      text,
    workflow_id  text,
    details      jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_tenant_time_idx ON audit_log (tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_log_trace_idx ON audit_log (trace_id);

CREATE OR REPLACE FUNCTION zeus_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only (% forbidden)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION zeus_forbid_mutation();

DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION zeus_forbid_mutation();

-- Trace spans (OTel data model). Retention do B quản lý (job xoá theo partition/time sau này).
CREATE TABLE IF NOT EXISTS trace_spans (
    trace_id             text   NOT NULL CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    span_id              text   NOT NULL CHECK (span_id ~ '^[0-9a-f]{16}$'),
    parent_span_id       text,
    name                 text   NOT NULL,
    kind                 text   NOT NULL DEFAULT 'INTERNAL',
    start_time_unix_nano bigint NOT NULL,
    end_time_unix_nano   bigint,
    status_code          text   NOT NULL DEFAULT 'UNSET' CHECK (status_code IN ('UNSET', 'OK', 'ERROR')),
    status_message       text,
    tenant_id            text,
    task_id              text,
    workflow_id          text,
    attributes           jsonb  NOT NULL DEFAULT '{}'::jsonb,
    events               jsonb  NOT NULL DEFAULT '[]'::jsonb,
    recorded_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS trace_spans_task_idx ON trace_spans (task_id);
CREATE INDEX IF NOT EXISTS trace_spans_start_idx ON trace_spans (start_time_unix_nano);
