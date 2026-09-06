"""Did the numbers survive? A reference-free check, and the one that matters most.

English writes four thousand two hundred as `4,200`. Romanian and German both write it
`4.200`. So a translator moving between EN and either of the other two has to *change the
separator*, and a translator moving between RO and DE has to *not*. Both mistakes are
silent, both produce fluent output, and both are off by a factor of a thousand:

    DE  "Die monatliche Miete beträgt 4.200 Lei"
    EN  "The monthly rent is 4.200 lei"     <- read in English, that is four point two

Measured on qwen3:14b, this is not hypothetical: it converts correctly EN<->DE and
EN<->RO, which is why a naive substring check reported "the model dropped 4.200" when the
model had in fact done the right thing. The check below reads the hypothesis under the
*target* language's convention — which is what the human reading it will do — and
compares values, not spellings.

This needs no reference translation, so unlike chrF++ it can run on live traffic. It is
the metric that belongs in production telemetry: every translated turn reports whether
every numeric value in the source appears in the output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Group/decimal conventions per language. Adding a language means adding a row.
COMMA_DECIMAL: frozenset[str] = frozenset({"ro", "de"})
"""Languages where `,` is the decimal mark and `.` groups thousands."""

_NUMBER = re.compile(r"\d[\d.,  ]*\d|\d")
"""A numeric literal, possibly with grouping. Ends on a digit, so German ordinals
(`1. Mai`) yield `1` rather than swallowing the following word."""


def parse(token: str, language: str) -> float | None:
    """Read a numeric literal the way a reader of `language` would read it."""
    t = token.replace(" ", "").replace(" ", "")
    if language in COMMA_DECIMAL:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def values(text: str, language: str) -> list[float]:
    out = []
    for m in _NUMBER.finditer(text or ""):
        v = parse(m.group(0), language)
        if v is not None:
            out.append(v)
    return out


@dataclass(frozen=True, slots=True)
class NumeralCheck:
    source_values: tuple[float, ...]
    output_values: tuple[float, ...]
    missing: tuple[float, ...]
    """Values in the source that no value in the output equals, read in the target's own
    convention. Non-empty is a hard failure: either a number was dropped, or the
    separator convention was not converted and the output now means something else."""

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def kept(self) -> int:
        return len(self.source_values) - len(self.missing)


def check(source: str, output: str, src_lang: str, dst_lang: str) -> NumeralCheck:
    src = values(source, src_lang)
    out = list(values(output, dst_lang))
    missing = []
    for v in src:
        # Tolerant to 1e-9 only: this is about 4200 vs 4.2, not about rounding.
        hit = next((o for o in out if abs(o - v) < 1e-9), None)
        if hit is None:
            missing.append(v)
        else:
            out.remove(hit)
    return NumeralCheck(tuple(src), tuple(values(output, dst_lang)), tuple(missing))


__all__ = ["COMMA_DECIMAL", "NumeralCheck", "check", "parse", "values"]
