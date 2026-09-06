-- 0001_memory_records: core semantic memory table.
--
-- `rowid` (SQLite's implicit integer rowid, since no column here is declared
-- `INTEGER PRIMARY KEY`) is the correlation key used by the FTS5 index (0002) and the
-- vector index (0003/0004) — deleting a row here and its matching rowid everywhere
-- else is the whole of `forget()`.

CREATE TABLE memory_records (
    id                      TEXT NOT NULL UNIQUE,      -- classification: identifier only (mem_...), safe to log
    kind                    TEXT NOT NULL,              -- classification: identifier only (MemoryKind enum value)
    text                    TEXT NOT NULL,              -- classification: mirrors `sensitivity` below — may be SENSITIVE. Never log this column's value.
    language                TEXT NOT NULL,              -- classification: identifier only (en|ro)
    sensitivity             TEXT NOT NULL,              -- classification: identifier only (public|personal|sensitive) — drives retention (security/policies/retention.md)
    provenance_source       TEXT NOT NULL,              -- classification: identifier only
    provenance_trust        TEXT NOT NULL,              -- classification: identifier only
    provenance_uri          TEXT,                       -- classification: PERSONAL — may reveal a mailbox address, file path, or URL
    provenance_fetched_at_ms INTEGER NOT NULL,          -- classification: identifier only
    provenance_label        TEXT,                       -- classification: PERSONAL — short human label, may reference a person or place
    created_at_ms           INTEGER NOT NULL,           -- classification: identifier only
    valid_until_ms          INTEGER,                    -- classification: identifier only
    superseded_by           TEXT                        -- classification: identifier only (id of the record that replaced this one)
);

CREATE UNIQUE INDEX idx_memory_records_id ON memory_records(id);
CREATE INDEX idx_memory_records_superseded_by ON memory_records(superseded_by);
CREATE INDEX idx_memory_records_valid_until ON memory_records(valid_until_ms);
CREATE INDEX idx_memory_records_sensitivity_created ON memory_records(sensitivity, created_at_ms);
CREATE INDEX idx_memory_records_language ON memory_records(language);
