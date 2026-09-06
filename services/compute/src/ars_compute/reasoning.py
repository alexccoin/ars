"""How much the model is allowed to think before it speaks.

This is a latency control with a very sharp edge, and it is worth stating the measurement
that produced this module before the code.

`qwen3:14b` is a reasoning model. Left to its default it emits a thinking block before any
visible content, so the first token the *user* hears arrives only after the whole reasoning
pass has finished. Measured on an M5 Max against a real `/api/chat`, warm, 3 runs, median:

| `think` | first visible token (RO) | first visible token (EN) | thinking |
|---|---:|---:|---:|
| default (on) | 16869 ms | — | yes |
| `true`       | ~2764 ms | 2303 ms | ~550 chars |
| `"low"`      | 2815 ms | — | ~600 chars |
| **`false`**  | **29 ms** | **49 ms** | none |

The budget for `llm_first_token` is 200 ms. Thinking misses it by two orders of magnitude.

Two findings shaped the design:

1. **The intermediate levels do not help on this model.** `think: "low"` produced the same
   ~600 characters of reasoning and the same ~2.8 s delay as `think: true`. There is no
   useful middle tier for qwen3:14b — it is 29 ms or ~2.8 s. So the policy below only ever
   selects OFF or ON, even though the vocabulary can express the levels (other models
   honour them, and the Anthropic backend maps them onto `output_config.effort`).

2. **Thinking tokens are spent from `num_predict`.** At `num_predict: 120` with thinking
   on, the reasoning block consumed the entire allowance and the reply came back *empty*
   (`done_reason: "length"`, 602 characters of thinking, 0 of content). Enabling reasoning
   without also raising the output allowance turns a slow answer into no answer, so
   `OllamaBackend` carries a separate, larger allowance for reasoning turns.

Quality on ordinary assistant turns was unaffected: both settings produced correct,
idiomatic Romanian with proper diacritics.
"""

from __future__ import annotations

from enum import StrEnum


class ReasoningMode(StrEnum):
    """How much internal reasoning to buy, in provider-neutral terms.

    Ordered from cheapest to most expensive. `PROVIDER_DEFAULT` is not "middle" — it means
    "send nothing and let the provider decide", which for a reasoning model on Ollama means
    full thinking and a 17-second first token. It exists so that behaviour can be selected
    deliberately, never reached by accident.
    """

    OFF = "off"
    """No thinking. The only mode that meets the 200 ms first-token budget, and therefore
    the only mode a spoken turn may use."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"

    ON = "on"
    """Think, at the provider's own default depth."""

    PROVIDER_DEFAULT = "provider_default"
    """Send no reasoning parameter at all. Use when a backend rejects the field."""

    @property
    def thinks(self) -> bool:
        return self not in (ReasoningMode.OFF,)

    @property
    def ollama_think(self) -> bool | str | None:
        """Value for the `think` field of Ollama's `/api/chat`. `None` means omit it.

        Ollama validates this field strictly — an unrecognised *value* is a 400 with
        `invalid think value: ... (must be "high", "medium", "low", "max", true, or false)`
        — while ignoring unrecognised *fields* entirely. Both behaviours were verified
        against a live server, and both matter: it means we cannot smuggle the setting
        through under another name, and it means a bad value fails loudly rather than
        silently costing 17 seconds.
        """
        return {
            ReasoningMode.OFF: False,
            ReasoningMode.ON: True,
            ReasoningMode.LOW: "low",
            ReasoningMode.MEDIUM: "medium",
            ReasoningMode.HIGH: "high",
            ReasoningMode.MAX: "max",
            ReasoningMode.PROVIDER_DEFAULT: None,
        }[self]

    @property
    def anthropic_effort(self) -> str | None:
        """Value for `output_config.effort` on the Messages API. `None` means omit it.

        Adaptive thinking on Claude cannot be switched off the way Ollama's can, so OFF
        maps to the lowest effort rather than to nothing. That is the honest mapping: the
        request is "spend as little as possible before answering", and `low` is what that
        means on this provider.
        """
        return {
            ReasoningMode.OFF: "low",
            ReasoningMode.LOW: "low",
            ReasoningMode.MEDIUM: "medium",
            ReasoningMode.HIGH: "high",
            ReasoningMode.MAX: "max",
            ReasoningMode.ON: "high",
            ReasoningMode.PROVIDER_DEFAULT: None,
        }[self]


__all__ = ["ReasoningMode"]
