-- Add company-level activity pack assignments and role mappings.
CREATE TABLE IF NOT EXISTS company_activity_packs (
    activity_code TEXT PRIMARY KEY,
    profile_code TEXT NOT NULL,
    enabled BIGINT NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    is_primary BIGINT NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_company_activity_primary
    ON company_activity_packs (is_primary) WHERE is_primary = 1;
CREATE TABLE IF NOT EXISTS company_activity_account_map (
    activity_code TEXT NOT NULL,
    role TEXT NOT NULL,
    account_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (activity_code, role),
    FOREIGN KEY (activity_code)
        REFERENCES company_activity_packs (activity_code)
);
