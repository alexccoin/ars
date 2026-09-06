# A.R.S — architecture overview

*Last verified: 2026-09-06 against commit `initial`. Owner: `system-architect`.*

A.R.S is a **private, local-first voice and compute assistant**. It listens, speaks
English and Romanian, acts on the user's own accounts under explicit grants, searches
and scrapes the web and GitHub, and learns how the user wants it to behave by asking.

## The four constraints that drive every decision

1. **Privacy is structural, not a setting.** The default deployment runs entirely on the
   user's own hardware. Cloud backends are opt-in per capability, and `SENSITIVE` data
   never leaves the device regardless of configuration.
2. **Latency is a budget, not an aspiration.** Voice interaction dies above ~1.2 s to
   first audio. Every component is allocated a slice and must report against it.
3. **The assistant is fed hostile text.** Email and scraped pages contain instructions
   aimed at the model. Provenance and taint are enforced in the protocol itself.
4. **It runs on a phone.** Anything that cannot degrade to a constrained device is
   designed wrong. The phone may be a thin client to a home node, or fully standalone.

## Components

```mermaid
flowchart TB
    subgraph clients["Clients — apps/"]
        D[desktop]:::c
        P[phone]:::c
        W[web]:::c
        C[cli]:::c
    end

    G["gateway<br/><i>WebSocket, session, streaming</i>"]:::s

    subgraph pipeline["Voice — services/voice"]
        WK["wakeword<br/><i>always on, on-device</i>"]:::v
        VAD[VAD + endpointing]:::v
        ASR["ASR — EN/RO auto-detect"]:::v
        TTS["TTS — EN/RO, streaming"]:::v
    end

    CO["compute<br/><i>reasoning, routing, context</i>"]:::s
    GU{{"GUARD<br/><i>allow / ask / deny</i>"}}:::g
    AU["auth<br/><i>grants, OAuth vault, audit</i>"]:::g
    ME["memory<br/><i>facts + learned behaviour</i>"]:::s
    SK["skills-runtime<br/><i>sandboxed execution</i>"]:::s

    subgraph skills["Skills"]
        EM[email]:::k
        SE[web search]:::k
        SC[web scraper]:::k
        GH[github scraper]:::k
        FS[files]:::k
    end

    clients <-->|"ars protocol"| G
    G <--> pipeline
    G <--> CO
    CO <--> ME
    CO -->|"every tool call"| GU
    GU <--> AU
    GU -->|ALLOW only| SK
    SK --> skills
    skills -.->|"EXTERNAL — taints turn"| CO

    classDef c fill:#e8eef7,stroke:#5b7fa6,color:#1a2733
    classDef s fill:#eef2ea,stroke:#7a9463,color:#1f2a18
    classDef v fill:#f6efe6,stroke:#a68a5b,color:#2b2318
    classDef g fill:#f7e9e9,stroke:#a65b5b,color:#331a1a
    classDef k fill:#f0edf5,stroke:#8a7aa6,color:#241a33
```

The dotted line is the important one: **everything a skill fetches from the outside world
re-enters the reasoning layer marked `EXTERNAL`**, which taints the turn and downgrades
what the guard will allow without asking.

## Latency budget — speech end to first audio

Target **p95 ≤ 1400 ms** on an M-series laptop for a local-model turn with no tool call.
Measured from the moment the user stops speaking, because that is the silence they feel.
Corrected in [ADR 0001](../adr/0001-endpointing-latency-budget.md) — the original table
allocated 400 ms to endpointing, which is not achievable while waiting 700 ms for silence.

| Stage | Budget (p95) | Owner |
|---|---:|---|
| Endpoint confirmation (700 ms silence + detection) | 750 ms | voice-engineer |
| ASR final (after endpoint) | 250 ms | voice-engineer |
| Context assembly + memory recall | 80 ms | backend / ml |
| LLM first token | 200 ms | ml-engineer |
| TTS time-to-first-audio | 120 ms | voice-engineer |
| **Total** | **1400 ms** | system-architect |

Wakeword detection is budgeted separately at **150 ms**: it happens before the user
speaks, so it does not sit on this path.

The endpoint row dominates and is mostly idle waiting. Overlapping ASR and LLM prefill
with the silence window takes the total to ~1050 ms; that is the next voice work item,
not a smaller silence window (ADR 0001).

A turn with a tool call gets a separate budget and **must speak a filler acknowledgement
within 600 ms** — silence while it works reads as failure.

Guard evaluation is allocated **0 ms**: it is an in-process policy check against loaded
grants, with no network call on the hot path. If it ever needs I/O, the design is wrong.

## The tier ladder — how much machine a question is worth

Most questions a personal assistant gets are lookups, and running a 14B model for a
lookup is slower than finding the passage, not faster. So a turn climbs a ladder and
stops at the first rung that answers, in `services/gateway/brain.py`:

| Tier | Answers from | Cost | Gate |
|---|---|---:|---|
| 0 `RECALL` | an answer already given to this same question | ~1 ms, no model | raw cosine ≥ 0.82 |
| 1 `DOCUMENTS` | a passage from the user's own learned files | ~10 ms, CPU only | calibrated confidence ≥ 0.85 |
| 2 `LOCAL` | the local model, on the GPU | ~4-8 s | — |
| 3 `CLOUD` | a cloud model, only if granted and not private | network | — |

The two gates are deliberately on different scales, because they ask different questions
of different populations. Tier 0 compares a question to a question ("is this the same
thing again?"); tier 1 asks whether a passage answers one. Both numbers come from
`research/benchmarks/retrieval_calibration.py`, which measures them on the real embedding
model with the same questions in English and Romanian, and refuses to emit constants when
the relevant and irrelevant populations overlap.

The percentage the interface shows is that calibration, not a raw cosine: 85% means "as
far above the noise floor as a real answer sits". Every turn reports the tier that
answered it and why, and the escalation endpoint records that a question needed a higher
tier next time — which is what actually tunes the threshold.

**Known limit: the document tier is same-language.** A Romanian question finds a Romanian
document (measured 0.844) and an English one an English document (0.831); the same
question asked in the *other* language scores 0.778-0.817, which is the range unrelated
questions also reach. No threshold separates those, so a cross-language question falls
through to the model — which reads the passage as context and answers correctly, in the
language it was asked.

A larger model does not fix it: `intfloat/multilingual-e5-base` was measured on the same
fixtures and separates cross-language matches from irrelevant ones by **-0.007** — it
cannot tell them apart either — while being *worse* than e5-small on the same-language
tier (-0.014 against +0.007). So the 768-dimensional migration buys nothing and was not
made. Re-run the benchmark against a candidate before proposing that again.

Which model built the vector index is recorded in `memory_meta` and checked on every
open: vectors from two models are not comparable, and a silently mixed index returns the
wrong passage with high confidence. On a mismatch the store re-embeds.

## Trust boundaries

| Boundary | Crossing | Enforcement |
|---|---|---|
| Microphone → system | audio | nothing leaves the device pre-wakeword |
| Skill → reasoning | fetched text | `Provenance` + `TrustLevel.EXTERNAL` |
| Reasoning → real world | tool call | `GuardEngine.evaluate` before any side effect |
| Device → cloud LLM | context | `SENSITIVE` records refused; redaction applied |
| Third-party skill → host | process | subprocess, no ambient creds, network allowlist |

## Deployment shapes

| Shape | Reasoning | Voice | Notes |
|---|---|---|---|
| **Laptop standalone** | local (Ollama) | fully local | reference deployment; no network needed |
| **Home node + phone** | on node | wake/VAD on phone, ASR/TTS on node | phone stays a thin client; battery-friendly |
| **Phone standalone** | small local model | on-device | degraded reasoning, full privacy |
| **Hybrid** | cloud for hard turns | local | opt-in per capability, `SENSITIVE` never routed out |

## Where things live

| Path | Contents |
|---|---|
| `packages/protocol` | wire contracts — **single source of truth**, no service may fork a type |
| `packages/core` | engine interfaces + config; no vendor SDK ever imported outside a backend |
| `services/voice` | wakeword, VAD, ASR, TTS |
| `services/compute` | reasoning, tool routing, context assembly, observation extraction — see [README](../../services/compute/README.md) |
| `services/memory` | facts, embeddings, learned preferences, deletion |
| `services/auth` | grants, consent, OAuth token vault, audit log |
| `services/skills-runtime` | sandboxed skill execution |
| `services/gateway` | WebSocket front door |
