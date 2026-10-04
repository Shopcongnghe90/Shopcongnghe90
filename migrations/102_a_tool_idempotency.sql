-- 102_a_tool_idempotency.sql — Workstream A. Bất biến sau khi áp dụng.
-- Khoá idempotency bền vững của Tool Gateway cho action external (gửi tin, ghi Odoo...): chiếm khoá TRƯỚC khi gọi provider,
-- hoàn tất SAU. Activity retry sau khi control worker restart không thực thi lại hành động ngoài (review R7).

CREATE TABLE IF NOT EXISTS tool_idempotency (
    tenant_id  text        NOT NULL REFERENCES tenants(tenant_id),
    idem_key   text        NOT NULL,
    status     text        NOT NULL CHECK (status IN ('PENDING','DONE')),
    result     jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, idem_key)
);
