-- 001_token_refs.sql - the OAuth token vault index.
--
-- This table holds NO token material and NO key material. It holds only what the rest of
-- the system is allowed to know: that an account exists, for which provider, with which
-- scopes, which capabilities may spend it, and when it expires. The secret lives in the
-- macOS Keychain, or in a Fernet-encrypted file whose key lives in the Keychain.
--
-- The split is the point. Someone who walks off with this database learns that the user
-- has a Gmail account. They do not gain the ability to read the mail.

CREATE TABLE IF NOT EXISTS token_refs (
    id             TEXT PRIMARY KEY,          -- tkr_...
    provider       TEXT NOT NULL,             -- "google", "github", ...
    account        TEXT NOT NULL,             -- account label, e.g. an address
    scopes         TEXT NOT NULL,             -- JSON array, provider's own scope strings
    capabilities   TEXT NOT NULL,             -- JSON array of ars Capability values
    created_at_ms  INTEGER NOT NULL,
    expires_at_ms  INTEGER,
    backend        TEXT NOT NULL,             -- "keychain" | "fernet_file"
    secret_locator TEXT NOT NULL,             -- Keychain account name / key file id. Not a secret.
    revoked_at_ms  INTEGER,
    UNIQUE (provider, account)
);

CREATE INDEX IF NOT EXISTS idx_token_refs_provider ON token_refs (provider);
