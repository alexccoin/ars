-- 0004_memory_vec (OPTIONAL): sqlite-vec virtual table for real vector similarity
-- search. "Optional" because it requires the sqlite-vec loadable extension, which is
-- not guaranteed to load on every Python/SQLite build (extension loading can be
-- compiled out). The migration runner (ars_memory.db.migrate) is expected to attempt
-- this file, catch a failure to create it, and skip it — ars_memory then falls back to
-- ars_memory.vector_index.numpy_index.NumpyBruteForceIndex over `memory_embeddings`
-- (migration 0003), behind the same VectorIndex interface. Either way `forget()`
-- deletes from whichever of these tables is in use.
--
-- Dimension is fixed at 384 to match paraphrase-multilingual-MiniLM-L12-v2's output
-- (the real embedding backend) and the deterministic hash-based mock backend, which
-- also emits 384-dim vectors so tests exercise the exact same schema as production.
--
-- classification: `embedding` is derived from memory_records.text; same sensitivity
-- as its source record, deleted whenever that record is.
CREATE VIRTUAL TABLE memory_vec USING vec0(
    embedding float[384] distance_metric=cosine
);
