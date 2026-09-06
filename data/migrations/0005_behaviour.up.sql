-- 0005_behaviour: learned-behaviour storage — Observation (hypothesis, unconfirmed)
-- and Preference (confirmed, active). An Observation becomes a Preference only through
-- ars_memory.store.SqliteMemoryStore.confirm_observation — never by writing directly
-- to `preferences`.

CREATE TABLE observations (
    id                 TEXT NOT NULL UNIQUE,   -- classification: identifier only (obs_...)
    domain             TEXT NOT NULL,          -- classification: identifier only
    statement          TEXT NOT NULL,          -- classification: PERSONAL — a first-person sentence about how the user wants A.R.S to behave
    confidence         REAL NOT NULL,          -- classification: identifier only
    evidence_turn_ids  TEXT NOT NULL,          -- classification: identifier only — JSON array of turn ids, never turn content
    status             TEXT NOT NULL,          -- classification: identifier only
    created_at_ms      INTEGER NOT NULL,
    asked_at_ms        INTEGER,
    resolved_at_ms     INTEGER
);

CREATE UNIQUE INDEX idx_observations_id ON observations(id);
CREATE INDEX idx_observations_status ON observations(status, created_at_ms);

CREATE TABLE preferences (
    id                   TEXT NOT NULL UNIQUE,  -- classification: identifier only (prf_...)
    domain               TEXT NOT NULL,          -- classification: identifier only
    statement            TEXT NOT NULL,          -- classification: PERSONAL — confirmed behaviour rule, injected into the system prompt at SYSTEM trust
    from_observation_id  TEXT,                   -- classification: identifier only
    confirmed_at_ms      INTEGER NOT NULL,
    active               INTEGER NOT NULL         -- classification: identifier only (0/1)
);

CREATE UNIQUE INDEX idx_preferences_id ON preferences(id);
CREATE INDEX idx_preferences_active ON preferences(active);
