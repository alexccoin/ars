# Retention policy — memory service

Owner: `services/memory`. Enforced in code by `ars_memory.retention.RetentionSweeper`,
not just documented here — this file is the source of truth the sweeper is tested
against, but the sweeper is what actually deletes data.

Retention is keyed on `Sensitivity` (see `packages/protocol/src/ars_protocol/memory.py`)
and on the record's own `valid_until_ms`, which always takes precedence when it would
expire a record *sooner* than the caps below. A caller-set expiry can only shorten
retention, never extend it past a sensitivity cap.

## Caps

| Sensitivity | Current record, no expiry set | Superseded / historical record | Notes |
|---|---:|---:|---|
| `PUBLIC` | indefinite | 365 days from `created_at_ms` | low risk; kept mainly for history/debugging |
| `PERSONAL` | indefinite (durable facts like "I live in Cluj" must not silently vanish) | 180 days from `created_at_ms` | history stays queryable but is not kept forever |
| `SENSITIVE` | **90 days from `created_at_ms`, hard cap** | 30 days from `created_at_ms` | health, finance, credentials, intimate life — the cap applies even if the record was never superseded and has no `valid_until_ms`; the user must actively re-affirm it for it to persist past 90 days |

## Rules

1. **`valid_until_ms` is always honored.** Any record past its own `valid_until_ms` is
   deleted by the sweeper, regardless of sensitivity. Recall also filters expired
   records defensively between sweeps — a record must never be *returned* past expiry
   even for the seconds before the next sweep runs.
2. **Supersession shortens retention, deletion does not wait on it.** A superseded
   record (`superseded_by IS NOT NULL`) is history, not the current fact. It stays
   queryable for the "historical" window above, then is purged — the audit trail is not
   permanent.
3. **`SENSITIVE` never gets an indefinite grant.** Even an unmodified, never-superseded
   `SENSITIVE` fact is deleted 90 days after creation. If it is still true, the caller
   (compute service, on next relevant turn) is expected to re-remember it, which resets
   the clock — this is a deliberate forcing function against silent accumulation of
   health/finance/intimate data.
4. **Deletion via the sweeper is real deletion.** The sweeper calls the same `forget()`
   path used everywhere else: record, embedding, FTS entry. No soft-delete flag, no
   tombstone that still contains the text.
5. **The sweep itself is audited without content.** Each run is logged with counts only
   (how many expired, how many superseded-purged, how many hit a sensitivity cap) — see
   migration `0006_retention_log`. The log never contains record text or ids from
   `SENSITIVE` records.

## Non-goals

This policy governs `MemoryRecord`. `Observation` and `Preference` (learned behaviour)
are not time-capped here — a confirmed `Preference` is meant to persist until the user
changes it; `Observation`s that go stale are the compute service's job to mark
`ObservationStatus.EXPIRED` when their evidence is no longer relevant, not the
sweeper's.
