-- Persistent document numbering table, kept in sync with the SQLite core schema.
CREATE TABLE IF NOT EXISTS document_sequences (
    sequence_key TEXT PRIMARY KEY,
    next_value BIGINT NOT NULL CHECK (next_value > 0)
);