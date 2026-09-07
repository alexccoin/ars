-- 0009_vital_readings: one row per measurement.
--
-- Immutable like `memory_records` (0001): a wrong reading is superseded via
-- `superseded_by`, never edited in place — see `ars_medical.store` module docstring for
-- the same validated transition memory's store enforces (superseded_by NULL -> a value,
-- exactly once, nothing else about the row ever changes).
--
-- `rowid` (SQLite's implicit integer rowid; no column here is `INTEGER PRIMARY KEY`) is
-- not used as a correlation key by anything else the way it is in ars_memory (there is
-- no FTS/vector shadow table for vitals), but every deletion still goes by `id`, kept
-- consistent with the rest of the codebase for the same reason: a numeric id is never
-- exposed to a caller, only the prefixed `vit_...` id from the protocol.
--
-- The dedup constraint is the point of this table's shape: a device that resends its
-- buffer must not be able to create a second row for the same measurement. Kept as a
-- real UNIQUE index (not "check before insert" in Python alone) so it holds even under
-- concurrent writers, not just under this store's own asyncio.Lock.
CREATE TABLE vital_readings (
    id                 TEXT NOT NULL UNIQUE,   -- classification: identifier only (vit_...), safe to log
    kind               TEXT NOT NULL,           -- classification: identifier only (VitalKind enum value)
    value              REAL NOT NULL,           -- classification: SENSITIVE — the measurement itself. Never log this column's value.
    measured_at_ms     INTEGER NOT NULL,        -- classification: identifier only
    device_kind        TEXT NOT NULL,           -- classification: identifier only (DeviceKind enum value)
    device_name        TEXT,                    -- classification: PERSONAL — may name a specific product/model the user owns
    device_id          TEXT,                    -- classification: PERSONAL — stable per physical device
    note               TEXT,                    -- classification: SENSITIVE — free text the user attached to a health reading. Never log.
    sensitivity        TEXT NOT NULL,           -- classification: identifier only (public|personal|sensitive)
    superseded_by      TEXT                     -- classification: identifier only (id of the reading that replaced this one)
);

CREATE UNIQUE INDEX idx_vital_readings_id ON vital_readings(id);

-- Dedup key from the task brief, verbatim: (kind, value, measured_at_ms). Deliberately
-- not including device_id/source — a device replaying its buffer is still the same
-- measurement even if paired over a different transport, and two distinct real
-- measurements landing on the exact same kind+value+millisecond are not a case this
-- store needs to distinguish.
CREATE UNIQUE INDEX idx_vital_readings_dedup ON vital_readings(kind, value, measured_at_ms);

-- Series/latest/aggregate queries all filter by kind and order/range by time — this is
-- the index that keeps them CPU-tier fast (see tests/load).
CREATE INDEX idx_vital_readings_kind_time ON vital_readings(kind, measured_at_ms);
CREATE INDEX idx_vital_readings_superseded_by ON vital_readings(superseded_by);
