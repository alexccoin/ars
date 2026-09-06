-- `origin_id`: the record this one was derived from.
--
-- A translated copy of a passage exists only because the passage does. Two things follow,
-- and both are correctness rather than bookkeeping:
--
--   Deletion. Without this, `forget()` removes a document and leaves its translations
--   answering questions — non-negotiable #7 failing quietly.
--
--   Retrieval. A passage and its translation are one passage. The tiered brain refuses to
--   answer when the top two hits score alike, so a record competing with its own twin
--   makes an answerable question unanswerable.
ALTER TABLE memory_records ADD COLUMN origin_id TEXT;  -- classification: identifier only

CREATE INDEX idx_memory_records_origin_id ON memory_records(origin_id);
