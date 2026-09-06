# ADR 0001 — The endpointing budget was wrong; waiting beats truncating

*Status: accepted, 2026-09-06. Supersedes the original latency table in
[docs/architecture/overview.md](../architecture/overview.md).*

## Context

The first latency budget allocated **400 ms** to "endpointing after speech ends" inside a
1200 ms total. The voice pipeline shipped with `endpoint_silence_ms = 700`.

These cannot both hold. Endpointing means *waiting for silence to prove the user has
finished*; you cannot confirm 700 ms of silence in 400 ms. The budget row described a
number someone wanted, not a thing that can happen.

Measured on 12 fixtures (6 EN, 6 RO):

| silence window | truncations | p95 |
|---|---:|---:|
| 300 ms | 4 | 310 ms |
| 400 ms | 2 | 410 ms |
| 500 ms | 1 | 510 ms |
| **700 ms** | **0** | 710 ms |

The two fixtures truncated at 400 ms are `en_long_thought` and `ro_long_thought` — a
person pausing between clauses. Romanian sentence rhythm makes this worse, not better.

## Decision

**Keep the 700 ms window. Correct the budget.**

- `ENDPOINTING` budget → **750 ms** (700 ms window + 50 ms detection overhead).
- `TOTAL` (speech end → first audio) → **1400 ms** for the sequential pipeline.

Being cut off mid-sentence is not a latency cost, it is a failure. The user must repeat
themselves, which costs several seconds and, more importantly, teaches them that the
assistant does not listen properly. A confident extra 300 ms is cheaper than that.

## Consequence, and the way out

1400 ms is slower than we want. The route to ~1050 ms is **not** a smaller window — it is
overlapping work with it. The silence window is dead time in which the machine is idle
while it waits to be sure:

- at ~300 ms of silence, speculatively run the ASR final decode on the audio so far;
- assemble context and start LLM prefill against that speculative transcript;
- if speech resumes, discard the speculative work and keep listening.

The cost is wasted compute on false endpoints; the saving is most of the ASR and context
rows disappearing from the perceived path. This is tracked as the next voice work item
and is why `TOTAL` is written as a phase-1 number.

## Alternatives rejected

- **Shorten the window to 400 ms.** Truncates 2 of 12 fixtures. Rejected: see above.
- **Adaptive window (420 ms mean).** Saves ~280 ms at p50 but truncates the same two
  long-thought fixtures. Implemented, ships **off**, available behind config.
- **Leave the budget at 400 ms as an aspiration.** Rejected: a budget nobody can meet
  gets ignored, and then no budget is enforced at all. The `xfail` this replaces was
  already the beginning of that.
