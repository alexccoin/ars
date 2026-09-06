# services/compute — the reasoning layer

Assembles the prompt, streams from a model, routes tool calls through the guard, feeds
results back with their provenance intact, and learns how the user wants A.R.S to behave
by asking rather than deciding.

```
ars_compute/
  context.py      provenance-preserving assembly, quarantine framing, taint, budget
  language.py     per-utterance reply language, code-switching, Romanian diacritics
  sensitivity.py  what may not leave the device  (seam owned by security-engineer)
  tokens.py       token estimate + price table
  toolschema.py   ToolSpec -> provider tool schema, via a versioned type map
  turn.py         the orchestrator
  learning.py     behaviour observations
  backends/       ollama.py · anthropic.py · scripted.py · router.py · base.py
  prompts/        every prompt A.R.S sends, as versioned files
```

## Model choice, with numbers

Measured 2026-09-06 on this checkout. Prices from platform.claude.com, verified the same
day. Quality columns are **not filled in** for the two real backends: Ollama is not
installed on this machine and no eval has been run against a live model, so writing a
number there would be a guess. `research/benchmarks/compute/run.py --backend ollama` on a
machine that has Ollama is the first thing to do.

| Backend | Model | Local | Ctx | Cost / turn (1.2k in, 120 out) | Cost / turn (2.6k in) | First token p50 / p95 | Eval |
|---|---|:-:|--:|--:|--:|--:|--:|
| `OllamaBackend` | `qwen3:14b` | ✅ | 8k | **$0.000000** | **$0.000000** | **111 / 118 ms** EN · **142 / 154 ms** RO | not yet measured |
| `AnthropicBackend` | `claude-sonnet-5` | ❌ | 1M | $0.003600 | $0.006400 | not yet measured | not yet measured |
| `AnthropicBackend` | `claude-haiku-4-5` | ❌ | 200k | $0.001800 | $0.003200 | not yet measured | not yet measured |
| `AnthropicBackend` | `claude-opus-5` | ❌ | 1M | $0.009000 | $0.016000 | not yet measured | not yet measured |
| `ScriptedBackend` | — | ✅ | — | $0 | $0 | ~0 ms | deterministic |

Ollama numbers: M5 Max, `qwen3:14b` Q4_K_M, real `/api/chat`, warm weights and warm
system-prompt prefix, `think: false`, 8 prompts per language. Both meet the 200 ms
`llm_first_token` budget. Quality columns for the cloud models are still empty because no
eval has been run against them; writing a number there would be a guess.

Decisions already made, and why:

* **Default is `qwen3:14b` on Ollama.** Not because it is the best model, but because the
  reference deployment is "laptop standalone, no network needed" and the cost column is
  the entire argument. Cloud is opt-in per capability.
* **`claude-sonnet-5`, not `claude-opus-5`, for the cloud slot.** Opus is 2.5× the price
  for a voice turn that is usually a calendar lookup. Escalating to Opus is a policy
  change in `LlmConfig`, not a code change.
* **`output_config.effort: "low"` on the Anthropic path.** The API default is `high`;
  the effort docs recommend `low` for "latency-sensitive workloads... where faster
  turnaround is prioritized", and the budget here is 200 ms to first token. Raising it
  needs an eval showing the quality difference is worth the latency.
* **`num_ctx = 8192` locally, not 32k.** KV cache growth dominates first-token latency on
  an M-series laptop. A bigger window is a latency decision with a number attached.
* **Thinking is off for spoken turns.** See the next section. This is the single largest
  latency lever in the service: 111 ms against 4221 ms.
* **Neither backend uses a vendor SDK.** Both speak the documented HTTP API through
  `httpx`. Streaming and cancellation are properties of the transport, and barge-in has to
  close the socket rather than trust an SDK's context manager to get there.

## Reasoning: how much the model may think

`qwen3:14b` is a reasoning model. Left to itself it emits a thinking block before any
visible content, so the first token the *user* hears arrives after the whole reasoning pass
has finished. Measured on an M5 Max against a real server, warm, 8 prompts per language:

| `think` | EN first token p50 / p95 | RO first token p50 / p95 | total p50 | output tokens |
|---|--:|--:|--:|--:|
| **`false`** (what we send) | **111 / 118 ms** | **142 / 154 ms** | 579 / 729 ms | 20 / 23 |
| `true` | 4221 / 7389 ms | 5022 / 6791 ms | 4724 / 6067 ms | 187 / 201 |
| omitted entirely | — | ~3900 ms | — | — |

The budget for `llm_first_token` is 200 ms. **Omitting the field is not a safe default** —
for a reasoning model it means "think as much as you like". The safe default is an explicit
`false`, and that is what `ReasoningMode.OFF` sends.

Two further findings, both in `reasoning.py` with the numbers:

* **The intermediate levels are useless on this model.** `think: "low"` produced the same
  ~600 characters of reasoning and the same ~2.8 s delay as `think: true`. For qwen3:14b it
  is 111 ms or several seconds, with nothing in between. The vocabulary still expresses
  `low`/`medium`/`high`/`max` because other models honour them and because the Anthropic
  backend maps them onto `output_config.effort`.
* **Thinking tokens are spent from `num_predict`.** With `think: true` and
  `num_predict: 120`, the reasoning block consumed the whole allowance and the reply came
  back **empty** (`done_reason: "length"`, 602 characters of thinking, 0 of content).
  `OllamaBackend` therefore carries a separate, larger allowance for reasoning turns.

Quality was unaffected on ordinary assistant turns: both settings produced correct,
idiomatic Romanian with proper diacritics, and `test_ollama_live.py` asserts that.

### The policy

`ThinkPolicy` lives in `backends/router.py`, next to `RoutingPolicy`, because it is the
same kind of decision made from the same information: buy quality with latency, or latency
with quality. It is decided **per turn**, not configured per process — the same model in
the same session must think for a typed question and not for a spoken one.

| Turn | Mode | Why |
|---|---|---|
| Spoken, any round | `OFF` | The user is sitting in silence. 200 ms budget, ~4 s cost. |
| Typed | `ON` | No first-audio budget. A screen can show "thinking" honestly. |
| Typed + escalated | `ON` | The router already judged it worth spending on. |
| Spoken, round ≥ 1 after a filler | `spoken_after_filler`, default `OFF` | Available, off. The user is still waiting for an answer after the tool returns, and 4 s of silence there is 4 s of silence. |

`spoken_after_filler` is the *only* setting by which a spoken turn can ever think.
Escalation deliberately has no say on the spoken path: it means the question is hard, not
that the user stopped waiting for a voice to start. One line to read when asking "can this
path be slow".

Reproduce all of it, including the cold-start cliff:

```bash
uv run python research/benchmarks/compute/latency_live.py            # 111 / 116 ms, PASS
uv run python research/benchmarks/compute/latency_live.py --think    # the slow arm
uv run python research/benchmarks/compute/latency_live.py --no-warm  # p95 1162 ms, OVER
```

`OllamaBackend` only sends `think` to models that take it. Verified against a live server:
Ollama **ignores unknown top-level fields** (an unknown key returns 200) but validates
`think` strictly (a bad value returns 400 naming the valid set). Since the behaviour for a
model without the `thinking` capability is undocumented, the backend is optimistic and
self-correcting: it sends the field, and on a 4xx that mentions `think` it records that the
model does not support it, drops the field and retries once. Calling `capabilities()` at
startup — which reads `/api/show` — resolves it up front and avoids even that one retry.

### Warm both languages at startup

A.R.S has one system prompt per language, so it has one KV-cache prefix per language, so it
has one cold start per language. Measured: with weights already loaded, the first Romanian
turn of a process still cost **1158 ms** to first token while every later one cost ~30 ms.
Alternating EN/RO afterwards is free (116–139 ms either way), so it is a one-off per prefix,
not a per-switch cost.

`OllamaBackend.warm([...])` prefills them. It costs two one-token generations at startup and
removes a 1.1 s cliff from the first Romanian utterance after every restart — which is
exactly the moment a bilingual household forms its opinion of the assistant.

```python
await backend.warm([assembler.build_system(lang)[0] for lang in (Language.EN, Language.RO)])
```

## Context budget

`ContextBudget`, defaults: 8192-token window, 768 reserved for output, so **7424 tokens of
input**. Measured occupancy:

| Section | EN | RO | Notes |
|---|--:|--:|---|
| System prompt | 884 | 1184 | never dropped |
| `+ tool list` (2 tools) | ~170 | ~190 | grows with the skill catalogue |
| Quarantine frame, per external block | 368 | 485 | **fixed cost per block, never truncated** |
| 8 recalled memories | ~950 | ~1330 | |
| 6 history turns | ~950 | ~1180 | |
| The user's utterance | ~15 | ~20 | never dropped |

Romanian costs 25–35% more tokens than English for the same content, because the
diacritics are multi-byte and split. The Romanian system prompt alone is 16% of the
window. That is the single largest known inefficiency here; the obvious fix — a compact
frame for the second and subsequent external blocks — is **not** implemented, because it
trades a security property for tokens and there is no eval on a real model yet to show
what it costs in refusal rate.

Assembly cost, 200 iterations:

| Context | p50 | p95 |
|---|--:|--:|
| Bare (system + utterance) | 0.01 ms | 0.01 ms |
| Typical (8 memories, 6 history turns) | 0.19 ms | 0.21 ms |
| With one quarantined web page | 1.14 ms | 1.21 ms |

The architecture doc allocates 80 ms to "context assembly + memory recall" jointly, so
assembly uses ~1.5% of its slice and the rest belongs to retrieval.

## Drop order under pressure

`context.DROP_STEPS`, a constant, applied in order until the context fits.

1. **`HISTORY_OLDEST`** — earlier turns, oldest first. Most speculative thing in the
   window: the user is asking about now.
2. **`MEMORY_LOWEST_RANK`** — recalled memory, worst recall score first. The retriever
   guessed these were relevant.
3. **`EXTERNAL_TRUNCATE`** — shrink each quarantined block to `external_floor_tokens`
   (256), keeping head and tail and eliding the middle *inside* the fence. The frame is
   never truncated.
4. **`TOOL_RESULT_OLDEST`** — results of earlier tool calls in this turn, oldest first.
   The most recent result is never dropped here; it is usually why the model is being
   called again.
5. **`EXTERNAL_OLDEST`** — whole external blocks, oldest first, frame and body together.
6. **`PREFERENCES_OLDEST`** — confirmed behaviour rules, oldest confirmation first. Last,
   because they are small and the user explicitly asked for them.

Never dropped: the system prompt and the user's current utterance. If those alone do not
fit, `ContextOverflow` is raised and A.R.S asks the user to break it up — a silently
mutilated question is worse than an honest refusal.

**Atomicity invariant:** a quarantine frame's token cost is measured on the rendered
block, so there is no state in which untrusted body text is in the window and its frame is
not. Asserted by `test_a_dropped_external_block_takes_its_frame_with_it`.

## Trust and taint

* `tainted` is computed from provenance alone — `taints(TrustLevel.EXTERNAL)` — never from
  a keyword scan. A user who says "ignore your instructions" does not taint their own turn.
* EXTERNAL blocks render inside a fence whose nonce is `blake2s(turn_id:index)`, 64 bits:
  deterministic for tests, unguessable for the author of a scraped page.
* `neutralise()` strips Unicode bidi overrides and defangs the literal fence token. Nothing
  else is altered — the user may ask what the page actually said.
* `detect_injection_markers()` is **not a filter**. Quarantine applies regardless. It exists
  to flag a specific block as hostile inside its own frame, to give the orchestrator
  something to quote to the user, and to be a metric.
* Defence in depth in `turn.py::_backstop`: inside a tainted turn, a guard `ALLOW` for a
  CRITICAL capability is refused anyway, and a guard `ALLOW` for an
  `exfiltrates_outward` capability is downgraded to `ASK` with the literal resource quoted.

## Language

Reply in the language of the user's most recent utterance, per utterance. `Transcript.
language` is the primary signal; a matrix-language detector overrules it when the text
disagrees decisively. That override exists for one case: a Romanian sentence carrying
English technical nouns, which ASR routinely tags `en`. Romanian enclitics on foreign stems
(`branch-ul`, `commit-uri`) and diacritics are near-proof of a Romanian matrix; words in
`TECHNICAL_BORROWINGS` score zero in both directions.

Romanian output passes through `DiacriticRepairStream`, which repairs a closed vocabulary
of unambiguous ASCII-fied forms and refuses to guess at forms whose ASCII spelling is a
different real word (`sa`/`să`, `pana`/`până`). The residue is reported by
`diacritics_report` and tracked as `diacritics_clean` in the evals.

## Prompts

Every prompt is a file in `prompts/`, listed in `manifest.toml`, hashed at load time.
`AssembledContext.prompt_refs` carries refs like `system/v1/ro@a07cd0cf` so a turn's
telemetry names the exact bytes that produced it. Bump a version by adding a new file;
never edit a shipped one, because eval results are recorded against the version string.

## Running things

```bash
uv run pytest tests/unit/compute -q                      # no network needed
uv run python research/benchmarks/compute/run.py         # eval delta table

# Latency gate. Needs a real server; skips (loudly) without one.
ollama serve & ollama pull qwen3:14b
uv run pytest tests/unit/compute/test_ollama_live.py -v
```

`test_ollama_live.py` is the only test in this service that can catch a latency
regression. The `think` cliff — 16.9 s to first token against a 200 ms budget — passed the
entire scripted suite, because a scripted model has no opinion about how long a real one
takes to start talking.

It gates on the **best** of five samples, not the median or the max, because a gate and a
benchmark want different statistics. Contention on a shared GPU produces a few multi-second
outliers among good samples (observed `[161, 907, 4123, 177, 259]` with an unrelated job
running) and says nothing about this code; the regression moves *every* sample, because
thinking happens on every request. Verified both directions on real hardware:

| | samples (ms) | best | gate |
|---|---|--:|:--|
| fixed | `[116, 30, 31, 32, 31]` | 30 ms | PASS |
| `think` dropped | `[2668, 3066, 3271, 3067, 3145]` | 2668 ms | **FAIL** |

The published p50/p95 come from `latency_live.py` on an idle machine, which is where
percentile numbers belong. The suite also skips itself, loudly, if a bare one-token probe
shows the server is already saturated.

## Known gaps

* Cloud backends have never been run. All Anthropic numbers are prices and doc-derived
  wire shapes, not measurements.
* No quality eval against a real model. Every quality claim here is about A.R.S's own behaviour
  (routing, framing, taint, language selection), not about generation quality.
* `sensitivity.py` ships a regex baseline written by ml-engineer. The real rule set and
  the redaction-rather-than-refusal path belong to `security-engineer`;
  `SensitivityClassifier` is the seam.
* Token counts are a calibrated character-ratio estimate, not a tokenizer. Deliberately
  pessimistic. `TokenEstimator` is the seam for a real one.
* `docs/architecture/overview.md` is owned by `system-architect` and was not edited; it
  needs one line pointing here from the "Where things live" table.
