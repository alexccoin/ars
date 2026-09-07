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
| `AsrEngine` | `MlxWhisperEngine` (large-v3-turbo, Metal) / `FasterWhisperEngine` (int8 CPU) | `MockAsrEngine` | HF cache / `models/asr` |
| `TtsEngine` | `PiperTtsEngine` (en/ro/de, 31 named voices) + `CharacterTtsEngine` (robot/alien, no weights) | `MockTtsEngine`, `NullTtsEngine` | `models/tts` |

Vendor SDKs are imported inside methods, never at module scope: the package imports, all
engines construct, and the whole pipeline runs with none of them installed. Select backends
with `ARS_WAKE_BACKEND`, `ARS_VAD_BACKEND`, `ARS_ASR_BACKEND`, `ARS_TTS_BACKEND`.

`asr_backend` is declared in `ars_core.VoiceConfig` (not here) because the deployment shape
depends on it, and it defaults **per platform**: `mlx-whisper` on Apple Silicon,
`faster-whisper` everywhere else. That is a measurement, not a preference — CTranslate2 has
no Metal backend, so on an M-series machine faster-whisper decodes on the CPU:

| backend | EN (5.9 s of speech) | RO (4.8 s) | realtime |
|---|---:|---:|---|
| faster-whisper large-v3-turbo int8 | 8237 ms | 8333 ms | 0.7x / 0.6x |
| **mlx-whisper large-v3-turbo** | **121 ms** | **135 ms** | **49x / 36x** |

Identical transcripts, both languages detected at confidence 1.00. faster-whisper stays: it
is the portable path, and it is what runs on anything that is not Apple Silicon.

```bash
uv pip install -e services/voice          # mock path, no weights
uv pip install -e 'services/voice[all]'   # + openwakeword, faster-whisper, silero, piper
scripts/fetch_voice_models.sh             # ~2.5 GB, never committed
```

## Running it

```bash
uv run ars-voice-fixtures all                      # fixtures (spoken ones need Piper)
uv run ars-voice-latency --engines real --turns 20 # real backends, against the budget
uv run ars-voice-latency --turns 12                # mock path, runs anywhere
uv run ars-voice-endpoint-eval --sweep             # endpointing tuning, before/after
uv run ars-voice-wakeword-eval                     # FA/hour and FR on a fixed set
```

## Measured latency

Real engines, 20 turns, audio replayed at wall clock, M5 Max. Budget rows as amended by
[ADR 0001](../../docs/adr/0001-endpointing-latency-budget.md).

| stage | p50 | p95 | budget | |
|---|---:|---:|---:|---|
| wakeword detection | 4.2 ms | 4.8 ms | 150 ms | ok |
| endpointing | 700 ms | 700 ms | 750 ms | ok |
| ASR final | 125 ms | 150 ms | 250 ms | ok |
| TTS time-to-first-audio | 53 ms | 81 ms | 120 ms | ok |
| **total** (speech end → first audio, + wake) | 887 ms | **930 ms** | 1400 ms | ok |

Startup, paid once by `VoicePipeline.warm_up()` and never inside a turn: wakeword ~460 ms,
VAD ~385 ms, ASR ~1.4 s, TTS ~835 ms.

The endpointing row is the 700 ms silence window plus sub-millisecond processing. Getting to
that number required fixing two measurement bugs, both of which flattered or penalised us for
the wrong reason: the replay harness slept `FRAME_MS` per frame and accumulated ~35 ms of
drift across a silence window (it now paces against an absolute schedule, like a capture
device), and the "speech ended" timestamp was taken one frame after the endpointer had
already started counting, under-reporting by 20 ms.

**Warm-up is load-bearing, not tidiness.** A Piper voice costs ~355 ms to load and it is
*per voice*, so a bilingual household would miss the 120 ms budget once in English and again
in Romanian. Both voices are preloaded and primed on a full sentence — priming on one word
left the first real reply paying for ONNX Runtime's shape allocation (115 ms vs 40-60 ms
after). The same applies to mlx-whisper: priming on silence decodes almost no tokens, so the
first turns still paid for kernel compilation (ASR final ranged 121-293 ms; priming on a
representative utterance in both languages flattened it to 121-150 ms).

## Time-to-first-audio and sentence length

Piper renders a whole sentence before it emits a single sample, so time-to-first-audio
scales with the length of the *first* sentence — about 0.9 ms per character on both voices:

| first sentence | EN | RO |
|---:|---:|---:|
| 60 chars | 56 ms | 60 ms |
| 100 chars | 92 ms | 96 ms |
| 140 chars | 119 ms | 124 ms |

`tts.first_sentence_max_chars` was therefore lowered from 140 to **90**: at 140 the measured
end-to-end first audio was 128-132 ms, over the 120 ms budget; at 90 it is ~55 ms p50 /
82 ms p95. The cap only bites when the first sentence has no boundary inside 90 characters —
a normal short opening sentence is still emitted whole.

## Voices, and the robot and alien characters

`SynthesisRequest.voice` used to be ignored: the engine knew one voice per language and
never set Piper's `speaker_id`, so the multi-speaker models were unreachable. It is honoured
now, and `ars_voice.tts.catalogue` is the list of names it accepts.

* **31 named voices across en/ro/de**, female and male, from 16 model files - four of them
  multi-speaker (`en_GB-vctk-medium` is 109 speakers in 77 MB). `voices(language=..., gender=...)`
  enumerates them; `en_GB-vctk-medium#p300` reaches any speaker the roster does not name.
  Every label and caveat exists in EN, RO and DE. `scripts/fetch_voice_models.sh voices`
  downloads the roster (~1.1 GB, opt-in); the three defaults come with `tts`.
* **Romanian has exactly one Piper voice in existence and it is male.** The catalogue says
  so (`ROMANIAN_LIMITATION`, in three languages) instead of substituting English. The two
  extra Romanian entries are `mihai` pitch- **and formant**-shifted (`formant_k`, free -
  the backend resamples anyway); at k=1.30 that measures 155 Hz, which is a smaller man,
  not a woman, and is labelled as such.
* **Characters are DSP, not models**: `CharacterTtsEngine` wraps any `TtsEngine` and applies
  `robot_ring` / `robot_dalek` / `robot_vocoder` / `alien_ring` / `alien_swarm`. Measured
  0.08-1.01 ms per 120 ms chunk, causal, no lookahead, bit-identical at 320/640/997/1920
  samples per chunk. Being an effect rather than a model is what makes them work in Romanian.
  Ask for one as `robot_dalek` (over the language default) or `robot_dalek/alan`.
* **No `high`-quality voice is in the catalogue.** Measured 262-436 ms to first audio against
  a 120 ms budget; `test_voice_catalogue.py` asserts their absence and the budget itself.

Warm, one voice per model file, median of 5 (M5 Max): **39-64 ms** to first audio, worst
`en_US-sam-medium` 64 ms; a character over the default voice costs +0 to +2 ms.

**Voices load lazily and the defaults are pinned.** 95-110 MB and ~340 ms each means
preloading the roster would be 3 GB and ten seconds of startup; `warm_up()` loads the active
voice per language only, everything else is loaded on first use and evicted LRU
(`max_resident_voices`, default 5). The pin matters: without it, trying five character voices
evicts the English default and the next ordinary turn pays a model load. The eviction policy
also protects the voice it has just loaded - a cap equal to the number of languages otherwise
evicted every non-default voice before it produced a sample, measured as 400 ms to first
audio on voices that do 55 ms warm.

## The wake phrase in the transcript

openWakeWord fires at the *end* of the keyword, so the pre-roll ring buffer — which exists so
the first syllable of the command is not clipped — also hands ASR the tail of the keyword.
Measured, the final transcript came back as
`"Jarvis, good morning, I found three new messages from the bank."`

Shortening the pre-roll would trade a cosmetic problem for a truncated first word. So the
audio keeps the keyword and `wakeword.strip_from_transcript` (on by default) removes a
*leading* match from the text, only on turns that began with a wake event — a barge-in turn
has no wake phrase in front of it and keeps its first word.

## The push-to-talk button, in every state

A press goes through `VoicePipeline.request_turn()`, never straight at the wakeword engine.
Arming the engine only does something while the pipeline is IDLE — that is the only state in
which it is fed frames — so an arm set at any other moment is not lost, it is *deferred*
until the current turn ends, and then opens the microphone when nobody is talking. Measured
before the fix, on mock engines: presses during LISTENING and THINKING produced zero wake
events and zero state changes, and the arm fired 1.95 s after the last press; through the
gateway, behind a 14B reply, the same deferral was 13 s. From outside, a dead button.

| state at press | what happens | why |
|---|---|---|
| IDLE / ERROR | arm the engine; fires within one 80 ms window, with the pre-roll | unchanged — one way to begin a turn |
| LISTENING | nothing, and **no arm is left behind** | the microphone is already open; an arm here is the stale arm above |
| TRANSCRIBING / THINKING / ACTING / SPEAKING | cancel the turn (synthesiser, compute request, task, speaker queue) and listen | the user's own hand is on the button — not an echo, not ambiguous |

The last row is not barge-in and does not depend on it: barge-in stays off, because on open
speakers A.R.S hears itself. `interrupt()` (the stop button, Escape, `{"type":"interrupt"}`)
still cancels *to idle* — "be quiet" and "listen to me" are different intentions.

`VoiceLoop.press()` logs the outcome (`armed` / `already_listening` / `interrupted`) and the
state it was in. It used to log "press: armed" unconditionally, which was true and useless.

## Silent drops, and gaps that were never drops

Capture assigns sequence numbers *before* the bounded queue (`CaptureQueue`), so a block the
queue cannot hold leaves a hole and every gap detector downstream reports it. They used to be
assigned on the way out, which numbered dropped audio out of existence.

The pipeline renumbers frames into the wakeword channel, because that channel is fed only
while IDLE: every turn used to leave a hole there and the engine, unable to know the pause
was deliberate, logged `wakeword: frame gap, expected seq=4 got 1077` — 21 s of audio
apparently thrown away, none of which was. Real loss is reported by `VoicePipeline._on_frame`,
in frames and milliseconds, and counted in `counters.extra["frames_dropped"]`.

Measured on the desktop app, two minutes of live capture with a 14B model, an embedding model
and an HTTP server on the same event loop: **6132 frames, 0 dropped, 0 gaps.** The pipeline is
not starved by the gateway's loop.

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

The 400 ms budget row this contradicted was corrected to 750 ms in
[ADR 0001](../../docs/adr/0001-endpointing-latency-budget.md); the measured p95 on the real
pipeline is 739 ms. The route below 700 ms is not a smaller window but speculative decoding
during it — see the ADR's consequences section.

## Wakeword false accepts

`false_accepts_per_hour` reads a measured record from `research/benchmarks/wakeword/` and
returns **NaN** when there is none — a false accept means the microphone opened when nobody
asked, so the number is never guessed. It also returns NaN when the record was measured at a
different threshold, because FA/hour is a function of the operating point.
`assert_shippable()` raises instead of returning NaN.

There is no `hey_ars` openWakeWord model; a custom keyword has to be trained, so the
deployment uses a stock one (`ARS_WAKEWORD=hey_jarvis` — `alexa` would fire every time
somebody in the house talks to an Echo). A configured keyword with no model on disk raises
`MissingWakewordModel` listing what *is* present, rather than letting openWakeWord treat the
name as a hub id and download something nobody asked for in the always-on, pre-consent part
of the system.

## Fixtures

Two kinds, both generated, both labelled:

* `endpointing/`, `wakeword_*/` — **speech-shaped noise** with controlled onsets, levels and
  pauses, deterministic. Constrains *timing* and nothing else.
* `spoken/` — **real synthesised speech** from the Piper voices, spliced onto a noise floor.
  Real phonetics and prosody, which is what openWakeWord and whisper actually respond to;
  this is what the real-engine benchmark and `tests/unit/voice/test_real_backends.py` use.

Neither is a human in a kitchen. Real FA/hour, real recognition accuracy and voice quality
need real recordings, in both languages, in the room A.R.S will live in.
