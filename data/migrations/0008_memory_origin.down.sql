DROP INDEX IF EXISTS idx_memory_records_origin_id;
-- SQLite before 3.35 cannot drop a column; the table is rebuilt instead.
ALTER TABLE memory_records DROP COLUMN origin_id;
