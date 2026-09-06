-- 001_grants.sql - grants are append-only; revocation is a row, never an UPDATE.
--
-- The user must be able to see the whole history of what they allowed and when they took
-- it back. An UPDATE erases that history, and the consent record collapses to "whatever
-- the current state happens to be" - which is exactly the shape of record that lets a
-- system quietly re-interpret what it was allowed to do.
--
-- Enforced by triggers, not by convention: a future maintainer who reaches for UPDATE
-- gets an error, not a silent rewrite of the user's consent record.

CREATE TABLE IF NOT EXISTS grants (
    id                TEXT PRIMARY KEY,
    capability        TEXT NOT NULL,
    resource_patterns TEXT NOT NULL,          -- JSON array of glob patterns
    confirm           TEXT NOT NULL,
    granted_at_ms     INTEGER NOT NULL,
    expires_at_ms     INTEGER,                -- NULL = no expiry
    source            TEXT NOT NULL,
    note              TEXT,                   -- the user's own words; may be NULL
    written_at_ms     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grants_capability ON grants (capability);
CREATE INDEX IF NOT EXISTS idx_grants_granted_at ON grants (granted_at_ms);

-- One row per revocation event. A grant is revoked at MIN(revoked_at_ms) over its rows,
-- so a duplicate revocation is harmless and no later row can move the revocation time
-- forward - i.e. a revocation cannot be undone by appending.
CREATE TABLE IF NOT EXISTS grant_revocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id      TEXT NOT NULL REFERENCES grants (id),
    revoked_at_ms INTEGER NOT NULL,
    reason        TEXT,
    written_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revocations_grant ON grant_revocations (grant_id);

CREATE TRIGGER IF NOT EXISTS grants_no_update
BEFORE UPDATE ON grants
BEGIN
    SELECT RAISE(ABORT, 'grants are append-only: revoke and re-grant, never update');
END;

CREATE TRIGGER IF NOT EXISTS grants_no_delete
BEFORE DELETE ON grants
BEGIN
    SELECT RAISE(ABORT, 'grants are append-only: revoke, never delete');
END;

CREATE TRIGGER IF NOT EXISTS revocations_no_update
BEFORE UPDATE ON grant_revocations
BEGIN
    SELECT RAISE(ABORT, 'revocations are append-only');
END;

CREATE TRIGGER IF NOT EXISTS revocations_no_delete
BEFORE DELETE ON grant_revocations
BEGIN
    SELECT RAISE(ABORT, 'revocations are append-only');
END;

-- The effective view the guard loads into memory at startup.
CREATE VIEW IF NOT EXISTS grants_effective AS
SELECT g.id, g.capability, g.resource_patterns, g.confirm, g.granted_at_ms,
       g.expires_at_ms, g.source, g.note,
       (SELECT MIN(r.revoked_at_ms) FROM grant_revocations r WHERE r.grant_id = g.id)
           AS revoked_at_ms
FROM grants g;
