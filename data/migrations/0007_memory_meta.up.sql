-- Small key/value table for facts *about* the store rather than in it.
--
-- First use: which embedding model produced the vectors. Vectors from different models
-- are not comparable, so an index built by one model and queried by another returns
-- wrong passages with high confidence. Recording the identity lets the store notice and
-- re-embed instead of silently degrading.
CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
