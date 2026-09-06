# Voices: more of them, character voices, and German

Researched and measured 2026-09-06 on the target machine (M5 Max, arm64, macOS 25.6,
Python 3.12, `piper-tts` 1.8.0, `onnxruntime` 1.29.0, `mlx-whisper` 0.4.3, numpy 2.5.2,
scipy 1.18.1).

**No audio device was opened at any point.** Every measurement below comes from synthesis
into numpy arrays or WAV files on disk. Auditions were rendered for the owner to listen to;
they were not listened to here.

> **Update, same day, after the research was done.** German landed in the working tree
> while this was being written — `Language.DE`, `SUPPORTED_LANGUAGES = (EN, RO, DE)`,
> `tts_voice_de = "de_DE-thorsten-medium"`, German hesitation tokens, German prompts.
> Section 7 item 7 is therefore largely done, and the chosen German voice matches the
> recommendation in §2 independently. **`services/voice/src/ars_voice/asr/language.py` was
> not touched** (zero diff). That turns §5.2 from a latent design flaw into a **live
> arithmetic bug**, measured below. It is now the top item in §7.

---

## The question

Three decisions, in the owner's priority order:

1. **Many more voices** — female, male, and deliberately synthetic robot/alien characters —
   without leaving the 120 ms time-to-first-audio budget.
2. **German as a third language**, TTS and ASR.
3. Whether Piper is still the right engine in 2026, or whether something else on Apple
   Silicon is worth the migration.

## Recommendation in one paragraph

**Stay on Piper. Add 5 model files, not 20.** Four of the five are *multi-speaker* — one
`.onnx` that contains many voices — which turns "many more voices" from a 1.5 GB download
into a 355 MB one. That gets you **150 measured English voices** (65 male / 66 female /
19 pitch-ambiguous) and **251 measured German voices** (122 / 74 / 55) for 355 MB, all
inside budget. **Do not switch TTS engines**: nothing in 2026 beats Piper on the specific
constraint that actually binds here, which is Romanian. **Do not use `high`-quality Piper
voices anywhere on the interactive path** — measured 395–436 ms to first audio, 3.3–3.6×
the entire budget. **Build robot/alien as a DSP chain, not as another model**: measured
0.08–1.01 ms per 120 ms chunk, bit-exact under chunking, no lookahead. **German ASR needs
no new model** — large-v3-turbo already does it at p=1.00 — but `asr/language.py` carries a
comment asserting a two-language invariant that stopped being true the moment German
shipped, and the arithmetic under it now overstates confidence by up to 2×. **That is the
single highest-value fix in this document**, and it is a handful of lines.

**The one thing you cannot buy your way out of: Romanian has exactly one Piper voice in
existence, and it is male.** Everything else here has an easy answer. That does not.

---

## 1. What Piper actually has for en / ro / de

Source: [`rhasspy/piper-voices/voices.json`](https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json),
downloaded 2026-09-06. 176 voices across 57 locales. Download base URL for everything below:
`https://huggingface.co/rhasspy/piper-voices/resolve/main/<path>` (and `<path>.json` for the
config, which is mandatory — Piper will not load without it).

| locale | voices in catalogue | of which multi-speaker |
|---|---:|---|
| `en_US` | 27 | arctic (18), l2arctic (24), libritts (904), libritts_r (904) |
| `en_GB` | 11 | vctk (109), aru (12), semaine (4) |
| `de_DE` | 10 | mls (236), thorsten_emotional (8 emotions) |
| `ro_RO` | **1** | — |

That `ro_RO: 1` is the headline. `ro_RO-mihai-medium` is the only Romanian voice Piper has
ever shipped. There is no female Romanian Piper voice and no second male one.

### 1.1 Gender mix — measured, not assumed

I could not listen, so I measured median fundamental frequency (F0) instead: synthesise one
sentence per voice/speaker, autocorrelation pitch track over voiced frames, median. This is
a **proxy**, not ground truth. Classification bands: male &lt; 145 Hz, female &gt; 180 Hz,
ambiguous in between (a deliberately wide band — the estimator octave-halves on breathy
voices).

**402 distinct voices/speakers measured.**

| language | measured | male (&lt;145 Hz) | female (&gt;180 Hz) | ambiguous |
|---|---:|---:|---:|---:|
| English | 150 | 65 | 66 | 19 |
| German | 251 | 122 | 74 | 55 |
| Romanian | 1 | 1 | 0 | 0 |

Two places the proxy and the model card disagree, both flagged rather than resolved:
`en_US-kristin-medium` measures 145 Hz but its model card says "US English female voice";
CMU ARCTIC's `slp` is listed male by festvox.org but measures 232 Hz. Trust the listening
pass, not this table, on those two.

### 1.2 Time to first audio — the number that decides everything

`TtsConfig.first_sentence_max_chars` is 90, so I measured TTFA on an ~86–90 character first
sentence in each language, warm, median of 7 runs, one voice per process. This is directly
comparable to the table in `services/voice/src/ars_voice/config.py`.

| voice | lang | TTFA median | vs 120 ms budget |
|---|---|---:|---|
| `en_GB-vctk-medium` (spk 0) | en | **57 ms** | 52% headroom |
| `en_US-lessac-medium` | en | 67 ms | ok |
| `en_US-arctic-medium` (spk 2) | en | 75 ms | ok |
| `en_GB-semaine-medium` (spk 1) | en | 80 ms | ok |
| `en_US-amy-medium` *(current default)* | en | **82 ms** | ok |
| `en_GB-alan-medium` | en | 87 ms | ok |
| `ro_RO-mihai-medium` *(current default)* | ro | **94 ms** | ok, 26 ms spare |
| `de_DE-kerstin-low` | de | 60 ms | ok |
| `de_DE-ramona-low` | de | 62 ms | ok |
| `de_DE-thorsten-medium` | de | **69 ms** | ok |
| `de_DE-thorsten_emotional-medium` (neutral) | de | 84 ms | ok |
| `de_DE-mls-medium` (spk 7) | de | 95 ms | ok, 25 ms spare |
| `en_US-ryan-high` | en | **395 ms** | **3.3× over** |
| `de_DE-thorsten-high` | de | **421 ms** | **3.5× over** |
| `en_GB-cori-high` | en | **436 ms** | **3.6× over** |

Aggregated over all 402 measurements: `high` tier n=3, median first-chunk 128 ms;
everything else n=399, median 30 ms, p95 51 ms, max 64 ms (short first sentence).

**The `high` tier is disqualified.** It is not close and no amount of tuning
`first_sentence_max_chars` recovers it — at the 20-char sentence where a medium voice takes
15 ms, cori-high already takes 88 ms. If the owner wants a "prestige" narration voice for
long non-interactive output (reading an article aloud), `en_GB-cori-high` is public domain
and fine *there*, on a path with no latency budget. Never on a turn.

Confirmation of the existing docs: amy at 86 chars measured 82 ms against the 85 ms the
config docstring predicts. The existing numbers in the repo are sound.

### 1.3 Cost of holding voices resident

Measured RSS delta per loaded-and-primed voice, same process:

| | medium (63–77 MB file) | high (114 MB file) |
|---|---:|---:|
| voice load | ~315 ms | ~410 ms |
| priming synthesis | 20–29 ms | 92 ms |
| **resident RSS** | **95–110 MB** | **163 MB** |

Eight voices resident = 989 MB RSS and 2.5 s of startup. This kills the obvious design.
`PiperTtsEngine.warm_up()` currently preloads every voice, which is correct for two voices
and wrong for twenty.

**Design consequence:** preload exactly the three *active* voices (one per language). Every
other voice is lazy-loaded on first use and evicted LRU with a cap (suggest 5 resident,
~550 MB). A persona switch pays ~340 ms once; that is acceptable because it is a deliberate
user action, not a turn.

### 1.4 Licences

Personal private use is fine for all of these. Flagged for the record because A.R.S is
described as a product:

* **CC0 / public domain** — `ro_RO-mihai`, all `de_DE-thorsten*`, `de_DE-kerstin`,
  `en_US-joe`, `en_US-mike`, `en_GB-cori`, `en_US-kristin`, `en_US-norman`, `en_US-john`,
  `en_US-bryce`. No obligations.
* **CC BY 4.0** — `en_GB-vctk`, `en_GB-alba`, `de_DE-mls`, `en_US-libritts_r`. Attribution.
* **Apache-2.0** — `en_US-sam` (the Accenture non-binary voice).
* **CC BY-NC-SA 4.0 — non-commercial** — `en_US-hfc_female`, `en_US-hfc_male`,
  `en_US-ryan-high`, `en_GB-semaine`, `de_DE-pavoque`. Fine for the owner's own machine.
  A blocker if A.R.S is ever distributed commercially with these bundled.
* **"See URL"** — `en_GB-alan` (Mycroft mimic3 / apope), `de_DE-eva_k`, `de_DE-karlsson`,
  `de_DE-ramona` (M-AILABS), `en_GB-jenny_dioco`. Needs a read before shipping.

Recording licence per voice in `MODEL_CARD` next to each `.onnx`.

---

## 2. The voices I would add

Five files. 355 MB. Everything is measured except where marked.

| file | lang | speakers | size | what it buys | licence |
|---|---|---:|---:|---|---|
| `en_GB-vctk-medium` | en_GB | **109** | 77 MB | 45 M / 57 F / 7 ambiguous, British + Scottish + Irish + American accents, F0 81–256 Hz. **Fastest voice measured (57 ms).** | CC BY 4.0 |
| `en_US-arctic-medium` | en_US | **18** | 77 MB | Accented English: Scottish, Canadian, Indian (×4), German (×2), Israeli. 12 M / 6 F per festvox. Character range you cannot get elsewhere. | see LICENSE |
| `en_GB-semaine-medium` | en_GB | **4** | 77 MB | **Purpose-built characters.** `spike` (97 Hz, aggressive male), `obadiah` (106 Hz, gloomy male), `prudence` (199 Hz, level female), `poppy` (272 Hz, bright female). From the SEMAINE sensitive-artificial-listener corpus — these were designed as personalities. | CC BY-NC-SA 4.0 |
| `de_DE-thorsten-medium` | de_DE | 1 | 63 MB | The German default. 133 Hz male, CC0, 69 ms TTFA. | CC0 |
| `de_DE-thorsten_emotional-medium` | de_DE | **8** | 77 MB | Same speaker in 8 emotions: `neutral` `amused` `angry` `disgusted` `drunk` `sleepy` `surprised` `whisper`. `whisper` is genuinely useful for a JARVIS product (night mode). `sleepy` runs 1.56× longer than `neutral` — the emotion is in the timing, not just the pitch. | CC0 |

**Second tier, add only if the listening pass wants more single-speaker character** (63 MB
each): `en_GB-alan-medium` (99 Hz British male — closest thing to the JARVIS archetype in
the catalogue), `en_US-sam-medium` (138 Hz, Apache-2.0, deliberately non-binary),
`en_US-hfc_female-medium` (256 Hz, brightest single-speaker female measured),
`en_GB-jenny_dioco-medium` (199 Hz), `en_GB-alba-medium` (201 Hz Scottish female),
`de_DE-ramona-low` (193 Hz German female, native 16 kHz so no resample at all),
`de_DE-kerstin-low` (172 Hz, CC0, 16 kHz).

**`de_DE-mls-medium` (236 German speakers, 77 MB, CC BY 4.0) — I am not recommending it
yet.** It measures fine on latency (95 ms) and gives 116 M / 82 F / 38 ambiguous, but it is
trained from LibriVox audiobook recordings, was trained from scratch rather than fine-tuned,
and per-speaker quality will be all over the place. In my German ASR test the one MLS voice
I used was the *only* case Whisper misheard ("E-Mails … kurz" came out "Malz … kopfz") —
the model's own diction failed, not the recogniser. Treat it as a 236-voice grab bag to be
curated down to maybe 10 after listening, not as a shipped feature.

### Curated starting points for the listening pass

VCTK, spanning the measured F0 range (speaker id in the model's `speaker_id_map`):

`p254`(76) 81 Hz · `p347`(32) 86 · `p345`(75) 94 · `p270`(12) 101 · `p259`(4) 116 ·
`p374`(25) 126 · `p299`(68) 176 · `p297`(52) 185 · `p300`(72) 195 · `p257`(18) 208 ·
`p323`(31) 230 · `p317`(36) 256

MLS German, if you pursue it: `11480`(150) 83 Hz · `9948`(97) 97 · `11355`(229) 112 ·
`11987`(233) 140 · `8567`(219) 178 · `1474`(66) 197 · `3885`(15) 240

### Romanian: the honest answer

There is no second Romanian Piper voice to add. Three options, none free:

1. **Do nothing.** Romanian keeps one voice; personas are English/German-only. Violates the
   spirit of CLAUDE.md #5 ("a feature that works only in English is unfinished") in a way
   that will be visible the moment the owner picks a persona and Romanian does not follow.
2. **Derive variants from mihai by pitch/formant shifting** (§3.3). Costs nothing and is
   already implemented in the prototype. Gets you a *different* Romanian timbre. It will not
   get you a convincing female voice from a male one — formant-shifting a male voice up
   ~1.2× produces something young and androgynous, not female. Renders are in the audition
   set at k = 0.85 / 1.18 / 1.30; judge them yourself.
3. **Train Romanian Piper voices on SWARA** — 21 hours, 17 non-professional speakers, 44.1
   kHz, on [Zenodo](https://zenodo.org/records/15736789), used by the 2026 IEEE Access study
   *["How Open is Open TTS? A Practical Evaluation of Open Source TTS Tools for
   Romanian"](https://arxiv.org/html/2603.24116v2)* (published April 2026). That study rated
   **VITS first in human listening tests for naturalness and speaker similarity** (SECS
   0.92) on exactly this corpus — and Piper *is* VITS. Piper ships a training guide
   ([TRAINING.md](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/TRAINING.md)) and
   fine-tuning from `ro_RO-mihai-medium` should need far less than the from-scratch budget.
   This is the only route to real Romanian voice variety, and it is a multi-day project, not
   a config change. **Estimate, not measurement:** fine-tuning one medium voice from a
   1-hour subset is typically single-digit GPU-hours; I have not run it.

My read: do (2) now because it is free, and put (3) on the roadmap as its own piece of work.
Do not pretend (1) is fine.

---

## 3. Robot and alien voices

The two honest options were (a) a distinct TTS model or (b) a DSP chain on Piper output.

**(a) is not worth it and I want to be blunt about why.** A distinct model costs another
63–115 MB and another ~100 MB resident *per character*, has to exist in all three languages
(it will not — nothing does Romanian), and no open model is actually trained on robot or
alien speech; you would be fine-tuning one, which is the SWARA project all over again for a
much lower payoff. Meanwhile the entire sci-fi canon of robot voices — Dalek, Cylon, HAL,
vocoder-Vader, Star Trek universal translator — is **DSP applied to a human recording**. The
effect *is* the character. Doing it in a model would be imitating an effect with a
neural net.

**(b) works, is nearly free, and is language-independent** — which for a trilingual product
where Romanian has one voice is the decisive property. A DSP character applies identically
to amy, mihai and thorsten.

### 3.1 Measured

Reference implementation at
`/Users/alexandrustratulat/ars/research/experiments/character_dsp.py`, operating on the real
format: 16 kHz mono int16, 1920-sample (120 ms) chunks — exactly `bytes_for_ms(chunk_ms)`.

| preset | chain | ms / 120 ms chunk (median) | p95 | % of realtime |
|---|---|---:|---:|---:|
| `robot_dalek` | ring 30 Hz → comb 3 ms fb .7 → crush 7-bit hold 3 | **0.083** | 0.096 | 0.07% |
| `robot_ring` | ring 50 Hz mix .92 → comb 5.5 ms fb .55 → crush 8-bit hold 2 | **0.122** | 0.135 | 0.10% |
| `alien_swarm` | 3-tap chorus (11/17/23 ms, deep slow LFOs) → ring 410 Hz mix .3 | **0.159** | 0.170 | 0.13% |
| `alien_ring` | ring 230 Hz mix .45 → 3-tap chorus mix .55 → comb 11 ms fb .45 | **0.341** | 0.361 | 0.28% |
| `robot_vocoder` | 16-band channel vocoder, sawtooth carrier 105 Hz + noise | **1.009** | 1.186 | 0.84% |

Worst case adds **~1 ms** to a 57–95 ms TTFA. The budget is untouched.

Correctness, verified rather than asserted:

* **Chunk-size invariance** — identical output at 320 / 640 / 1920-sample chunks to
  2×10⁻⁶ relative RMS. The state really is carried; the effect does not depend on the chunk
  grid, so `chunk_ms` stays a free knob.
* **No boundary clicks** — max |x[n] − x[n−1]| at a chunk boundary is *below* the 99.99th
  percentile of interior deltas for every preset (e.g. `robot_ring` 0.45 at boundaries vs
  0.53 interior).
* **Vocoder is bit-exact** chunked vs whole-buffer (0.00e+00).
* **No lookahead anywhere.** Nothing needs a future sample.

### 3.2 Parameter values that have reason to work

* **Ring modulation** is the single highest-value effect and the cheapest. `y = x·cos(2πft)`.
  **f = 30 Hz, mix 1.0** is the Dalek: below the pitch range, so it reads as a buzz welded
  onto the voice rather than a second note. **f = 50 Hz, mix 0.92** is a cleaner "computer".
  Above ~150 Hz it stops sounding mechanical and starts sounding *alien*, because the
  sidebands at f₀±f are no longer harmonically related — **f = 230 Hz, mix 0.45** is the
  alien setting. **The phase accumulator must persist across chunks**; reset it per chunk and
  you get a click at 8.33 Hz.
* **Bit crushing.** Quantise to **7–8 bits** and sample-and-hold every **2–3 samples**
  (16 kHz → 5.3–8 kHz effective). Cheap, and it is what makes a ring-modulated voice sound
  *digital* rather than merely distorted. The hold grid needs a cross-chunk offset.
* **Feedback comb**, delay **3–6 ms**, feedback **0.55–0.7**: a resonance at 170–330 Hz that
  reads as a metallic throat. Implement as `lfilter(b=[1], a=[1,0,…,0,−fb])` with `zi`
  carried — vectorised and exactly streamable. My first version was a per-sample Python loop
  and cost 3× more for identical output.
* **Chorus / detune** for alien. 3 taps at **11 / 17 / 23 ms** with **deep (0.9–1.7 ms) and
  slow (0.7–1.7 Hz)** LFOs, mix 0.75 → "several of it talking at once", the hive/collective
  read. Shallow-and-fast (0.13–0.31 ms at 2.6–4.1 Hz) instead gives ordinary chorus, which
  is a *nicer* voice, not a stranger one.
* **Channel vocoder** for the classic monotone robot. 16 geometric bands 150 Hz–6.5 kHz,
  2nd-order Butterworth, envelope follower at 22 Hz, **sawtooth carrier at 105 Hz**
  (monotone — the flat pitch is most of the effect). **Add ~0.25 of white noise to the
  carrier** or every fricative disappears and it becomes unintelligible. Most expensive
  preset and still under 1% of realtime.
* **Reverb: not included, and I would not.** It is the one effect that needs a tail after the
  last chunk, which means the engine has to keep emitting after Piper is done — and that
  fights `cancel()`/barge-in for a small aesthetic gain.

### 3.3 The free formant shift

`_iter_piper_audio` already resamples 22050 → 16000 at the backend edge. **Lie about the
source rate and you get a pitch *and formant* shift for zero additional cost.** Resample as
if the source were `22050·k`; pitch and formants both scale by k. Cancel the duration change
by passing `length_scale = k` to Piper.

Measured on `en_US-amy-medium`, F0 200 Hz baseline:

| k | intended | measured F0 | measured shift | duration vs baseline |
|---:|---|---:|---:|---:|
| 0.80 | deeper / bigger | 163 Hz | −3.51 st (predicted −3.86) | 1.10× |
| 0.88 | slightly deeper | 180 Hz | −1.85 st (predicted −1.99) | 1.05× |
| 1.15 | higher / smaller | 232 Hz | +2.56 st (predicted +2.42) | 0.96× |
| 1.30 | much smaller | 262 Hz | +4.69 st (predicted +4.54) | 0.95× |

The pitch shift is accurate to ~0.15 semitones. **The duration compensation is only good to
~10%** — `length_scale` does not scale pauses linearly — so if exact duration matters,
trim `length_scale` empirically per k. This is unlike a naive pitch shifter: because
formants move with pitch, k&lt;1 sounds like a *physically larger* speaker rather than a
slowed-down one. That is what makes it usable for alien, and it is the only lever available
for a second Romanian timbre.

### 3.4 Where this belongs in the code

Not inside `PiperTtsEngine`. A `CharacterTtsEngine(TtsEngine)` that wraps another
`TtsEngine` and maps its `SynthesisChunk` stream keeps CLAUDE.md #2 intact (no vendor SDK
leakage, and the effect works over any backend), keeps `cancel()` semantics (the wrapper just
stops pulling), and keeps the DSP testable without weights. The formant-shift `k` is the one
exception — it has to reach Piper's resample and `length_scale`, so it is a constructor
argument on `PiperTtsEngine`, not a wrapper concern.

---

## 4. Is a different TTS engine worth it in 2026?

| engine | en | ro | de | size | licence | MLX / Metal | TTFA on this box | verdict |
|---|:-:|:-:|:-:|---|---|---|---|---|
| **Piper** (current) | ✅ | ✅ | ✅ | 63–77 MB/voice | MIT engine, per-voice weights | ONNX Runtime CPU | **57–95 ms measured** | **Keep** |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | ✅ | ❌ | ❌ | 82M / ~330 MB | Apache-2.0 | yes, via mlx-audio | fast (not measured) | **No — no ro, no de.** 8 languages: en/es/fr/hi/it/ja/pt/zh. Ends the discussion. |
| [Chatterbox Multilingual V3](https://huggingface.co/ResembleAI/chatterbox) | ✅ | ❌ | ✅ | 500M | MIT | no first-party MLX | ≫120 ms (autoregressive) | **No.** HF card language list is `ar da de el en es fi fr he hi it ja ko ms nl no pl pt ru sv sw tr zh` — **no `ro`**, despite third-party blogs claiming otherwise. Also watermarks output by default. |
| Chatterbox Turbo / Nano | ✅ | ❌ | ❌ | 350M / 110M | MIT | no | — | English only. |
| [XTTS-v2](https://huggingface.co/coqui/XTTS-v2) | ✅ | ❌ | ✅ | ~1.8 GB | Coqui CPML (non-commercial) | no | ≫120 ms | **No.** 17 languages, no Romanian. Coqui is defunct; this is unmaintained. |
| [facebook/mms-tts-ron](https://huggingface.co/facebook/mms-tts-ron) | — | ✅ | — | VITS, ~145 MB | **CC BY-NC 4.0** | no (transformers/torch) | not measured | **Only interesting as a second Romanian timbre.** Single speaker, 16 kHz native. Non-commercial. Worth an hour to audition; not worth an engine migration. |
| Supertonic / Qwen3-TTS / MOSS-TTS etc. | ✅ | ? | ✅ | — | — | partial | vendor-claimed 97 ms | **Not evaluated.** Every 2026 "best local TTS" list I found is SEO content with no Romanian row. Revisit only if Romanian appears on a first-party model card. |
| [F5-TTS-RO](https://arxiv.org/pdf/2512.12297) | — | ✅ | — | F5-TTS + adapter | paper CC BY 4.0 | no | — | Dec 2025 paper, Romanian via lightweight input adaptation. **Could not confirm weights are released.** Worth watching; not actionable today. |

**Piper wins on the constraint that binds.** Every candidate that beats it on naturalness
loses Romanian, and most also blow the latency budget because they are autoregressive. Piper
is non-autoregressive VITS, which is precisely why it can hit 57 ms. The 2026 Romanian study
above independently rates VITS top for naturalness on Romanian in human listening. There is
no migration here worth making.

The one real limitation to record: **Piper runs on ONNX Runtime CPU, not Metal.** That is
not costing anything today (69 ms with a 120 ms budget) but it means TTS competes with the
LLM for CPU, and `high` voices are unusable. If someone ports Piper's VITS decoder to MLX,
the `high` tier becomes available and this document changes.

---

## 5. German ASR

### 5.1 The model: nothing to do

MLX Whisper large-v3-turbo, the model already in the repo, through the exact call shape
`MlxWhisperEngine` uses (one `lang_id` decode → constrain → one `transcribe` decode). Audio
was Piper-synthesised German; no microphone.

| said | detected | p(de) | transcribed |
|---|---|---:|---|
| Guten Morgen. Ich habe drei neue Nachrichten von der Bank gefunden. | `de` | **1.00** | ✅ exact |
| Stell einen Wecker für halb acht und erinnere mich daran, Milch zu kaufen. | `de` | **1.00** | ✅ ("halb 8") |
| Wie ist das Wetter heute in München? Brauche ich einen Regenschirm? | `de` | **1.00** | ✅ exact |
| Schalte bitte das Licht im Wohnzimmer aus, bevor ich das Haus verlasse. | `de` | **1.00** | ✅ exact |
| Fasse mir die drei wichtigsten E-Mails von gestern Abend kurz zusammen. | `de` | **1.00** | ⚠️ "Malz … kopfz" |
| Nein danke. | `de` | **1.00** | ✅ |
| Ja. | `de` | 0.66 | ✅ (en 0.24) |

Decode latency 187–232 ms warm, same as EN/RO, comfortably inside the 250 ms ASR row.
The one miss is on `de_DE-mls-medium`, whose own diction is poor — a TTS artifact of the test
fixture, not an ASR weakness. **Say it plainly: German ASR requires no new model, no new
download, and no config change to the engine.**

Caveat stated up front: this is synthetic speech, so these numbers are an **upper bound**.
Real-world German WER on a real microphone in a real room will be worse, and the honest
number will only come from real fixtures.

### 5.2 The language arbiter: a live bug

This is the most important finding in the document.

#### 5.2.1 What was wrong before German landed

`constrain_probabilities` renormalises Whisper's distribution over `SUPPORTED_LANGUAGES`.
When the true language is not in that set, renormalising a near-zero numerator by a
near-zero denominator produces a **confidently wrong answer**. Measured against the
two-language support set that existed when this research started:

```
said     : Stell einen Wecker für halb acht und erinnere mich daran, Milch zu kaufen.
constrain: en @ conf 1.00     arbiter -> en ("unchanged")
heard    : I set a timer for half 8 and remember to make me a milk.

said     : Wie ist das Wetter heute in München? Brauche ich einen Regenschirm?
constrain: en @ conf 1.00     arbiter -> en ("unchanged")
heard    : How is the weather today in Möncheng? I need a regular system.
```

Whisper reports German at p=1.00. The support set forces English. Whisper then *translates*.
The pipeline reports **confidence 1.00** on a fabricated English sentence — which is
verbatim the failure the `NoLanguageDistribution` docstring in `mlx_whisper_engine.py` says
is unrecoverable ("fluent, confident and completely wrong … a plausible mistranslation is
not [recoverable]"). The guard exists for a null distribution but not for a distribution
whose mass is entirely outside the support set.

Adding `Language.DE` fixes this **for German specifically**, as a side effect. The hole
itself is untouched and is still live for French, Italian, Spanish — anything the household
might say that A.R.S does not speak.

#### 5.2.2 What is wrong *now*, because German landed and the arbiter did not

The refusal branch computes `confidence = 1.0 - confidence` under the comment *"Binary
support set, so the retained language's probability is the complement."* That premise was
true this morning. `SUPPORTED_LANGUAGES` now has three entries, so it is false. Measured
against the tree as it stands:

```
SUPPORTED_LANGUAGES is now: ['en', 'ro', 'de']

distribution {de 0.50, en 0.30, ro 0.20}, current language EN, utterance "Ja"
  detected de @ 0.50 -> switch refused, kept en
  reported confidence in EN : 0.50      <- 1.0 - 0.50
  ACTUAL P(en)              : 0.30
  overstated by             : +0.20

distribution {de 0.34, en 0.33, ro 0.33}  (a three-way tie)
  reported confidence in EN : 0.66
  ACTUAL P(en)              : 0.33       <- overstated 2x
```

On a three-way near-tie the arbiter claims **twice** the confidence it has. `confidence`
is not decoration: it lands in `Transcript.language_confidence` and propagates to whatever
downstream logic trusts it. The comment in the code says this value is *"reported honestly
rather than as a fabricated 1.0"* — as of today it is fabricated again, just less obviously.

The fix is small and mechanical: `constrain_probabilities` should return the whole
constrained distribution, and the refusal branch should report `scores[self.current]`
instead of `1.0 - confidence`. It is a handful of lines and it does not require touching
any threshold.

### 5.3 What `asr/language.py` needs for three languages

Four changes, in order of importance:

1. **Fix the complement in the refusal branch — this one is a live bug, not a design
   improvement.** See §5.2.2 for the measured 2× overstatement. `constrain_probabilities`
   must hand back the whole constrained distribution so the arbiter can report
   `scores[self.current]` instead of inventing a complement. Also delete the now-false
   "Binary support set" comment; a stale comment asserting a broken invariant is how this
   survived the German change in the first place.
2. **Report out-of-support mass.** `constrain_probabilities` must return the pre-normalisation
   total (or an `in_support` fraction) alongside the winner. If `total < ~0.5`, the honest
   answer is "this is not a language I speak", and the engine should say so rather than
   decode. Adding German fixed the German instance of this; the hole is still open for every
   other language.
3. **Pairwise hysteresis, not global.** `switch_confidence = 0.70` and
   `weak_switch_confidence = 0.55` are single scalars. With three languages the confusion is
   not symmetric — EN↔DE share far more surface than RO↔DE, and the `Ja.` case above (de 0.66
   / en 0.24) is exactly where a single global threshold misbehaves. Minimum viable version:
   keep the scalars but require the winner to beat the *runner-up* by a margin, not just beat
   an absolute threshold. That generalises to N languages and is a small change.
4. **The one-word case gets worse, not better.** `da`/`ja`/`nu`/`no`/`yes` are already the
   documented failure mode, and adding German adds `ja` (≈ RO `da` phonetically) and `nein`.
   The existing `min_words_for_weak_switch = 3` guard is the right instinct; it should be
   raised for switches into a language with a phonetically-colliding affirmative, or those
   tokens should be excluded from triggering switches entirely.

### 5.4 Everything else German touches

`Language` is a `StrEnum` in `packages/protocol` with a `display_name` map; adding `DE = "de"`
is one line plus `SUPPORTED_LANGUAGES`. The docstring there is right that "adding one is a
project, not a config change" — `Language.RO` appears in 20 files. The ones that need real
German content, not just an enum arm:

* `services/voice/src/ars_voice/vad/endpointing.py` — `DEFAULT_HESITATION_DE` (`äh`, `ähm`,
  `also`, `und`, `aber`, `oder`, `weil`, `der`, `die`, `das`, `zu`, `mit`, `mein`, `dein`).
  Needs the same fixture-based before/after the file demands for any endpointing change.
* `services/compute/src/ars_compute/language.py` and `learning.py` — 6 `Language.RO` refs each.
* `services/auth/src/ars_auth/messages.py` — 6 refs; user-facing consent strings, CLAUDE.md #5
  means these must exist in German before German ships.
* `services/voice/src/ars_voice/fixtures.py` — 10 refs; German spoken fixtures.
* `packages/core/src/ars_core/config.py` — `tts_voice_de`, plus `ArsConfig.languages`.
* `MlxWhisperEngine._load` warms one decode per language in `SUPPORTED_LANGUAGES`; a third
  adds **~200 ms to startup** *(derived from the measured ~190–230 ms per decode, not
  separately measured)*.
* `PiperTtsEngine.voice_for` is a two-way ternary and must become a mapping.
* `scripts/fetch_voice_models.sh` `fetch_tts()` loops over exactly two voices.

---

## 6. Method

* **Catalogue** — `voices.json` from the official repo, 2026-09-06; `MODEL_CARD` fetched per
  voice for licence and dataset provenance.
* **Latency** — one voice per process (a first pass with 17 models resident inflated every
  number 1.5–2×; anchors were re-measured to calibrate and reproduce to within ±20%).
  Warm-up synthesis discarded, then median of 7 runs at the configured 90-char first sentence.
* **Gender** — median F0 by autocorrelation over voiced frames (40 ms window, 10 ms hop,
  RMS gate 0.02, peak-correlation gate 0.35, ≥8 voiced frames required). A **proxy**.
* **DSP** — processed in 1920-sample chunks, validated by chunk-size invariance (320/640/1920),
  boundary-delta comparison, and chunked-vs-whole equality where the effect is deterministic.
* **German ASR** — `mlx_whisper.decoding.decode` directly, mirroring `MlxWhisperEngine`, on
  Piper-synthesised German.

Reproduce: `/Users/alexandrustratulat/ars/research/experiments/character_dsp.py` and
`/Users/alexandrustratulat/ars/research/experiments/voice_audition.py`.
32 auditions already rendered to `/Users/alexandrustratulat/ars/var/voice-auditions/`
(gitignored). **Somebody has to listen to them. No number in this document is a substitute.**

## 7. Implementation plan, ordered by value for effort

| # | work | effort | why here |
|---|---|---|---|
| 1 | **Fix `LanguageArbiter`'s `1.0 - confidence` complement** (§5.2.2), and have `constrain_probabilities` report out-of-support mass (§5.3 item 2). | S | German shipped into `SUPPORTED_LANGUAGES` today and `asr/language.py` was not touched. The complement is now arithmetically wrong on a live three-language system — measured 2× overstatement on a three-way tie — and it feeds `Transcript.language_confidence`. Handful of lines. Independent of everything else here. |
| 2 | **`speaker_id` support in `PiperTtsEngine`** + honour `SynthesisRequest.voice` (currently ignored — `voice_for()` only knows language). | S | Unlocks 4 of the 5 recommended files. `piper.config.SynthesisConfig` already has the field; it is simply never set. Nothing else in this document works without it. |
| 3 | **Lazy load + LRU eviction; preload only the active voice per language.** | S–M | 95–110 MB and 315 ms per resident voice makes the current preload-everything `warm_up()` untenable past ~5 voices. Do it before adding voices, not after. |
| 4 | **Fetch + audition the 5 files** (355 MB); pick the roster from listening. | S | The download is trivial; `fetch_voice_models.sh` already parses these names correctly (verified, including `x_low`). Only the two-voice loop in `fetch_tts()` needs generalising. |
| 5 | **`CharacterTtsEngine` wrapper** with `robot_ring` / `robot_dalek` / `robot_vocoder` / `alien_ring` / `alien_swarm`. | M | ~1 ms per chunk for the whole sci-fi register, in all three languages at once, from a validated reference implementation. Highest character-per-effort ratio in the document. |
| 6 | **Formant-shift `k`** on `PiperTtsEngine` (reuse the existing resample; `length_scale = k`). | S | Free — the resample already happens. Also the only lever for a second Romanian timbre. |
| 7 | ~~**German**: `Language.DE`, `tts_voice_de`, hesitation tokens, EN/RO/DE strings, `de_DE-thorsten-medium`.~~ **Largely done in the working tree today.** Remaining: `thorsten_emotional`, German spoken fixtures, and the +~200 ms startup from the third warm-up decode. | L → S | ASR was genuinely free (§5.1) and the voice choice matches §2 independently. What is *not* done is item 1, which should have been part of this. |
| 8 | **Pairwise / margin-based language hysteresis.** | M | Now that DE exists, this pays off immediately — `Ja` vs `Yeah` vs `Da` is the exact collision a single global threshold mishandles. |
| 9 | **Romanian voices from SWARA** (fine-tune from `mihai`). | XL | The only real fix for `ro_RO: 1`. Own project. |
| — | ~~Switch TTS engine~~ | — | **Do not.** Nothing in 2026 does en+ro+de locally inside 120 ms. |
| — | ~~`high`-quality voices on the turn path~~ | — | **Do not.** 395–436 ms measured. |
| — | ~~A separate robot/alien model~~ | — | **Do not.** §3. |

## 8. What would change these conclusions

* **A listening pass that says the VCTK/ARCTIC/MLS multi-speaker voices sound bad.** This is
  the largest open risk: my entire "many voices, cheap" recommendation rests on 4 multi-speaker
  files whose *perceptual* quality I could not assess. If they sound like compressed
  audiobooks, the answer collapses back to ~10 single-speaker files at 63 MB each.
* **A Romanian voice appearing in `piper-voices`**, or F5-TTS-RO releasing weights, or any
  first-party model card adding `ro`. Romanian is the constraint; relieve it and the whole
  engine comparison reopens.
* **Piper's VITS decoder getting an MLX port.** Would put `high` back inside budget and change
  §1.2 entirely.
* **Real-microphone German fixtures showing WER far worse than the synthetic upper bound.**
  Would turn §5.1's "nothing to do" into a fine-tuning question.
* **A commercial release.** Would strip the CC BY-NC-SA voices (`semaine`, `hfc_*`,
  `ryan-high`, `pavoque`) out of the roster — including the four purpose-built SEMAINE
  characters, which are the best character voices in the catalogue.
* **The latency budget moving.** Everything here is measured against 120 ms. At 250 ms the
  `high` tier and possibly Kokoro-for-English come back into play.

## 9. What I did not do

* **Did not listen to anything.** No perceptual quality judgement in this document.
* Did not measure real-microphone anything — no device was opened.
* Did not evaluate `en_US-libritts_r-medium` (904 speakers), `en_US-l2arctic`, `en_GB-aru`,
  or the `low`/`x_low` English tier.
* Did not benchmark Kokoro, Chatterbox, XTTS or MMS-TTS on this machine — all were ruled out
  on language coverage from first-party model cards before latency mattered.
* Did not measure `mms-tts-ron`, which is the one remaining unexplored Romanian option.
* Did not run German end-to-end through `VoicePipeline`; §5 exercised the engine's call shape
  directly.
* Wrote no code outside `research/`. `models/tts/` was populated with 2.1 GB of Piper weights
  (gitignored) and `var/voice-auditions/` with 32 renders (gitignored).

## Sources

* [rhasspy/piper-voices `voices.json`](https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json) — retrieved 2026-09-06
* [rhasspy/piper-voices sample player](https://rhasspy.github.io/piper-samples) — audition without downloading
* [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl) — Piper engine, and [TRAINING.md](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/TRAINING.md)
* ["How Open is Open TTS? A Practical Evaluation of Open Source TTS Tools for Romanian"](https://arxiv.org/html/2603.24116v2) — IEEE Access, April 2026
* [SWARA Romanian speech corpus](https://zenodo.org/records/15736789) — 21 h, 17 speakers
* ["F5-TTS-RO: Extending F5-TTS to Romanian TTS via Lightweight Input Adaptation"](https://arxiv.org/pdf/2512.12297) — December 2025
* [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) — Apache-2.0, 8 languages
* [ResembleAI/chatterbox](https://huggingface.co/ResembleAI/chatterbox) — MIT, 23 languages, no `ro`
* [coqui/XTTS-v2](https://huggingface.co/coqui/XTTS-v2) — CPML non-commercial, 17 languages, no `ro`
* [facebook/mms-tts-ron](https://huggingface.co/facebook/mms-tts-ron) — CC BY-NC 4.0, Romanian VITS
* [CMU ARCTIC databases](http://festvox.org/cmu_arctic/) — per-speaker gender and accent
* [VCTK corpus](https://datashare.ed.ac.uk/handle/10283/3443) — CC BY 4.0
