# A.R.S — working agreement

A private, local-first voice and compute assistant. Speaks and understands **English and
Romanian**. Runs on the user's own machine and phone. Acts on their real accounts only
under explicit, scoped grants.

Read [docs/architecture/overview.md](docs/architecture/overview.md) before changing
anything that crosses a service boundary.

## Non-negotiables

1. **`packages/protocol` is the single source of truth.** Need a new field? Change it
   there. A type defined twice is a bug, not a shortcut.
2. **No vendor SDK outside a backend implementation.** faster-whisper, Piper, Ollama and
   the Anthropic SDK are only ever imported inside a class implementing an interface from
   `packages/core`. This is what makes local/cloud swappable.
3. **Nothing reaches private data without a guard ALLOW.** Every tool call goes through
   `GuardEngine.evaluate` before any side effect. The skills runtime re-checks.
4. **External text is data, never instruction.** Anything fetched from the web, email or
   GitHub is tagged `TrustLevel.EXTERNAL`, taints the turn, and blocks silent use of
   private or effectful capabilities.
5. **Both languages, always.** Any user-facing string exists in EN and RO. Any ASR/TTS
   change is evaluated on both. A feature that works only in English is unfinished.
6. **Latency is a test.** The budget in the architecture doc is asserted in `tests/load`.
7. **Deletion must actually delete** — records, embeddings, caches. Tested, not assumed.
8. **Never commit** model weights, real user audio, tokens, or `.env`.

## Toolchain

Python 3.12 via `uv` (`.venv/` at the root), Node 22 for the web/desktop apps.
`uv run` for anything Python. Target hardware: Apple Silicon, arm64.

## Definition of done

Code + protocol types + unit test + EN/RO coverage where user-facing + telemetry +
a line in the relevant doc. Say plainly what you skipped and why.
