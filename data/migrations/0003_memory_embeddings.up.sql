-- 0003_memory_embeddings: raw embedding storage, used by the numpy brute-force
-- fallback index (ars_memory.vector_index.numpy_index) and, regardless of which vector
-- backend is active, as the single place `forget()` must prove empty for a deleted
-- record — see tests/unit/memory/test_forget_deletes_everything.py.
--
-- When sqlite-vec is available, ars_memory instead uses the `memory_vec` virtual table
-- created by migration 0004 for actual similarity search, but this table is still kept
-- as the authoritative byte store either way (0004's vec0 table is populated from it),
-- so there is exactly one place that "the embedding" lives conceptually.
--
-- classification: `embedding` is derived entirely from memory_records.text — it
-- inherits that record's sensitivity and must be deleted whenever the source record is.
CREATE TABLE memory_embeddings (
    memory_rowid INTEGER PRIMARY KEY,   -- classification: identifier only; = memory_records.rowid
    dim          INTEGER NOT NULL,      -- classification: identifier only
    embedding    BLOB NOT NULL          -- classification: derived from source record's text; same sensitivity
);
