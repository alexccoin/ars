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

| Backend | Model | Local | Ctx | Cost / turn (1.2k in, 120 out) | Cost / turn (2.6k in) | First token | Eval |
|---|---|:-:|--:|--:|--:|--:|--:|
| `OllamaBackend` | `qwen3:14b` | ✅ | 8k | **$0.000000** | **$0.000000** | budget 200 ms | not yet measured |
| `AnthropicBackend` | `claude-sonnet-5` | ❌ | 1M | $0.003600 | $0.006400 | not yet measured | not yet measured |
| `AnthropicBackend` | `claude-haiku-4-5` | ❌ | 200k | $0.001800 | $0.003200 | not yet measured | not yet measured |
| `AnthropicBackend` | `claude-opus-5` | ❌ | 1M | $0.009000 | $0.016000 | not yet measured | not yet measured |
| `ScriptedBackend` | — | ✅ | — | $0 | $0 | ~0 ms | deterministic |

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
* **Neither backend uses a vendor SDK.** Both speak the documented HTTP API through
  `httpx`. Streaming and cancellation are properties of the transport, and barge-in has to
  close the socket rather than trust an SDK's context manager to get there.

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
uv run pytest tests/unit/compute -q                      # 170 tests, no network
uv run python research/benchmarks/compute/run.py         # eval delta table
```

## Known gaps

* No eval against a real model. Every quality claim here is about A.R.S's own behaviour
  (routing, framing, taint, language selection), not about generation quality.
* `sensitivity.py` ships a regex baseline written by ml-engineer. The real rule set and
  the redaction-rather-than-refusal path belong to `security-engineer`;
  `SensitivityClassifier` is the seam.
* Token counts are a calibrated character-ratio estimate, not a tokenizer. Deliberately
  pessimistic. `TokenEstimator` is the seam for a real one.
* `docs/architecture/overview.md` is owned by `system-architect` and was not edited; it
  needs one line pointing here from the "Where things live" table.
