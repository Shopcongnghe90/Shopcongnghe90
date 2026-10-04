-- 401_d_channels.sql — owner D. Kênh: tài khoản (KHÔNG chứa secret), cursor, khoá chống trùng webhook.

CREATE TABLE IF NOT EXISTS channel_accounts (
    tenant_id    text        NOT NULL REFERENCES tenants(tenant_id),
    channel      text        NOT NULL,
    account_ref  text        NOT NULL,            -- vd page_id / oa_id / shop_id (không phải secret)
    display_name text,
    enabled      boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, channel, account_ref)
);

CREATE TABLE IF NOT EXISTS channel_cursors (
    tenant_id  text        NOT NULL REFERENCES tenants(tenant_id),
    channel    text        NOT NULL,
    cursor     text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, channel)
);

CREATE TABLE IF NOT EXISTS channel_dedupe (
    dedupe_key text        PRIMARY KEY,
    seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS channel_dedupe_seen_at ON channel_dedupe (seen_at);
