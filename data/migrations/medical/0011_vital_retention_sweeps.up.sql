-- 0011_vital_retention_sweeps: audit trail for ars_medical.retention.RetentionSweeper.
--
-- Same shape and same reasoning as the memory service's `retention_sweeps` (root
-- data/migrations/0006_retention_log.up.sql): counts only, never a reading id, never a
-- value. See services/medical/src/ars_medical/retention.py's module docstring for why
-- this table has only two count columns rather than mirroring memory's three
-- (superseded_purged, sensitivity_cap) — vitals have no `valid_until_ms`-style
-- caller-set expiry to track separately.
CREATE TABLE vital_retention_sweeps (
    id                      TEXT NOT NULL UNIQUE,  -- classification: identifier only (rsw_...)
    ran_at_ms               INTEGER NOT NULL,
    superseded_purged_count INTEGER NOT NULL,       -- classification: identifier only (count)
    current_capped_count    INTEGER NOT NULL,       -- classification: identifier only (count)
    total_deleted           INTEGER NOT NULL        -- classification: identifier only (count)
);

CREATE UNIQUE INDEX idx_vital_retention_sweeps_id ON vital_retention_sweeps(id);
CREATE INDEX idx_vital_retention_sweeps_ran_at ON vital_retention_sweeps(ran_at_ms);
