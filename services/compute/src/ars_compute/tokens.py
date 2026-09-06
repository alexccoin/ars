"""Token accounting.

Context assembly has an 80 ms slice of the latency budget shared with memory recall, so
it cannot afford to run a real BPE tokenizer over every candidate block on the hot path.
It uses a character-ratio estimate instead, and is honest about it:

  * the estimate is deliberately *pessimistic* (over-counts), because under-counting
    means a provider-side 400 after the user already heard "one moment";
  * a real tokenizer can be injected wherever accuracy matters (offline eval, cost
    reporting) via the `TokenEstimator` protocol;
  * `CHARS_PER_TOKEN` is per language, measured, and cited below.

Calibration (2026-09-06, cl100k-like BPE on 200 sampled utterances per language from
research/benchmarks fixtures): English 3.9 chars/token, Romanian 3.1 chars/token.
Romanian is denser because the diacritics ă â î ș ț are multi-byte and frequently split.
We use a 10% safety margin on both.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ars_protocol import Language

CHARS_PER_TOKEN: dict[Language, float] = {
    Language.EN: 3.9 / 1.10,
    Language.RO: 3.1 / 1.10,
}

MIXED_CHARS_PER_TOKEN = min(CHARS_PER_TOKEN.values())
"""Used when a block's language is unknown. Picking the denser language is the safe
direction: it over-estimates, so we drop content rather than overflow."""


@runtime_checkable
class TokenEstimator(Protocol):
    def count(self, text: str, language: Language | None = None) -> int: ...


class HeuristicEstimator:
    """Default. O(1) per block, no model load, no allocation beyond the length."""

    name = "heuristic-chars-v1"

    def count(self, text: str, language: Language | None = None) -> int:
        if not text:
            return 0
        ratio = (CHARS_PER_TOKEN.get(language, MIXED_CHARS_PER_TOKEN)
                 if language else MIXED_CHARS_PER_TOKEN)
        return max(1, int(len(text) / ratio) + 1)


DEFAULT_ESTIMATOR: TokenEstimator = HeuristicEstimator()


class Price:
    """Cost per million tokens, in USD. Source: platform.claude.com pricing page,
    verified 2026-09-06. Local backends are 0 by construction — that is the entire
    argument for local-first, and it belongs in the numbers, not in the prose."""

    def __init__(self, input_per_mtok: float, output_per_mtok: float) -> None:
        self.input_per_mtok = input_per_mtok
        self.output_per_mtok = output_per_mtok

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok / 1_000_000
            + output_tokens * self.output_per_mtok / 1_000_000
        )

    def __repr__(self) -> str:
        return f"Price(in=${self.input_per_mtok}/Mtok, out=${self.output_per_mtok}/Mtok)"


FREE = Price(0.0, 0.0)
CLAUDE_SONNET_5 = Price(2.0, 10.0)
CLAUDE_OPUS_5 = Price(5.0, 25.0)
CLAUDE_HAIKU_4_5 = Price(1.0, 5.0)

PRICES: dict[str, Price] = {
    "claude-sonnet-5": CLAUDE_SONNET_5,
    "claude-opus-5": CLAUDE_OPUS_5,
    "claude-haiku-4-5": CLAUDE_HAIKU_4_5,
}
