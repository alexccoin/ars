# Real-time translation in A.R.S — decision document

*Owner: `ml-engineer`. Written 2026-09-06 against commit `b225a77` + the in-flight German
work. Every number below was measured on this machine unless it is explicitly labelled
"published" or "estimate".*

Machine: `macOS-26.6.2-arm64`, M5 Max. Reasoning model `qwen3:14b` (Q4_K_M, 9.3 GB) via
Ollama, already resident. ASR `mlx-community/whisper-large-v3-turbo` on Metal. TTS Piper
`en_US-amy` / `ro_RO-mihai` / `de_DE-thorsten` medium.

Harness written for this document, all of it runnable:

| Path | What it measures |
|---|---|
| `research/benchmarks/translation/fixtures.py` | 24 EN/RO/DE parallel sentences in four domains, plus a German lease and its EN/RO/DE questions |
| `research/benchmarks/translation/chrf.py` | chrF++ with no dependency; the floors that decide ship/no-ship |
| `research/benchmarks/translation/numerals.py` | reference-free: did every numeric **value** survive, read under the target's own separator convention |
| `research/benchmarks/translation/quality.py` | quality + latency per direction, for any Ollama model or any CTranslate2 model |
| `research/benchmarks/translation/ingest_retrieval.py` | the three-arm document-tier experiment |

---

## 0. Recommendation in one page

**Build translation at ingest first. It is not a translation feature, it is the fix for a
measured hole in the document tier, and it closes it.**

`ars_gateway.brain.TierConfig.document_threshold` says tier 1 is same-language-only
because no threshold separates a cross-language match from an irrelevant question, and
that a bigger embedding model does not fix it (e5-base: −0.007 separation). Both true.
The document says nothing about the third option, which is to stop asking the embedding
model to bridge languages and bridge them at ingest instead. Measured, on a German lease:

| Question language | today (German indexed as-is) | translated at ingest | mean confidence, before → after |
|---|---:|---:|---|
| German | 4/4 answered | 4/4 | 98% → 98% |
| **English** | **1/4** | **4/4** | **25% → 100%** |
| **Romanian** | **0/4** | **3/4** | **0% → 75%** |
| irrelevant questions, highest cosine | 0.811 | 0.811 | noise floor unmoved |

Six of twelve questions move from "falls through to a 4-8 s GPU turn" to "answered from
the file in ~20 ms", the noise floor does not move, and the embedding model is untouched.
That is the largest single win available here and it costs no hot-path latency at all.

Then, in order: **text translation on request** (small, useful, reuses everything), then
**live interpreter mode** (large, flashy, and the only one that does not fit the latency
budget).

**Use `qwen3:14b`, the model already loaded.** It is good enough on all six directions
(chrF++ 59-82), it is already warm, it costs zero extra RAM and zero dollars, it streams,
and it cancels. Do not add a dedicated NMT model until an eval says qwen3 is the problem.

**Do not put interpreter mode inside the 1400 ms budget.** Measured, it lands at
~1650 ms p50 / ~2050 ms p95. Give it its own documented budget rather than pretending, or
pay for a dedicated NMT model — that trade is spelled out in §3.

---

## 1. The three modes, ranked

### #1 — Document translation at ingest. Build this. (mode 2 in the brief)

This is the one that closes a real hole, and it is the cheapest of the three.

`research/benchmarks/translation/ingest_retrieval.py` runs three arms over the same German
lease, the same twelve questions (four German, four English, four Romanian), the same six
irrelevant questions, and the same tier-1 gate from `TierConfig` (0.85 calibrated
confidence, 0.04 ambiguity margin, 80-char minimum):

```
  q lang    A as-is   B doc-xlat   C query-xlat   A conf  B conf  C conf
      de        4/4          4/4            4/4     98%    98%    98%
      en        1/4          4/4            3/4     25%   100%    75%
      ro        0/4          3/4            2/4      0%    75%    50%
  arm C adds 379 ms to every tier-1 question; arms A and B add nothing.
  irrelevant question, highest cosine: A 0.811  B 0.811  C 0.792
```

Three things worth reading twice:

**Arm B wins and costs nothing at query time.** The translations are produced once, at
upload. Retrieval afterwards is exactly what ships today.

**Arm C — translating the question instead of the document — loses on both axes.** It
answers fewer questions (5/8 of the cross-language ones against 7/8) *and* it puts a
379 ms p50 translation on the hot path of a tier whose entire justification is that it
costs ~20 ms and is therefore cheaper than waking the model. It loses on quality because a
five-word question translated without context picks the wrong term: `Cât este garanția?`
became `Was ist die Garantie?` where the lease says `Kaution`, and scored 0.769. A whole
paragraph translated with its own context does not have that problem. Arm C is the option
that *looks* cheaper and is not; it is in the script so nobody has to re-argue it.

**The ambiguity margin has to change, and this is a trap.** `try_cheap_tiers` rejects a
hit whose runner-up is within 0.04, because two passages scoring alike means the question
is ambiguous. Index three translations of the same paragraph and the runner-up to every
hit is *its own twin*. Measured with the rule exactly as written today
(`--no-dedupe`), 2 of 12 questions are rejected as "ambiguous: runner-up 100% within 4%"
— including two that arm A answered. The fix is one line of policy: **the margin must be
computed against the best hit from a different source chunk**, which requires a translated
chunk to know which original it came from. That is the `origin` field in the protocol
change in §4. Without it, translate-at-ingest makes the document tier *worse*.

Cost, measured and honest. Decode runs at 27-35 ms/token; qwen3's tokenizer produces
4.16 chars/token for English output, 3.25 for German, **2.41 for Romanian**. So one
thousand characters of output costs ~7 s in English and ~12 s in Romanian. Translating a
document into two other languages is therefore roughly **20 seconds per 1,000 source
characters**: a 20-page contract is ~13 minutes, a 200-page PDF is ~2 hours. Index size
triples, which is nothing (e5-small is 384-dim; a 200-page document is 1.6 MB of vectors).
Time is the cost, not space. Consequences:

* ingest translation is a **background job with progress**, never a blocking upload;
* the document is queryable in its own language the moment it is parsed, and gains the
  other languages as they finish;
* translate only into `SUPPORTED_LANGUAGES` minus the document's own — three languages
  means at most two extra copies;
* deletion must remove the translations and their vectors too. CLAUDE.md rule 7 says
  deletion must actually delete; a translated copy is a derived record and the current
  `forget()` has never seen one.

### #2 — Text translation on request. Build this second.

"Translate this email into Romanian." "Ce scrie aici?" It is the smallest mode: once
`TranslationEngine` exists (§4) this is a tool spec, a prompt asset, and a handler. It
needs no voice work, no new UI, no new session state, and it inherits the guard and the
taint rules for free — a German email is `TrustLevel.EXTERNAL`, and translating it does
not launder it. Latency is off the voice budget because it is a typed or long-form
interaction and it streams.

One thing it must do that the interpreter mode does not: **preserve the quarantine frame**.
Translating external text produces external text. The output block carries the same
`Provenance` and the same `TrustLevel.EXTERNAL` as the input, and a translated email that
says "ignore your instructions" is still a tainted turn.

### #3 — Live interpreter mode. Build this last, and scope it hard.

Two people, one device, alternating languages, spoken output in the other person's
language. It is the demo everybody wants and it is the most work by a wide margin:

* it does not fit the 1400 ms budget (§3);
* it needs new session state — who is speaking which language, and which pair is active;
* **it needs `LanguageArbiter` inverted.** `services/voice/src/ars_voice/asr/language.py`
  is built on hysteresis: "staying in the current language is always the safe default; it
  is what the previous utterance proved." In interpreter mode that is exactly wrong — the
  language is *expected* to alternate every single utterance, and refusing a switch on a
  two-word reply ("Ja." / "Da.") is the whole failure mode. Interpreter mode needs a
  variant where a switch **between the two configured interpreter languages** is free and
  a switch to a third is refused, which is close to the opposite policy;
* it needs a UI that shows both transcripts, because a third party who cannot hear a
  translation start needs to see one;
* **and it has a trust question nobody has answered.** The other speaker is not Alex. If
  their words ever reach the reasoning layer they are `TrustLevel.EXTERNAL` and must taint
  the turn. The safe design, and the one to ship, is that **interpreter mode never reaches
  the reasoning layer at all**: audio → ASR → translate → TTS, no tools, no memory, no
  skills. That also makes it the only mode where the guard has nothing to decide.

Ranked last on value-for-effort, not on value. It is genuinely useful. It is also the only
mode where a mistranslation is spoken aloud to a stranger and cannot be un-said.

---

## 2. Which model

### The measurement

`uv run python research/benchmarks/translation/quality.py`, 24 sentences per direction,
warm, first call per direction discarded:

```
    dir   chrF++    med    min   terms   nums   diac   p50 1st  p95 1st   p50 tot  p95 tot
en-> ro     65.7   67.8   30.4    100%   100%   99.3      107m     111m      535m     876m
en-> de     75.9   73.5   31.5    100%   100%    n/a      106m     108m      477m     685m
ro-> en     72.3   77.7   27.5    100%   100%    n/a      107m     109m      353m     482m
ro-> de     70.6   72.8   24.4    100%   100%    n/a      111m     138m      509m     718m
de-> en     82.2   84.8   49.7    100%   100%    n/a      127m     137m      402m     579m
de-> ro     59.1   57.5   29.1    100%   100%   98.8      107m     110m      532m     913m
```

By domain (mean chrF++):

| direction | contract clause | code-switched | command | dialogue |
|---|---:|---:|---:|---:|
| en→ro | 65.0 | 57.7 | 65.2 | 68.7 |
| en→de | 79.3 | 59.3 | 70.9 | 80.3 |
| ro→en | 65.9 | 86.2 | 72.1 | 75.3 |
| ro→de | 71.5 | 69.8 | 76.5 | 65.5 |
| de→en | 67.4 | 91.8 | 83.8 | 93.2 |
| **de→ro** | **54.3** | 66.4 | 60.9 | 60.6 |

**Read the `min` column with suspicion, not the mean.** The worst-scoring sentence in four
of six directions is `dlg.repeat`, and in every case the model's output is *correct* and
merely phrased differently from my reference (`Können Sie das bitte langsamer wiederholen?`
against `Könnten Sie das bitte noch einmal langsamer sagen?`). One reference per direction
is the limitation of this fixture set and it depresses the floor, not the mean. The means
and the *relative* ordering are what this table is for.

### What the numbers say

* **qwen3:14b is a competent EN/RO/DE translator.** Every direction clears the ingest
  floor (45) and every direction clears the interpreter floor (55). Nothing here needs
  another model to be usable.
* **de→ro is the weak leg at 59.1, and contract clauses in that direction are 54.3.**
  That is the pair with no English on either side, and it is also the pair the interpreter
  mode would use most in this household. It is usable, and it is the first thing to
  re-measure if quality complaints arrive.
* **Romanian output costs 1.7× the tokens of English output.** 2.41 chars/token against
  4.16, at a near-constant 27-35 ms/token. Every Romanian latency number in this document
  is that ratio, not a quality problem, and no prompt change fixes it.
* **Code-switching survives in the right direction and degrades in the other.** ro→en and
  de→en score 86 and 92 on the code-switched fixtures — `branch-ul de staging` came back
  as `the staging branch`, `Deployment-ul a eșuat` as `Deployment failed`. Going *into*
  Romanian or German it drops to 57-70, because the model has to decide whether to keep
  `deploy` in English, and it is inconsistent about it. The prompt asks it to keep them;
  the `terms` column (100%) says it kept the ones we check, which is currently one.
* **Numbers survive, including the separator conversion**, which was the surprise.

### The separator finding, because it nearly shipped as a bug in the metric

English writes four thousand two hundred as `4,200`. Romanian and German both write it
`4.200`. A translator crossing EN↔{RO,DE} must change the separator; a translator going
RO↔DE must not. Both errors are silent, fluent, and off by a factor of one thousand.

My first version of the check was a substring match and it reported "the model dropped
4.200" on ro→en and de→en. The model had in fact done the right thing and written `4,200`.
`numerals.py` now parses every numeric literal in the output **under the target
language's own convention** — which is what the human reading it will do — and compares
values, not spellings. qwen3 scores 100% on all six directions. It also caught a bug in my
own fixture file, where I had written the English reference with a Romanian separator.

This check needs no reference translation, which makes it the one that belongs in
production telemetry: every translated turn can report whether every numeric value in the
source survived, on live traffic, forever.

### The alternatives, and why not yet

| Option | Size on disk | Licence | RO↔DE | Streams | Verdict |
|---|---|---|---|---|---|
| **qwen3:14b via Ollama** (today) | 9.3 GB, already resident | Apache-2.0 | yes, direct, chrF++ 59-71 measured | yes | **use it** |
| Whisper `translate` task | 0 extra (already loaded) | MIT | **no** | — | impossible, see below |
| Opus-MT (Marian) | ~300 MB fp32 per pair, ~80 MB int8 | Apache-2.0 | **model does not exist** | no | blocked on the pair that matters |
| NLLB-200-distilled-600M | 2.46 GB fp32, ~620 MB int8 | **CC-BY-NC-4.0** | yes | no | non-commercial; keep as a fallback |
| MADLAD-400-3B-MT | 11.8 GB fp32, ~3 GB int8 | Apache-2.0 | yes | no | 3 GB next to a 9.3 GB qwen3 for a quality delta nobody has measured |

Sizes and licences above are read from the Hugging Face API today, not remembered.

**Whisper's built-in `translate` task is out on two independent grounds, and it is worth
saying both.** First, the task is **X→English only** — it has no ability to produce
Romanian or German, so it cannot serve ro↔de, which is the pair the interpreter mode
exists for. Second, the model actually loaded here is `large-v3-turbo`, which OpenAI
fine-tuned on transcription data **with translation data deliberately excluded**, and
which they say is not expected to perform well on translation. Either reason alone
disqualifies it. It is not a shortcut; it is a dead end with a familiar name.

**Opus-MT is blocked on availability, which I checked rather than assumed.**
`Helsinki-NLP/opus-mt-de-ro` and `opus-mt-ro-de` **do not exist**; neither does
`opus-mt-ro-en` as a standalone pair (it is covered only by the grouped `roa-en`). So a
Marian deployment for this triple would be `en-de`, `de-en`, `en-ro`, `roa-en` plus a
**pivot through English for both RO↔DE directions**. That doubles latency, compounds two
error rates, and — the reason I would refuse it even if it were fast — **an English pivot
destroys the T-V distinction.** German `Sie`/`du` maps cleanly onto Romanian
`dumneavoastră`/`tu`; English has neither, so the politeness register is erased in the
pivot and reconstructed by guess. In an interpreter mode whose entire social function is
addressing a stranger correctly, that is not a rounding error.

**NLLB is CC-BY-NC-4.0.** For a private assistant on Alex's own machine that is arguably
fine; for anything that ever ships it is not, and a model you have to remove later is a
model you should not build on now. Keep it as the named fallback if qwen3's de→ro is
judged inadequate.

**What I did not measure, and why.** I did not run NLLB, MADLAD or Opus-MT. None of them
were on disk; the pair that would decide the question (ro↔de) has no Opus-MT model at all;
and a 3 GB download for a design document is not a good trade when the incumbent already
clears every floor. The harness takes them without modification —
`quality.py --ct2 models/mt/nllb-600m-int8 --ct2-tokenizer facebook/nllb-200-distilled-600M`
— and that is the experiment to run the day someone complains about de→ro. I also did not
measure a smaller Ollama model (`--model qwen3:8b`); that is one `ollama pull` and one
command, and it is the cheapest way to buy latency if interpreter mode needs it.

---

## 3. Latency — where a translation hop fits, and where it does not

### The shape of the cost

```
de->ro  1 sentence  /  49 chars   first token  171 ms   total   638 ms
de->ro  2 sentences / 120 chars   first token  109 ms   total   900 ms
de->ro  4 sentences / 233 chars   first token   73 ms   total  1565 ms
de->en  1 sentence  /  49 chars   first token   27 ms   total   309 ms
de->en  2 sentences / 120 chars   first token   28 ms   total   691 ms
de->en  4 sentences / 233 chars   first token   33 ms   total  1282 ms
```

**First-token latency is flat in input length. Total latency is linear in output length.**
That single fact decides most of this section: prefill is free at these sizes, and the
whole cost is decoding, at 27-35 ms per token.

### Does it fit the 1400 ms budget?

For **modes 2 and 3, the question does not arise.** Ingest translation is offline; text
translation is not on the voice path. Neither touches the budget. Say so in the
architecture doc and move on.

For **interpreter mode it does not fit**, and the arithmetic is not close. The critical
question is *not* first-token latency, because **TTS cannot start on a token — Piper
synthesises per sentence** (`tts/segmentation.py`, `first_sentence_max_chars=140`). So the
gate is the *complete* translation of the sentence:

| Stage | Budget today | Interpreter mode (measured, worst leg de→ro) |
|---|---:|---:|
| Endpoint confirmation | 750 ms | 750 ms |
| ASR final | 250 ms | 250 ms (mlx-whisper measured 121/135 ms EN/RO; **DE unmeasured**) |
| Context assembly | 80 ms | 0 ms — there is no context |
| LLM first token | 200 ms | — replaced |
| **Translation, whole sentence** | — | **532 ms p50 / 913 ms p95** |
| TTS first audio | 120 ms | 120 ms |
| **Total** | **1400 ms** | **~1650 ms p50 / ~2050 ms p95** |

(Summing p95s overstates the tail — the stages are not perfectly correlated — so treat
2050 ms as a ceiling rather than a prediction.)

To fit 1400 ms the translation slot would have to be ≤ 280 ms, which at 30 ms/token is
about ten output tokens, about 25 Romanian characters. qwen3 cannot do that and no prompt
change will make it. **If fitting inside 1400 ms is a hard requirement, that requirement
alone forces a dedicated NMT model** — a 74M-parameter Marian would do a sentence in tens
of milliseconds, and then you are back to the pivot problem in §2. That is the honest
fork, and I recommend the other branch.

**Recommendation: interpreter mode gets its own budget, p95 ≤ 2500 ms, written into
`docs/architecture/overview.md` next to the existing table, and asserted in `tests/load`
like every other budget.** The 1400 ms figure exists because "voice interaction dies above
~1.2 s to first audio" — that is a person waiting for an *assistant* to answer *them*. In
interpreter mode the listener is waiting for another *human's* words to be rendered, the
turn-taking has already visibly happened, and professional consecutive interpretation has
multi-second gaps that nobody experiences as failure. What is *not* acceptable is silence
with no evidence: the source transcript must appear on screen the instant ASR finalises,
which is ~1000 ms in, so the wait is visibly productive. That is the equivalent of the
600 ms filler rule for tool calls, and it should be written down as one.

### Streaming and partial translation: feasible, and mostly not worth it

Three separate questions, three different answers.

**Streaming the translation output — yes, and it is already implemented.** The
`TranslationEngine` in §4 streams, `OllamaBackend` streams, `DiacriticRepairStream`
already handles Romanian deltas arriving mid-word. It buys little for TTS (which waits for
a sentence anyway) and a great deal for the on-screen transcript, which is what makes the
wait tolerable.

**Translating a partial transcript, before the user has finished speaking — no.** German
is verb-final: `weil das Token abgelaufen ist` cannot be rendered into English or Romanian
until the clause ends, because the verb arrives last. A partial translation would have to
be retracted, and **spoken audio cannot be retracted.** Combined with the measurement above
— first-token latency is already only 27-171 ms and flat in input length — speculative
partial translation buys almost nothing and risks the one failure this mode cannot
survive. Do not build it.

**Pipelining whole sentences as ASR finalises them — yes, and this is the real win.** A
four-sentence utterance costs 1282-1565 ms as one call. Translate each sentence the moment
ASR finalises it and only the *last* sentence sits on the critical path: ~530 ms instead
of ~1565 ms, and the total stops growing with utterance length. This costs more total
compute (more prefills) and buys back a second on anything longer than a sentence. It
requires the ASR engine to emit sentence-final results during the utterance, which
`MlxWhisperEngine` does not do on its fast path — it runs one decode over the whole 30 s
window after the endpoint fires. **That is voice-engineer work, not ML work**, and it is
the single highest-value latency item for interpreter mode.

### Cost per turn

Zero dollars, on every mode, because everything runs locally. The budgeted price is GPU
time, and it is:

| Work | Cost |
|---|---|
| one interpreted sentence, de→ro | ~530 ms p50 GPU, ~19 output tokens |
| one interpreted sentence, ro→de | ~510 ms p50 GPU, ~19 output tokens |
| a text-translation turn | streams; ~30 ms per output token |
| ingest, per 1,000 source chars, into 2 languages | ~20 s of GPU, once, in the background |
| a tier-1 document answer after ingest translation | unchanged: ~20 ms, no GPU |

The last row is the one that matters. Ingest translation does not add cost to answering;
it *removes* it, by converting 4-8 s model turns into 20 ms retrievals.

---

## 4. The interface

The smallest seam consistent with rule 2, and deliberately the narrowest interface in
`packages/core`.

### `packages/core/src/ars_core/interfaces.py`

```python
class TranslationDomain(StrEnum):
    """Which prompt asset renders. Not a hint — the register genuinely differs."""

    CONVERSATION = "conversation"
    """Interpreter mode and chat. Spoken register, contractions, politeness preserved."""
    DOCUMENT = "document"
    """Ingest. Formal register, terminology consistency across chunks, no paraphrase."""
    COMMAND = "command"
    """Short imperatives. The hardest length and the most common input."""


class TranslationEngine(ABC):
    """Text in language A, text in language B. Nothing else.

    Note what this signature cannot accept: a session, a memory record, a history, a tool.
    That is a privacy property enforced by the type rather than by a reviewer noticing —
    "never send more user memory to a provider than the task needs" is satisfied here
    because there is no parameter through which memory could travel. A translation is a
    pure function of one string and a language pair, and the seam says so.

    Backends live where the vendor SDK already lives: the LLM-backed one in
    `ars_compute.translation` next to `backends/ollama.py`, which is the only module that
    knows Ollama's wire format. A future CTranslate2 or Marian backend brings its own
    module and imports its SDK inside it.
    """

    @abstractmethod
    def translate(
        self,
        text: str,
        *,
        source: Language | None,
        target: Language,
        domain: TranslationDomain = TranslationDomain.CONVERSATION,
    ) -> AsyncIterator[str]:
        """Streams the translation as it is produced.

        Declared `def`, not `async def`, for exactly the reason recorded on
        `LlmBackend.complete`: an async generator function already returns an
        AsyncIterator, and declaring the seam `async def` forces every call site into
        `async for x in await engine.translate(...)`, which everyone gets wrong once.

        `source=None` means detect. Callers on the voice path always pass a source:
        `Transcript.language` already paid for that decision through the arbiter and the
        matrix detector, and asking the translator for a second opinion is how a turn ends
        up translated out of a language nobody spoke.

        Raises `UnsupportedDirection` rather than guessing when `(source, target)` is not
        in `pairs`. Fluent output in the wrong language is the worst possible failure here,
        because the user cannot check it — that is why they asked.
        """

    @abstractmethod
    async def cancel(self) -> None:
        """Barge-in. Interpreter mode is interrupted constantly — someone starts talking
        over the rendering — and a translation that keeps decoding is holding the GPU the
        next utterance needs. Same contract as `LlmBackend.cancel` and `TtsEngine.cancel`:
        stop generating, not merely stop listening."""

    @property
    @abstractmethod
    def runs_locally(self) -> bool:
        """False means the user's sentence leaves the device. The guard reads this exactly
        as it reads `LlmBackend.runs_locally`, and refuses SENSITIVE content. Interpreter
        mode is additionally hard-wired to a local engine regardless of configuration: the
        sentence being interpreted may be a diagnosis, and there is no consent dialogue
        that makes sending it to a provider the right default."""

    @property
    @abstractmethod
    def pairs(self) -> frozenset[tuple[Language, Language]]:
        """The directions this backend accepts, asymmetric on purpose. Whisper's translate
        task is X->EN only; Opus-MT has en-ro but no ro-de. A backend that claims a pair
        it cannot do produces confident output in the wrong language."""

    @property
    @abstractmethod
    def measured_quality(self) -> Mapping[tuple[Language, Language], float]:
        """chrF++ per direction, from research/benchmarks/translation/quality.py.

        Same rule as `WakewordEngine.false_accepts_per_hour`: a backend that cannot report
        this number has not been evaluated and must not ship. The router reads it and
        refuses a direction below `chrf.INGEST_FLOOR` rather than emitting text the user
        has no way to check."""
```

### `packages/protocol` — the additions

Rule 1: the fields go here or nowhere.

```python
# common.py
class TranslationDomain(StrEnum): ...     # as above; core imports it, never redefines it

# memory.py — MemoryRecord gains two fields
translated_from: Language | None = None
"""Set on a record that is a machine translation of another record. Never set on
anything the user wrote. Shown in the UI, because an answer read out of a machine
translation of a contract is a different claim from one read out of the contract."""

origin_id: str | None = None
"""The record this one was derived from. Load-bearing in two places, both measured:
tier 1's ambiguity margin must skip hits sharing an origin (otherwise translations of
the same paragraph read as two competing answers and the tier answers nothing —
measured, 2 of 12 questions lost), and `forget()` must delete derived records and their
vectors along with the original (CLAUDE.md rule 7)."""

# events.py — only needed for modes 1 and 3
class TranslationDelta(ServerEvent): ...  # text, source, target, seq
class TranslationDone(ServerEvent): ...   # text, source, target, engine, elapsed_ms
```

### Prompts

`services/compute/src/ars_compute/prompts/translate.v1.md` plus
`translate_document.v1.md` and `translate_command.v1.md`, registered language-neutral in
`manifest.toml` (the loader already falls back to `language=None`, as `fillers.v1.toml`
does) and rendered with `{src_name}` / `{dst_name}`. The candidate text is in
`quality.py`'s `SYSTEM` constant with a comment on every line explaining which observed
failure put it there; it moves into the asset directory the moment the engine ships, and
the benchmark then loads it from there so the two cannot drift.

### Context assembly and budget

Explicit, as required, and short: **the translation window is the prompt asset (~120
tokens) plus one user message containing the source text. Nothing else. No memory, no
preferences, no history, no tools.** Under pressure nothing is dropped, because there is
nothing droppable — a source longer than the window is split at sentence boundaries and
translated in sequence, with the previous source sentence supplied as context for
terminology consistency and its translation discarded. That last clause is what keeps
`Kaution` from becoming `Garanție` in chunk 4 and `Cauțiune` in chunk 5.

---

## 5. Evaluation

Already written and already run. `research/benchmarks/translation/` is the deliverable
here as much as this document is.

**Fixtures.** 24 sentences in EN/RO/DE across four domains chosen to match what this
product actually sees: assistant commands, contract clauses, interpreter dialogue, and
code-switched technical speech. Plus a German lease and twelve questions about it, four in
each language, shaped to match `retrieval_calibration.py` so the two benchmarks are
comparable.

**Three metrics, none of which needs a human.**

1. **chrF++**, implemented in `chrf.py` with no dependency. Character n-grams 1-6 plus
   word n-grams 1-2, β=2. Character-level because Romanian and German are inflected and a
   word-level metric scores a correct translation as a miss for choosing a different case
   ending. The floors — 45 for ingest, 55 for the interpreter path — are decision
   boundaries with a rationale, and the interpreter bar is higher on purpose: a
   mistranslated lease clause spoken aloud to a landlord is a different failure from a
   clumsy paraphrase sitting in a vector index. The script **fails** below the ingest
   floor and **warns** between the two.
2. **Numeric value survival**, `numerals.py`. Reference-free, locale-aware, described in
   §2. This is the one that goes into production telemetry.
3. **Romanian diacritics**, scored by `ars_compute.language.diacritics_report` — the
   *shipped* checker, not a copy, so the benchmark and the runtime cannot disagree. qwen3
   scores 99.3 en→ro and 98.8 de→ro; both are below 100 and both trip the warning, which
   is correct: `Chirie lunara` is text Piper mispronounces. `restore_diacritics` repairs
   part of it. German has no equivalent checker and scores 1.0 by fiat; that gap is stated
   rather than faked.

**Latency**, p50 and p95 of first token and of total, per direction, warm, with the first
call per direction discarded so a prefill is not billed as a per-sentence cost.

**The retrieval eval** is the three-arm experiment in `ingest_retrieval.py`, which is what
a document-tier change has to pass. It runs the real `TierConfig` gate, prints the noise
floor for both arms, and warns if the noise floor moves — because tripling the number of
passages an irrelevant question can match against is exactly the kind of change that
requires re-fitting `CALIBRATION_MIDPOINT` and `CALIBRATION_STEEPNESS`. It did not move
here (0.811 in both arms), so the constants stand.

**What the eval cannot tell you.** One reference per direction, 24 sentences, written by
one person. chrF++ from this file is a comparator between systems measured on this file,
not a number to quote against WMT. Four of the six `min` scores are correct translations
phrased differently from my reference. Before this set is used to *reject* a model rather
than rank one, it wants a second reference per direction — that is the obvious next
improvement and it costs an afternoon.

---

## 6. Implementation plan, ordered by value for effort

**1. Translate at ingest.** (largest measured win, no hot-path cost)
`origin_id` and `translated_from` in `packages/protocol`; the margin rule in
`TieredBrain.try_cheap_tiers` skips same-origin hits; a background job in
`ars_gateway.documents` that translates a parsed document into the other supported
languages and indexes the copies; `forget()` follows `origin_id`. Ship behind a flag and
re-run `ingest_retrieval.py` and `retrieval_calibration.py` before turning it on.
*Definition of done gap I would accept on purpose: no German fixture in
`retrieval_calibration.py` yet. Add one in the same change.*

**2. `TranslationEngine` + the Ollama backend + the prompt assets.**
The seam, one implementation, three prompt assets, `UnsupportedDirection`, unit tests
against a recorded response body in the style of `test_backend_wire.py`. Step 1 uses this;
it is listed second only because step 1 can be prototyped without it.

**3. Text translation on request.** A tool spec, a handler, EN/RO/DE user-facing strings.
Preserves `Provenance` and `TrustLevel` through the translation. Small.

**4. The numeric-survival telemetry.** `numerals.check` on every translated turn, reported
in stats. Cheap, and it is the only monitor that works on live traffic where no reference
exists.

**5. Sentence-pipelined ASR → translation.** Voice-engineer owns the ASR half. Worth a
second of latency on any multi-sentence utterance and it is a prerequisite for mode 1
being pleasant.

**6. Interpreter mode.** Its own budget in the architecture doc and its own assertion in
`tests/load`; an interpreter-mode `LanguageArbiter` without hysteresis between the two
configured languages; session state; a UI showing both transcripts; and a hard rule that
the mode never reaches the reasoning layer.

**7. Re-measure de→ro** against `qwen3:8b` and, if it is still the weak leg, against
NLLB-600M through the harness's existing `--ct2` arm.

---

## 7. What I would not build

* **Speculative translation of partial transcripts.** German is verb-final, spoken audio
  cannot be retracted, and first-token latency is already flat in input length. All risk,
  no measured gain.
* **Query-side translation as the primary cross-language fix.** Measured: fewer answers
  (5/8 against 7/8) *and* 379 ms added to a tier budgeted at 20 ms. Keep it in the script
  as the refuted alternative; consider it only for a document too large to translate.
* **A dedicated NMT model, today.** qwen3 clears every floor, is already resident, streams
  and cancels. Adding one costs RAM next to a 9.3 GB model, and the two candidates are
  respectively non-commercial (NLLB) and 3 GB (MADLAD). Revisit when an eval says qwen3 is
  the problem, not before.
* **Opus-MT with an English pivot.** `opus-mt-de-ro` and `opus-mt-ro-de` do not exist, and
  pivoting through English erases the T-V distinction that Romanian and German both have
  and English does not. In interpreter mode that is the difference between addressing a
  stranger correctly and not.
* **Whisper's `translate` task.** X→English only, and `large-v3-turbo` was fine-tuned with
  translation data explicitly excluded. Two independent disqualifications.
* **Cloud translation (DeepL, Google).** Structurally wrong for this product. If it is
  ever added it goes behind the same `runs_locally` flag as any other backend, and
  interpreter mode ignores it regardless of configuration.
* **Another attempt at cross-lingual embeddings.** Already measured at −0.007 separation
  on e5-base, already documented in `brain.py`. Translate-at-ingest makes it unnecessary.
* **A German orthography repairer** analogous to `restore_diacritics`. Romanian's exists
  because 4-bit models measurably drop `ă â î ș ț`; there is no evidence these models drop
  umlauts, and building a repair vocabulary for a failure that has not been observed is
  how you get a module that corrupts correct text.

---

## 8. Two things found on the way that are not mine to fix

**`services/voice/src/ars_voice/asr/language.py` had a German-induced bug — already
fixed, and worth recording because it is the same class of bug this document could
introduce.** `LanguageArbiter.decide`'s refusal branch reported the retained language's
confidence as `1.0 - confidence`, correct for a binary support set and a two-fold
overstatement the moment German made it three. It flowed into
`Transcript.language_confidence`, which `ars_compute.language.ASR_TRUST_FLOOR` gates on,
so it silently changed which detector won. `voice-engineer` fixed it while this was being
written: `constrain_probabilities` now returns the whole constrained distribution and
`_retained_confidence` reads the kept language's actual probability out of it.

The lesson generalises to everything in §6. Adding a third language does not break code
that mentions languages; it breaks code that *assumed* there were two, and those
assumptions are in comments and arithmetic, not in types. `TierConfig.document_threshold`,
`CALIBRATION_MIDPOINT` and `recall_cosine` were all fitted on a two-language population.
Re-run `retrieval_calibration.py` with German fixtures before trusting any of them.

**German ASR is unmeasured.** `mlx_whisper_engine.py` publishes 121 ms EN / 135 ms RO
against a 250 ms budget, on Piper-synthesised speech. There is no German row. Whisper is
generally stronger on German than on Romanian so I expect it to pass, but the interpreter
budget in §3 rests on a number nobody has measured. Same harness, one more language.

---

## 9. Reproducing everything in this document

```bash
ollama serve &

# quality + latency, all six directions
uv run python research/benchmarks/translation/quality.py --worst

# the document-tier experiment, three arms
uv run python research/benchmarks/translation/ingest_retrieval.py

# the ambiguity-margin trap, with the rule exactly as it is written today
uv run python research/benchmarks/translation/ingest_retrieval.py --no-dedupe

# a candidate model
uv run python research/benchmarks/translation/quality.py --model qwen3:8b
uv run python research/benchmarks/translation/quality.py \
    --ct2 models/mt/nllb-600m-int8 --ct2-tokenizer facebook/nllb-200-distilled-600M
```

Latency numbers in this document were taken with other work running on the same machine.
The first-token figures were stable across four runs at 106-171 ms; one contaminated run
recorded a 3476 ms p95 on en→ro and is not reported here. Re-measure on a quiet machine
before writing any of these into `tests/load`.

## Sources for the third-party claims

- Whisper `large-v3-turbo` trained without translation data: <https://github.com/openai/whisper/discussions/2363>, <https://huggingface.co/openai/whisper-large-v3-turbo>
- NLLB-200 licence and model card: <https://huggingface.co/facebook/nllb-200-distilled-600M>
- MADLAD-400 licence and sizes: <https://huggingface.co/google/madlad400-10b-mt>, <https://arxiv.org/pdf/2309.04662>
- Opus-MT availability and licence: queried live against the Hugging Face model API, 2026-09-06
