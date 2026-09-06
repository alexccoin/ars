"""The tiered brain — answer as cheaply as possible, escalate only when it is not good enough.

Most questions a personal assistant gets do not need a 14-billion-parameter model. "What
did that contract say about the deadline" is a lookup. "What did I ask you yesterday" is a
lookup. Running the GPU for those wastes power, heats the machine, and is *slower* than
just finding the passage.

So A.R.S climbs a ladder and stops at the first rung that answers:

    tier 0  RECALL     an answer already given to this same question   ~1 ms   no model
    tier 1  DOCUMENTS  a passage from the user's own learned files     ~20 ms  CPU only
    tier 2  LOCAL      the local model, on the GPU                     ~600 ms
    tier 3  CLOUD      a cloud model, only if allowed and not private

Rungs 0 and 1 need a similarity threshold, and the honest thing to say about it is that
0.85 is a starting point, not a discovered constant. It is configurable, every decision
records the score that produced it, and when the user says "that is not a good answer" the
system escalates AND remembers that this question needed a higher tier next time. That
feedback loop is what actually tunes the threshold — not the number we guessed today.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import IntEnum

from ars_protocol import (
    ContentBlock, Language, MemoryKind, MemoryRecord, Provenance, Sensitivity,
    SourceKind, TrustLevel,
)


class Tier(IntEnum):
    """Ordered so escalation is `tier + 1` and comparisons read naturally."""

    RECALL = 0
    DOCUMENTS = 1
    LOCAL = 2
    CLOUD = 3

    @property
    def label(self) -> str:
        return {
            Tier.RECALL: "recall",
            Tier.DOCUMENTS: "documents",
            Tier.LOCAL: "local model",
            Tier.CLOUD: "cloud model",
        }[self]

    @property
    def uses_gpu(self) -> bool:
        return self >= Tier.LOCAL

    @property
    def leaves_device(self) -> bool:
        return self is Tier.CLOUD


@dataclass(frozen=True, slots=True)
class TierConfig:
    recall_cosine: float = 0.82
    """Tier 0 gates on the RAW cosine, not the calibrated confidence, because it is
    asking a different question of a different population: not "does this passage answer
    that?" but "is this the same question again?". Measured on the real model
    (research/benchmarks/retrieval_calibration.py, recall section):

        the same question — identical, reworded, or in the other language : 0.842 - 0.899
        a different question about the same topic                         : 0.773 - 0.796

    0.82 sits in that gap. It deliberately accepts a translation — asking in Romanian
    what was answered in English should not cost 600 ms — and rejects a neighbour, because
    returning a stale answer to a subtly different question is the worst thing this tier
    can do. The number happens to be close to CALIBRATION_MIDPOINT; that is a coincidence
    of two unrelated measurements, not a shared constant."""

    document_threshold: float = 0.85
    """Alex's number. Above this, a passage from his own files is very likely the answer
    and no model is needed.

    Same-language only, and that is a measured limit rather than a choice. Asking in the
    language the document is written in scores 0.831-0.844; asking the same thing in the
    other language scores 0.778-0.817, which is the same range unrelated questions reach
    (up to 0.817). No threshold separates those, so a cross-language question is not
    answered from the file — it falls through to the model, which reads the passage as
    context and answers correctly, in the language it was asked in. The tier is bilingual
    (a Romanian question finds a Romanian document at 100%, an English one an English
    document at 99%); what it is not is *cross*-lingual. A bigger model does not fix it —
    e5-base was measured and cannot separate them either, at -0.007, while scoring worse
    on the same-language tier. Do not swap the model for this without re-measuring."""

    document_margin: float = 0.04
    """The top hit must beat the runner-up by this much. Two passages scoring 0.86 and
    0.855 means the question is ambiguous, and picking one at random looks like
    confident nonsense — that is a case for the model, not for retrieval."""

    min_passage_chars: int = 80
    """A 12-character fragment can score well and answer nothing."""

    max_local_context: int = 8
    allow_cloud: bool = False


@dataclass
class Answer:
    text: str
    tier: Tier
    score: float = 0.0
    citations: tuple[str, ...] = ()
    elapsed_ms: float = 0.0
    escalated_from: Tier | None = None
    reason: str = ""
    """Why this tier. Shown in the UI so the user can see when the GPU was spent and why."""

    language: Language = Language.EN
    can_escalate: bool = True
    can_learn: bool = True
    """False when the turn errored. Such a turn still has something worth saying and
    nothing worth remembering — caching "the local model isn't responding" makes it the
    permanent answer to that question."""


@dataclass
class TierStats:
    """What the UI shows about where the work actually went."""

    counts: dict[str, int] = field(default_factory=dict)
    gpu_turns: int = 0
    total_turns: int = 0
    saved_gpu_turns: int = 0

    def record(self, answer: Answer) -> None:
        self.total_turns += 1
        self.counts[answer.tier.label] = self.counts.get(answer.tier.label, 0) + 1
        if answer.tier.uses_gpu:
            self.gpu_turns += 1
        else:
            self.saved_gpu_turns += 1

    @property
    def gpu_avoided_pct(self) -> float:
        return 100.0 * self.saved_gpu_turns / self.total_turns if self.total_turns else 0.0


# --------------------------------------------------------------------- confidence
#
# Alex asked for "85% match". A raw cosine does not mean that, and showing one as a
# percentage would be a number that looks like a probability and is not. So the cosine is
# mapped through a logistic placed on the *decision boundary* measured by
# research/benchmarks/retrieval_calibration.py, on the real embedding model, with the
# same questions asked in English and Romanian:
#
#     the passage that DOES answer the question : cosine 0.825 - 0.883  (EN and RO alike)
#     the best passage that does not            : cosine up to 0.818
#     a question no document answers            : cosine up to 0.817
#
# Midpoint sits halfway between the weakest true match and the strongest false one, and
# the steepness is set so the weakest true match reads exactly 85% — which puts the
# strongest false match at 15%. That is what the number in the UI means: not "85% similar"
# but "85% of the way from the noise floor to a real answer".
#
# The separation is real but it is 0.007 wide, so these two constants are the first thing
# to re-fit when the embedding model, the chunk size, or the kind of document changes.
# The benchmark prints them ready to paste, and refuses to print them at all when the two
# populations overlap — which is what the previous model did, in Romanian especially, and
# is how "what is the capital of Portugal?" came back as an employment contract at 97%.
CALIBRATION_MIDPOINT = 0.8214
CALIBRATION_STEEPNESS = 0.00195


def confidence_from_cosine(cosine: float | None) -> float:
    """Map raw cosine similarity onto a calibrated 0-1 confidence.

    Saturates rather than overflowing: the steepness is small enough that a cosine far
    below the midpoint would put `exp` past the float range, and a passage being
    *extremely* irrelevant should read 0%, not raise.
    """
    if cosine is None:
        return 0.0
    import math

    z = (cosine - CALIBRATION_MIDPOINT) / CALIBRATION_STEEPNESS
    if z < -60.0:
        return 0.0
    if z > 60.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n{2,}")


def extract_answer(question: str, passage: str, *, max_chars: int = 600) -> str:
    """Pull the part of a passage that actually addresses the question.

    Deliberately dumb — word overlap, no model — because the entire point of this tier is
    that it costs nothing. If it needs to be clever, it should have been tier 2.
    """
    # A retrieved chunk is already small and already topically matched. If the whole thing
    # fits, return the whole thing: picking one sentence out of it can only lose context,
    # and picking the WRONG sentence produces confident nonsense — "when does the lease
    # start" matching on "lease" and returning the deposit clause. Sentence selection is
    # only worth the risk on a passage too long to show.
    if len(passage) <= max_chars:
        return passage.strip()

    sentences = [s.strip() for s in _SENTENCE.split(passage) if s.strip()]
    if not sentences:
        return passage[:max_chars]

    q_words = {w for w in re.findall(r"\w+", question.lower()) if len(w) > 3}
    if not q_words:
        return " ".join(sentences)[:max_chars]

    scored = [
        (len(q_words & {w for w in re.findall(r"\w+", s.lower()) if len(w) > 3}), i, s)
        for i, s in enumerate(sentences)
    ]
    best = max(scored, key=lambda t: (t[0], -t[1]))
    if best[0] == 0:
        return " ".join(sentences)[:max_chars]

    # Keep the neighbours: a sentence lifted out of its context often reverses meaning
    # ("This does not apply when…" is the next sentence).
    i = best[1]
    window = sentences[max(0, i - 1): i + 2]
    return " ".join(window)[:max_chars]


class TieredBrain:
    """Chooses how much machinery a question deserves.

    Holds no model of its own: it is given a memory store and a callable for the model
    tiers, so the escalation policy stays testable and separate from any backend.
    """

    def __init__(self, *, memory, config: TierConfig | None = None) -> None:
        self.memory = memory
        self.config = config or TierConfig()
        self.stats = TierStats()
        self._forced: dict[str, Tier] = {}
        """Questions the user has already rejected a cheap answer for. Normalised text ->
        the tier that finally satisfied them. This is the learning part: the same question
        never wastes the user's time twice."""

    @staticmethod
    def _key(question: str) -> str:
        return " ".join(re.findall(r"\w+", question.lower()))

    def floor_for(self, question: str) -> Tier:
        """The lowest tier this question is allowed to start at."""
        return self._forced.get(self._key(question), Tier.RECALL)

    def remember_escalation(self, question: str, tier: Tier) -> None:
        key = self._key(question)
        if tier > self._forced.get(key, Tier.RECALL):
            self._forced[key] = tier

    async def try_cheap_tiers(
        self, question: str, *, language: Language = Language.EN
    ) -> Answer | None:
        """Tiers 0 and 1. Returns None when the question deserves a model.

        No GPU is touched on this path, and no network.
        """
        started = time.perf_counter()
        floor = self.floor_for(question)
        if floor >= Tier.LOCAL:
            return None

        if hasattr(self.memory, "recall_scored"):
            hits = await self.memory.recall_scored(question, limit=6, language=None)
            # The cosine rides along: the two tiers below gate on different things and
            # collapsing them to one number early is how the wrong one gets used.
            scored = [(confidence_from_cosine(h.cosine), h.cosine or 0.0, h.record)
                      for h in hits]
        else:  # a store without scores cannot support the cheap tiers at all
            return None
        if not scored:
            return None

        scored.sort(key=lambda triple: triple[0], reverse=True)
        top_score, top_cosine, top = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0

        # ---- tier 0: this exact question has been answered before
        if (floor <= Tier.RECALL and top.kind is MemoryKind.FACT
                and top_cosine >= self.config.recall_cosine
                and top.text.startswith("Q: ")):
            body = top.text.split("\nA: ", 1)
            if len(body) == 2:
                return self._finish(Answer(
                    text=body[1], tier=Tier.RECALL, score=top_score,
                    citations=("previously answered",), language=language,
                    reason=f"identical question answered before ({top_score:.0%} match)",
                ), started)

        # ---- tier 1: a passage from Alex's own documents
        if top.kind is not MemoryKind.DOCUMENT:
            return None
        if top_score < self.config.document_threshold:
            return None
        if len(top.text) < self.config.min_passage_chars:
            return None
        if runner_up > 0 and top_score - runner_up < self.config.document_margin:
            # Two passages score alike. Either they disagree or they cover different
            # matters that sound similar; picking one is how retrieval produces confident
            # nonsense. That is a question for the model.
            return None

        cite = (top.provenance.label or top.provenance.uri or "your documents")
        return self._finish(Answer(
            text=extract_answer(question, top.text),
            tier=Tier.DOCUMENTS, score=top_score, citations=(cite,), language=language,
            reason=f"found in {cite} ({top_score:.0%} match, no GPU used)",
        ), started)

    @staticmethod
    def _score_of(record: MemoryRecord) -> float:
        return float(getattr(record, "score", 0.0) or 0.0)

    def _finish(self, answer: Answer, started: float) -> Answer:
        answer.elapsed_ms = (time.perf_counter() - started) * 1000
        self.stats.record(answer)
        return answer

    def context_for_model(self, hits: tuple[MemoryRecord, ...]) -> tuple[ContentBlock, ...]:
        """Retrieved passages, handed to the model as data with their provenance intact.

        A document the user uploaded is USER_DATA, not USER: they may have uploaded a PDF
        that someone else wrote, and a PDF is a perfectly good place to hide an instruction
        aimed at the model.
        """
        return tuple(
            ContentBlock(
                text=h.text,
                provenance=Provenance(
                    source=SourceKind.LOCAL_FILE,
                    trust=TrustLevel.USER_DATA,
                    uri=h.provenance.uri,
                    label=h.provenance.label,
                ),
            )
            for h in hits[: self.config.max_local_context]
        )

    async def learn_answer(self, question: str, answer: Answer,
                           *, language: Language = Language.EN) -> None:
        """Store a good answer so the same question costs nothing next time.

        Only model-tier answers are worth storing: caching a tier-1 extract would just
        duplicate a document we already have, and caching a tier-0 hit is a loop.
        """
        if answer.tier < Tier.LOCAL or not answer.text.strip():
            return
        if not answer.can_learn:
            return
        await self.memory.remember(MemoryRecord(
            kind=MemoryKind.FACT,
            text=f"Q: {question}\nA: {answer.text}",
            language=language,
            sensitivity=Sensitivity.PERSONAL,
            provenance=Provenance(
                source=SourceKind.SKILL_OUTPUT, trust=TrustLevel.SYSTEM,
                label=f"answered by {answer.tier.label}",
            ),
        ))
