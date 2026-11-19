-- Durable database-level idempotency keys for newly posted journals.
-- Existing journal entries and their references are not rewritten.
CREATE TABLE IF NOT EXISTS journal_posting_keys (
    idempotency_key TEXT PRIMARY KEY,
    journal_entry_id BIGINT NOT NULL UNIQUE
        REFERENCES journal_entries(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
