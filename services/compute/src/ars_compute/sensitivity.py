"""Deciding whether a piece of context is too private to leave the device.

`Sensitivity` is declared on `MemoryRecord`, but a `ContentBlock` on its way into a prompt
has no sensitivity field — the protocol deliberately keeps that on the record. The router
still has to answer "is there SENSITIVE content in this context" before it may talk to a
cloud provider, so this module provides two answers and takes the worse of them:

  1. **Declared.** The ledger the context assembler builds while it pulls memory records
     in. This is authoritative: the user or the memory service said so.
  2. **Detected.** A pattern scan over the block text. This exists because the declared
     path can be wrong or missing — a sensitive number pasted into a chat message is a
     `ContentBlock` from the keyboard with no record behind it, and it must still not be
     shipped to a provider.

Detected-only is never used to *upgrade* retention or to hide things from the user; it is
used for exactly one decision, "may this leave the machine", where a false positive costs
a local-model turn and a false negative costs the user's medical history.

OWNERSHIP: the patterns below are a working default written by ml-engineer. The real
rule set, and the redaction (as opposed to refusal) path, belong to `security-engineer`.
`SensitivityClassifier` is the seam: drop in a replacement, change nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ars_protocol import ContentBlock, Provenance, Sensitivity


@runtime_checkable
class SensitivityClassifier(Protocol):
    def classify(self, text: str) -> tuple[Sensitivity, str | None]:
        """Returns the sensitivity and the name of the rule that fired (for the audit
        trail). Never returns the matched text: an audit log must not become a second
        copy of the secret."""


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # --- credentials and keys ------------------------------------------------------
    ("secret.api_key", re.compile(
        r"\b(sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})")),
    ("secret.private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("secret.password", re.compile(
        r"\b(?:password|passwd|parol[ăa]|parola\s+mea|passphrase|api[_ -]?key|secret|token)\b"
        r"\s*(?:is|este|e|:|=)\s*\S{4,}", re.IGNORECASE)),
    # --- finance -------------------------------------------------------------------
    ("finance.iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("finance.card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("finance.salary", re.compile(
        r"\b(?:my salary|i earn|i make|salariul meu|c[âa][șs]tig|venitul meu|"
        r"soldul meu|my balance|net worth)\b", re.IGNORECASE)),
    # --- identity ------------------------------------------------------------------
    ("identity.cnp", re.compile(r"\b[1-8]\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{6}\b")),
    ("identity.ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # --- health --------------------------------------------------------------------
    ("health.condition", re.compile(
        r"\b(?:diagnos(?:is|ed|tic|ticat)|prescription|re[țt]et[ăa]|my (?:doctor|therapist)|"
        r"medicul meu|psiholog|psihiatru|antidepres|chemotherapy|chimioterapie|"
        r"biopsy|biopsie|HIV|cancer|depresie|depression|anxietate|anxiety disorder|"
        r"pregnan(?:t|cy)|[îi]ns[ăa]rcinat[ăa])\b", re.IGNORECASE)),
    # --- intimate life -------------------------------------------------------------
    ("intimate.relationship", re.compile(
        r"\b(?:divor[țt]|divorce|affair|aman(?:t|t[ăa])|avort|abortion|"
        r"terapie de cuplu|couples therapy)\b", re.IGNORECASE)),
)


class PatternClassifier:
    """Regex baseline. Fast enough for the 80 ms assembly slice (single pass, precompiled).

    Known limitations, stated rather than hidden: it is recall-oriented and will flag a
    16-digit order number as a card. The cost of that false positive is that the turn
    stays local, which is the deployment default anyway.
    """

    name = "pattern-v1"

    def classify(self, text: str) -> tuple[Sensitivity, str | None]:
        for rule, pattern in _RULES:
            if pattern.search(text):
                return Sensitivity.SENSITIVE, rule
        return Sensitivity.PERSONAL, None


DEFAULT_CLASSIFIER: SensitivityClassifier = PatternClassifier()


@dataclass(frozen=True)
class SensitivityFinding:
    sensitivity: Sensitivity
    rule: str | None
    provenance: Provenance
    declared: bool
    """True when it came from a MemoryRecord's own field rather than from the scanner."""


@dataclass
class SensitivityLedger:
    """Per-turn record of how sensitive each block is.

    Built by the context assembler while it is already walking the blocks, so it costs one
    extra dict write per block rather than a second pass.
    """

    classifier: SensitivityClassifier = DEFAULT_CLASSIFIER
    _declared: dict[int, Sensitivity] = field(default_factory=dict)
    _findings: list[SensitivityFinding] = field(default_factory=list)

    def declare(self, block: ContentBlock, sensitivity: Sensitivity) -> None:
        """Record what the memory service said about this block. Authoritative."""
        self._declared[id(block)] = sensitivity
        if sensitivity is Sensitivity.SENSITIVE:
            self._findings.append(
                SensitivityFinding(sensitivity, "declared", block.provenance, declared=True)
            )

    def of(self, block: ContentBlock) -> Sensitivity:
        declared = self._declared.get(id(block))
        detected, _ = self.classifier.classify(block.text)
        if declared is None:
            return detected
        return Sensitivity.SENSITIVE if Sensitivity.SENSITIVE in (declared, detected) else declared

    def scan(self, blocks: tuple[ContentBlock, ...]) -> tuple[SensitivityFinding, ...]:
        """Full scan used by the router immediately before a non-local call. Runs even
        when a ledger was populated, because a block that never went through the
        assembler (a tool result added mid-turn) has no declared entry."""
        out = list(self._findings)
        for b in blocks:
            declared = self._declared.get(id(b))
            if declared is Sensitivity.SENSITIVE:
                continue  # already recorded in _findings
            sens, rule = self.classifier.classify(b.text)
            if sens is Sensitivity.SENSITIVE:
                out.append(SensitivityFinding(sens, rule, b.provenance, declared=False))
        return tuple(out)

    @property
    def findings(self) -> tuple[SensitivityFinding, ...]:
        return tuple(self._findings)

    @property
    def has_sensitive(self) -> bool:
        return bool(self._findings)


__all__ = [
    "DEFAULT_CLASSIFIER", "PatternClassifier", "SensitivityClassifier",
    "SensitivityFinding", "SensitivityLedger",
]
