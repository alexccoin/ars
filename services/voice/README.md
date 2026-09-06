# services/voice

Wakeword, VAD + endpointing, ASR and TTS for A.R.S. English and Romanian, on-device by
default. Owns four of the six rows of the latency budget in
[docs/architecture/overview.md](../../docs/architecture/overview.md).

Every engine sits behind an interface from `packages/core`; every sample rate, frame size
and encoding comes from `packages/protocol/src/ars_protocol/audio.py`. There is no
sample-rate literal in this service and a test asserts it.

## Backends

| Seam | On-device backend | No-weights backend | Weights needed |
|---|---|---|---|
| `WakewordEngine` | `OpenWakeWordEngine` | `MockWakewordEngine`, `NullWakewordEngine` | `models/wakeword` |
| `VadEngine` | `SileroVadEngine` | `EnergyVadEngine` (adaptive, real) | `models/vad` |
| `AsrEngine` | `FasterWhisperEngine` (large-v3-turbo, int8) | `MockAsrEngine` | `models/asr` |
| `TtsEngine` | `PiperTtsEngine` (EN + `ro_RO`) | `MockTtsEngine`, `NullTtsEngine` | `models/tts` |

Vendor SDKs are imported inside methods, never at module scope: the package imports, all
engines construct, and the whole pipeline runs with none of them installed. Select backends
with `ARS_WAKE_BACKEND`, `ARS_VAD_BACKEND`, `ARS_ASR_BACKEND`, `ARS_TTS_BACKEND`.

```bash
uv pip install -e services/voice          # mock path, no weights
uv pip install -e 'services/voice[all]'   # + openwakeword, faster-whisper, silero, piper
scripts/fetch_voice_models.sh             # ~2.5 GB, never committed
```

## Running it

```bash
uv run ars-voice-fixtures all          # regenerate the synthetic fixture set
uv run ars-voice-latency --turns 12    # end-to-end latency against the budget
uv run ars-voice-endpoint-eval --sweep # endpointing tuning, before/after
uv run ars-voice-wakeword-eval         # FA/hour and FR on a fixed set
```

## Endpointing

`endpoint_silence_ms` lives in `ars_core.VoiceConfig` (default 700). Everything else is in
`ars_voice.config.EndpointingConfig`. Three mechanisms bias the design toward waiting:

* **minimum-utterance guard** — a cough cannot endpoint a turn;
* **resume-onset guard** — a single noisy frame cannot restart the silence countdown;
* **trailing-hesitation grace** — an utterance ending in `uhm` / `and` / `păi` / `și` buys
  `hesitation_grace_ms` more, in both languages.

Measured on `data/fixtures/endpointing` (12 fixtures, 6 EN / 6 RO):

| configuration | truncations | p50 | p95 |
|---|---:|---:|---:|
| 300 ms | 4 | 300 | 310 |
| 400 ms | 2 | 400 | 410 |
| 500 ms | 1 | 500 | 510 |
| 600 ms | 0 | 600 | 610 |
| **700 ms (shipped)** | **0** | **700** | **710** |
| 900 ms | 0 | 900 | 910 |
| adaptive (420 ms when the utterance looks complete) | 2 | 420 | 700 |

Adaptive endpointing ships **off**: it saves ~280 ms at p50 and truncates
`en_long_thought` and `ro_long_thought` — both of them a user pausing between two clauses.
That trade is the wrong way round. `tests/unit/voice/test_fixture_regressions.py` pins it.

**The 400 ms ENDPOINTING budget row is not reachable with a 700 ms silence window.** The
measured p95 is ~720 ms. Either the row or the default has to move; the fixture sweep is the
evidence for whichever way that goes.

## Wakeword false accepts

`false_accepts_per_hour` reads a measured record from `research/benchmarks/wakeword/` and
returns **NaN** when there is none — a false accept means the microphone opened when nobody
asked, so the number is never guessed. It also returns NaN when the record was measured at a
different threshold, because FA/hour is a function of the operating point.
`assert_shippable()` raises instead of returning NaN.

## Fixtures

`data/fixtures/**` is **synthetic** — speech-shaped audio with controlled onsets, levels and
pauses, generated deterministically by `ars_voice.fixtures`. It constrains *timing* and
nothing else. Recognition accuracy, real FA/hour and voice quality need real recordings, in
both languages, in the room A.R.S will live in.
