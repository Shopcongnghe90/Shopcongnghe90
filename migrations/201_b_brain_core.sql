-- 201_b_brain_core.sql — Workstream B: Project Brain (canonical state, decision ledger, memory, artifacts, retention).
-- Mọi bảng có tenant_id FK tenants. vector tuỳ chọn (cột embedding chỉ tạo khi có extension).

CREATE TABLE IF NOT EXISTS canonical_state (
    tenant_id   text        NOT NULL REFERENCES tenants(tenant_id),
    key         text        NOT NULL,
    version     integer     NOT NULL CHECK (version >= 1),
    value       jsonb       NOT NULL,
    updated_by  text        NOT NULL DEFAULT 'system',
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, key, version)
);
DROP TRIGGER IF EXISTS canonical_state_append_only ON canonical_state;
CREATE TRIGGER canonical_state_append_only BEFORE UPDATE OR DELETE ON canonical_state
    FOR EACH ROW EXECUTE FUNCTION zeus_forbid_mutation();

CREATE TABLE IF NOT EXISTS decision_ledger (
    decision_id  text PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants(tenant_id),
    title        text        NOT NULL,
    decision     text        NOT NULL,
    rationale    text        NOT NULL DEFAULT '',
    status       text        NOT NULL DEFAULT 'ACCEPTED' CHECK (status IN ('ACCEPTED','PROPOSED','SUPERSEDED','GATED')),
    supersedes   text,
    evidence_ref text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS decision_ledger_tenant_idx ON decision_ledger (tenant_id, created_at DESC);
DROP TRIGGER IF EXISTS decision_ledger_append_only ON decision_ledger;
CREATE TRIGGER decision_ledger_append_only BEFORE UPDATE OR DELETE ON decision_ledger
    FOR EACH ROW EXECUTE FUNCTION zeus_forbid_mutation();

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id       text PRIMARY KEY,
    tenant_id       text        NOT NULL REFERENCES tenants(tenant_id),
    kind            text        NOT NULL CHECK (kind IN ('canonical_state','decision','episodic','semantic','artifact','evidence')),
    title           text,
    content         text        NOT NULL,
    search_text     text        NOT NULL,  -- bản bỏ dấu tiếng Việt, chữ thường (FTS 'simple')
    source_ref      text,
    trust           text        NOT NULL DEFAULT 'unverified' CHECK (trust IN ('verified','unverified','untrusted')),
    confidence      real        NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    tags            text[]      NOT NULL DEFAULT '{}',
    valid_from      timestamptz NOT NULL DEFAULT now(),
    valid_to        timestamptz,
    supersedes      text,
    embedding_model text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    fts             tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
);
CREATE INDEX IF NOT EXISTS memory_items_tenant_kind_idx ON memory_items (tenant_id, kind);
CREATE INDEX IF NOT EXISTS memory_items_fts_idx ON memory_items USING gin (fts);
CREATE INDEX IF NOT EXISTS memory_items_tags_idx ON memory_items USING gin (tags);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS embedding vector;
    ELSE
        RAISE NOTICE 'pgvector missing: memory_items has no embedding column (lexical-only)';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS artifacts (
    tenant_id   text        NOT NULL REFERENCES tenants(tenant_id),
    sha256      text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes  bigint      NOT NULL CHECK (size_bytes >= 0),
    mime        text        NOT NULL DEFAULT 'application/octet-stream',
    name        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, sha256)
);
DROP TRIGGER IF EXISTS artifacts_immutable ON artifacts;
CREATE TRIGGER artifacts_immutable BEFORE UPDATE OR DELETE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION zeus_forbid_mutation();

CREATE TABLE IF NOT EXISTS retention_policies (
    tenant_id     text    NOT NULL REFERENCES tenants(tenant_id),
    kind          text    NOT NULL CHECK (kind IN ('episodic','semantic','artifact','evidence')),
    max_age_days  integer NOT NULL CHECK (max_age_days >= 1),
    legal_hold    boolean NOT NULL DEFAULT false,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, kind)
);

-- Evidence: DELETE luôn cấm; UPDATE chỉ khi bản cũ còn UNVERIFIED (nâng cấp bằng chứng); verified => bất biến.
CREATE TABLE IF NOT EXISTS evidence_records (
    record_id     text PRIMARY KEY,
    tenant_id     text        NOT NULL REFERENCES tenants(tenant_id),
    task_id       text        NOT NULL,
    trace_id      text        NOT NULL,
    task_family   text        NOT NULL,
    final_outcome text        NOT NULL CHECK (final_outcome IN ('VERIFIED_SUCCESS','VERIFIED_FAILURE','UNVERIFIED')),
    strength      text        NOT NULL CHECK (strength IN ('strong','weak','none')),
    payload       jsonb       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_records_task_idx ON evidence_records (tenant_id, task_id, created_at);

CREATE OR REPLACE FUNCTION zeus_evidence_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.final_outcome <> 'UNVERIFIED' THEN
        RAISE EXCEPTION 'evidence_records is immutable once verified (% forbidden)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.record_id <> OLD.record_id OR NEW.tenant_id <> OLD.tenant_id OR NEW.task_id <> OLD.task_id THEN
        RAISE EXCEPTION 'evidence identity fields are immutable' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS evidence_records_guard ON evidence_records;
CREATE TRIGGER evidence_records_guard BEFORE UPDATE OR DELETE ON evidence_records
    FOR EACH ROW EXECUTE FUNCTION zeus_evidence_guard();

CREATE TABLE IF NOT EXISTS evidence_artifact_links (
    tenant_id  text NOT NULL REFERENCES tenants(tenant_id),
    record_id  text NOT NULL REFERENCES evidence_records(record_id),
    sha256     text NOT NULL,
    PRIMARY KEY (tenant_id, record_id, sha256),
    FOREIGN KEY (tenant_id, sha256) REFERENCES artifacts(tenant_id, sha256)
);
