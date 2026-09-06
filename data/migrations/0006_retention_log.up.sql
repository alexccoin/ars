-- 0006_retention_log: audit trail for the retention sweeper (security/policies/retention.md).
-- Counts only — never record text, never record ids for SENSITIVE deletions. This is
-- the answer to "did the sweeper run, and what did it do" without becoming a second
-- copy of user data.
CREATE TABLE retention_sweeps (
    id                        TEXT NOT NULL UNIQUE,  -- classification: identifier only (rsw_...)
    ran_at_ms                 INTEGER NOT NULL,
    expired_valid_until_count INTEGER NOT NULL,       -- classification: identifier only (count)
    superseded_purged_count   INTEGER NOT NULL,       -- classification: identifier only (count)
    sensitivity_cap_count     INTEGER NOT NULL,       -- classification: identifier only (count)
    total_deleted             INTEGER NOT NULL        -- classification: identifier only (count)
);

CREATE UNIQUE INDEX idx_retention_sweeps_id ON retention_sweeps(id);
CREATE INDEX idx_retention_sweeps_ran_at ON retention_sweeps(ran_at_ms);
