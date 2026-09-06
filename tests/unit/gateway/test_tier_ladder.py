"""The tier ladder's two gates, and what the number in the UI actually claims.

These are the assertions behind "85% match": that the confidence shown is a calibrated
decision and not a raw cosine wearing a percent sign, that a passage which does not
answer the question cannot reach the threshold, and that tier 0 returns a cached answer
only for the *same* question — including the same question asked in the other language,
and never for a neighbouring one.

The cosines used here are measured values from
research/benchmarks/retrieval_calibration.py, so if the calibration constants and the
model ever drift apart, these fail.
"""

from __future__ import annotations

import pytest
from ars_gateway.brain import Tier, TieredBrain, confidence_from_cosine
from ars_memory.store import Scored
from ars_protocol import (
    Language, MemoryKind, MemoryRecord, Provenance, Sensitivity, SourceKind, TrustLevel,
)

# Measured on intfloat/multilingual-e5-small (see the benchmark's printed table).
COSINE_ANSWERING_PASSAGE = 0.844   # "Cât este chiria pe lună?" vs the Romanian lease
COSINE_WEAKEST_TRUE_MATCH = 0.825  # the weakest true match observed, EN and RO alike
COSINE_STRONGEST_FALSE_MATCH = 0.818
COSINE_UNRELATED_QUESTION = 0.696  # "What is the capital of Portugal?" vs a contract
COSINE_SAME_QUESTION_AGAIN = 0.899
COSINE_SAME_QUESTION_TRANSLATED = 0.846
COSINE_NEIGHBOURING_QUESTION = 0.796


def _memory(text: str, kind: MemoryKind, label: str = "contract.txt") -> MemoryRecord:
    return MemoryRecord(
        kind=kind,
        text=text,
        language=Language.RO,
        sensitivity=Sensitivity.PERSONAL,
        provenance=Provenance(
            source=SourceKind.LOCAL_FILE, trust=TrustLevel.USER_DATA, label=label
        ),
    )


class _FakeMemory:
    """A memory store that returns exactly the scores a test wants to reason about."""

    def __init__(self, hits: list[Scored]) -> None:
        self._hits = hits

    async def recall_scored(self, query: str, *, limit: int = 8, language=None):
        return tuple(self._hits[:limit])


PASSAGE = (
    "CONTRACT DE ÎNCHIRIERE — Strada Republicii 42, Cluj-Napoca\n\n"
    "Chiria pe lună este de 4.200 lei, plătibilă până în data de 5 a fiecărei luni.\n"
    "Garanția este de două chirii, adică 8.400 lei."
)
CACHED = "Q: Cât este chiria pe lună?\nA: Chiria pe lună este de 4.200 lei."


def _document_hit(cosine: float) -> Scored:
    return Scored(rank=cosine, cosine=cosine, record=_memory(PASSAGE, MemoryKind.DOCUMENT))


def _cached_answer_hit(cosine: float) -> Scored:
    return Scored(rank=cosine, cosine=cosine,
                  record=_memory(CACHED, MemoryKind.FACT, label="answered by local model"))


# --------------------------------------------------------------------- the calibration

def test_the_percentage_is_a_decision_not_a_cosine() -> None:
    """85% means "as far above the noise floor as a real answer sits" — the whole point
    of the mapping. A raw cosine of 0.825 is not "82% similar" to anything."""
    assert confidence_from_cosine(COSINE_WEAKEST_TRUE_MATCH) >= 0.85
    assert confidence_from_cosine(COSINE_STRONGEST_FALSE_MATCH) < 0.15
    assert confidence_from_cosine(COSINE_UNRELATED_QUESTION) < 0.01


def test_confidence_saturates_instead_of_overflowing() -> None:
    """The logistic is steep enough that a very low cosine would put exp() past the
    float range. A wildly irrelevant passage reads 0%, it does not raise."""
    assert confidence_from_cosine(-1.0) == 0.0
    assert confidence_from_cosine(1.0) == 1.0
    assert confidence_from_cosine(None) == 0.0


# ------------------------------------------------------------------ tier 1: documents

@pytest.mark.asyncio
async def test_a_passage_that_answers_the_question_skips_the_gpu() -> None:
    brain = TieredBrain(memory=_FakeMemory([_document_hit(COSINE_ANSWERING_PASSAGE)]))
    answer = await brain.try_cheap_tiers("Cât este chiria pe lună?", language=Language.RO)

    assert answer is not None
    assert answer.tier is Tier.DOCUMENTS
    assert not answer.tier.uses_gpu
    assert "4.200 lei" in answer.text
    assert answer.citations == ("contract.txt",)


@pytest.mark.asyncio
async def test_a_question_no_document_answers_goes_to_the_model() -> None:
    brain = TieredBrain(memory=_FakeMemory([_document_hit(COSINE_UNRELATED_QUESTION)]))

    assert await brain.try_cheap_tiers("What is the capital of Portugal?") is None


@pytest.mark.asyncio
async def test_two_passages_scoring_alike_go_to_the_model() -> None:
    """Ambiguity is a reason to think, not to guess: picking one of two passages that
    score the same is how retrieval produces confident nonsense."""
    brain = TieredBrain(memory=_FakeMemory([
        _document_hit(COSINE_ANSWERING_PASSAGE),
        _document_hit(COSINE_ANSWERING_PASSAGE - 0.0001),
    ]))

    assert await brain.try_cheap_tiers("Cât este chiria pe lună?") is None


@pytest.mark.asyncio
async def test_a_fragment_too_short_to_answer_anything_is_not_an_answer() -> None:
    short = Scored(rank=0.9, cosine=COSINE_ANSWERING_PASSAGE,
                   record=_memory("4.200 lei", MemoryKind.DOCUMENT))
    brain = TieredBrain(memory=_FakeMemory([short]))

    assert await brain.try_cheap_tiers("Cât este chiria pe lună?") is None


# --------------------------------------------------------------------- tier 0: recall

@pytest.mark.asyncio
async def test_the_same_question_returns_the_cached_answer() -> None:
    brain = TieredBrain(memory=_FakeMemory([_cached_answer_hit(COSINE_SAME_QUESTION_AGAIN)]))
    answer = await brain.try_cheap_tiers("Cât este chiria pe lună?", language=Language.RO)

    assert answer is not None
    assert answer.tier is Tier.RECALL
    assert answer.text == "Chiria pe lună este de 4.200 lei."


@pytest.mark.asyncio
async def test_the_same_question_in_the_other_language_still_hits_the_cache() -> None:
    """Rule 5, at the cheapest tier: asking in English what was answered in Romanian is
    the same question, and should not cost 600 ms."""
    brain = TieredBrain(
        memory=_FakeMemory([_cached_answer_hit(COSINE_SAME_QUESTION_TRANSLATED)])
    )
    answer = await brain.try_cheap_tiers("How much is the monthly rent?", language=Language.EN)

    assert answer is not None
    assert answer.tier is Tier.RECALL


@pytest.mark.asyncio
async def test_a_neighbouring_question_does_not_get_a_stale_answer() -> None:
    """"How much is the deposit?" is not "how much is the rent?", and answering it from
    the cache would be worse than spending the GPU."""
    brain = TieredBrain(memory=_FakeMemory([_cached_answer_hit(COSINE_NEIGHBOURING_QUESTION)]))

    assert await brain.try_cheap_tiers("Cât este garanția?") is None


# ------------------------------------------------------------------------- escalation

@pytest.mark.asyncio
async def test_a_rejected_answer_makes_the_same_question_skip_the_cheap_tiers() -> None:
    """"That was not good enough" has to change what happens next time, or the 85% is
    just a number we guessed and never revisited."""
    brain = TieredBrain(memory=_FakeMemory([_document_hit(COSINE_ANSWERING_PASSAGE)]))
    question = "Cât este chiria pe lună?"
    assert await brain.try_cheap_tiers(question) is not None

    brain.remember_escalation(question, Tier.LOCAL)

    assert await brain.try_cheap_tiers(question) is None
    assert await brain.try_cheap_tiers("  cât  este   CHIRIA pe   lună ?? ") is None


# --------------------------------------------------------------------------- learning

class _RecordingMemory(_FakeMemory):
    def __init__(self) -> None:
        super().__init__([])
        self.remembered: list[MemoryRecord] = []

    async def remember(self, record: MemoryRecord) -> MemoryRecord:
        self.remembered.append(record)
        return record


@pytest.mark.asyncio
async def test_a_good_answer_is_learned() -> None:
    from ars_gateway.brain import Answer

    memory = _RecordingMemory()
    brain = TieredBrain(memory=memory)
    await brain.learn_answer(
        "Cât este chiria pe lună?",
        Answer(text="Chiria pe lună este de 4.200 lei.", tier=Tier.LOCAL),
        language=Language.RO,
    )

    assert len(memory.remembered) == 1
    assert memory.remembered[0].text.startswith("Q: Cât este chiria pe lună?")


@pytest.mark.asyncio
async def test_a_failed_turn_is_answered_but_never_learned() -> None:
    """When the model is unreachable the turn still says something useful. Caching it
    would make "the local model isn't responding" the permanent answer to that question,
    returned from tier 0 at 100% long after the model came back — which is exactly what
    happened before `can_learn` existed."""
    from ars_gateway.brain import Answer

    memory = _RecordingMemory()
    brain = TieredBrain(memory=memory)
    await brain.learn_answer(
        "What is the annual paid leave?",
        Answer(
            text="The local model isn't responding right now, so I can't answer that.",
            tier=Tier.LOCAL,
            can_learn=False,
        ),
    )

    assert memory.remembered == []


# ----------------------------------------------------------------- derived passages

@pytest.mark.asyncio
async def test_a_passage_does_not_compete_with_its_own_translation() -> None:
    """Translating documents at ingest is what makes a German lease answer an English
    question. It also puts a near-identical twin of every passage in the index, and the
    ambiguity rule — which exists to refuse when two DIFFERENT passages score alike —
    would fire on every single hit. The twin is the same passage, not a rival."""
    original = _memory(PASSAGE, MemoryKind.DOCUMENT)
    translation = _memory(PASSAGE, MemoryKind.DOCUMENT).model_copy(
        update={"id": "mem_translation", "origin_id": original.id, "language": Language.EN}
    )
    brain = TieredBrain(memory=_FakeMemory([
        Scored(rank=0.9, cosine=COSINE_ANSWERING_PASSAGE, record=original),
        Scored(rank=0.9, cosine=COSINE_ANSWERING_PASSAGE - 0.0001, record=translation),
    ]))

    answer = await brain.try_cheap_tiers("Cât este chiria pe lună?", language=Language.RO)

    assert answer is not None, "a passage was refused for being ambiguous with itself"
    assert answer.tier is Tier.DOCUMENTS


@pytest.mark.asyncio
async def test_two_genuinely_different_passages_still_refuse() -> None:
    """The rule must keep working for what it was written for."""
    brain = TieredBrain(memory=_FakeMemory([
        _document_hit(COSINE_ANSWERING_PASSAGE),
        _document_hit(COSINE_ANSWERING_PASSAGE - 0.0001),
    ]))

    assert await brain.try_cheap_tiers("Cât este chiria pe lună?") is None
