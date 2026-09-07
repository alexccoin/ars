# Retention policy — medical (vitals) service

Owner: `services/medical`. Enforced in code by `ars_medical.retention.RetentionSweeper`,
not just documented here — the same relationship `security/policies/retention.md` has
with `ars_memory.retention.RetentionSweeper`.

This file exists because `retention.md` covers `MemoryRecord` only. Before this change,
`vital_readings` had a `forget()` path (user-initiated, tested against the raw sqlite
file down to the WAL) but **no sweeper** — nothing aged data out on its own. CLAUDE.md
non-negotiable #7 ("deletion must actually delete... tested, not assumed") applies with
more force to health data than to anything else in this system, so that gap needed an
explicit answer, not silence. This is that answer.

## The decision, and why it is not just "copy `retention.md`"

`ars_memory`'s policy gives `SENSITIVE` records a hard 90-day cap even when current and
never superseded, on the theory that a stale, unconfirmed fact ("I take medication X",
said once six months ago) should not silently keep accumulating; if it is still true,
the compute service re-remembers it on a relevant turn, which resets the clock. That
mechanism — "the fact comes back on its own if it's still true" — has **no equivalent
for a vital reading**. A blood-pressure reading from March 3rd is not a fact that gets
re-affirmed by a later reading; the later reading is a *different* measurement, and
`SqliteMedicalStore.series`/`aggregate`/`Trend` exist specifically to answer "how has
this trended over months", which is exactly the question a rolling deletion window
would destroy the answer to. Applying `ars_memory`'s cap here would mean: the longer
someone conscientiously tracks a condition, the less of their own history A.R.S can show
them — the opposite of what non-negotiable #7 and the whole point of this store are for.

So the medical retention policy is asymmetric on purpose:

| state | default | rationale |
|---|---:|---|
| **Live reading** (not superseded) | **kept indefinitely** by the sweeper (`retention_current_days = None`) | the longitudinal series *is* the value of this store; see above |
| **Superseded reading** (a correction was recorded) | purged **30 days** after `measured_at_ms` (`retention_superseded_days`) | matches `ars_memory`'s tightest (`SENSITIVE`) superseded window; what remains after a correction is an audit trail, not the fact itself, and health data gets the tight bound, not the loose one |

`VitalReading` has no `valid_until_ms`-style caller expiry (unlike `MemoryRecord`), so
there is no "rule 1, valid_until always wins" analogue here — the table above is the
whole policy, not an abbreviation of it.

## What this is not

This is **not** a claim that vitals should be kept forever unconditionally:

* `retention_current_days` is a real, honoured config field. A caller with an actual
  reason — a user preference ("delete my vitals after N years"), a jurisdiction's data
  minimisation requirement, a device the user has stopped using and wants forgotten —
  sets it and the sweeper enforces it exactly like `ars_memory`'s cap. The default is
  `None`, not "there is no mechanism."
* `forget()` (already shipped, already tested against the raw sqlite file including the
  WAL — `tests/unit/medical/test_forget_deletes_vitals.py`) remains the immediate,
  user-initiated deletion path and is unaffected by any of the above. A user who says
  "delete that reading" gets it deleted now, regardless of age or supersession state.
* Superseded readings are still purged automatically, on the same reasoning
  `ars_memory` applies to its own historical windows: a correction's audit trail is not
  meant to be permanent.

## Enforcement

`ars_medical.retention.RetentionSweeper.sweep()` calls the same `SqliteMedicalStore.
forget()` path every other deletion in this service uses — record, WAL checkpoint,
everything `test_forget_deletes_vitals.py` already proves. No soft-delete flag, no
tombstone that still contains the value. Each run is logged in `vital_retention_sweeps`
with counts only (`superseded_purged_count`, `current_capped_count`,
`total_deleted`) — never a reading id, never a value, mirroring
`security/policies/retention.md`'s rule 5 for the same reason.
