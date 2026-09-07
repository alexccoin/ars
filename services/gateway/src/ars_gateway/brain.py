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
import unicodedata
import time
from collections.abc import Sequence
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
    recall_cosine: float = 0.88
    recall_margin: float = 0.04
    """Tier 0 gates on the RAW cosine and on a margin over the runner-up, because neither
    separates on its own. Measured against a real store, after the first version of this
    shipped and got it wrong:

        the same question — identical, reworded, or translated : cosine 0.846 - 0.899
        a different short question                             : cosine 0.737 - 0.850

    Those overlap. So does the margin, taken alone. The first calibration used 0.82 and a
    bare cosine, fitted on long specific questions where "same" scored 0.842+ and
    "different topic" scored 0.796 — a real gap, in that population. It does not
    generalise: short conversational utterances live in a much tighter band, and against a
    store containing "Q: yes" and "Q: hey there", the gate fired on nearly everything.
    Alex asked "how are you" and was told who created A.R.S, at 100% confidence. A rent
    question matched the cached answer to "yes" at 0.832.

    0.88 with a 0.04 margin rejects every different question measured, with the highest
    false candidate at 0.850. It also rejects three genuine recalls — including asking in
    the other language, which the previous gate deliberately allowed. That is the trade,
    taken knowingly: a missed cache hit costs a few seconds of GPU and produces a correct
    answer, and a false cache hit produces a confident wrong one and stores nothing to
    show the user why. The costs are not symmetric, so the threshold is not centred.

    Two changes make the loss smaller than it looks: the document tier now bridges
    languages at ingest, and `worth_caching` keeps greetings and acknowledgements out of
    the store, so the attractors that caused this are not created in the first place."""

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


# --------------------------------------------------------------------- what to cache
#
# A cached answer is only useful if asking the question again means the same thing. "Hi",
# "yes", "thanks" and "ok" fail that: they are turns, not questions, and their answers are
# not facts about anything. Caching them is also actively harmful, because a short generic
# string sits in the dense middle of the embedding space where everything is near
# everything — "Q: yes" ended up as the nearest neighbour of "How much is the monthly
# rent?" at 0.832, and served its answer.
#
# Stop words in all three languages, because the check has to work on a Romanian greeting
# as well as an English one.
_STOPWORDS = frozenset("""
a an the this that these those there here is are was were be been am do does did doing
i you he she it we they me him her us them my your his its our their of to in on at for
with from by about into over what when where why how which who whom whose can could will
would shall should may might must not no yes ok okay please thanks thank sorry hi hey
hello goodbye bye good morning evening night sure yeah yep nope and or but if then than
un o el ea noi voi eu tu ce cum cand unde cine care este sunt nu da bine multumesc merci
salut buna ziua seara noapte pa te va imi iti isi mie tie lui ei si sau dar daca la de pe
cu pentru din ca mai foarte prea putin mult
der die das den dem des ein eine einen einem einer und oder aber wenn dann als dass ist
sind war waren bin bist ich du er sie es wir ihr mich dich uns euch mir dir nicht kein
ja nein danke bitte hallo tschuss guten morgen abend nacht was wann wo warum wie wer
""".split())

_STOPWORDS = _STOPWORDS | frozenset("""
cat cata cate cati cand cine care cum unde incat oare
viel viele wieviel hoch lange oft weit teuer gross gross
much many long often far cost costs
""".split())
"""Interrogatives and quantifiers, folded. "How much", "cât este" and "wie hoch" are the
scaffolding of a question, not its subject — and a question's subject is what has to be
found in the passage that claims to answer it."""

_VOLATILE = frozenset("""
now today tonight tomorrow yesterday currently current date time clock hour weather
temperature forecast news price rate score today's latest
acum azi astazi maine ieri diseara data ora ceas vremea temperatura pret curs stiri
jetzt heute morgen gestern datum uhr uhrzeit wetter temperatur nachrichten preis kurs
""".split())
"""Words that make an answer true only at the moment it was given.

The date is the clearest case: A.R.S was asked "ce zi este azi?", answered correctly, and
cached it — and a cached date is wrong by tomorrow, served from tier 0 at 100% confidence
in 8 ms with no model involved to notice. The same applies to a temperature, a price or a
score. This is not about the question being unimportant; it is about the answer having a
shelf life shorter than the cache."""

MIN_CACHEABLE_CONTENT_WORDS = 2
"""Two words that are not stop words. One is not enough: "who created you" reduces to
{created} and "how are you" reduces to {} — but so does "thanks", and the difference
between them has to be visible to this function, not to a threshold downstream."""


def worth_caching(question: str) -> bool:
    """Whether an answer to this question is worth storing for next time.

    Deliberately strict. The cost of not caching is that a repeated question costs the GPU
    again; the cost of caching wrongly is a permanent wrong answer served at 100%
    confidence, which is what happened.
    """
    words = [_fold(w) for w in re.findall(r"\w+", question) if len(w) > 1]
    if any(w in _VOLATILE for w in words):
        # The answer expires. Caching it means confidently serving yesterday's date, or
        # last week's weather, long after it stopped being true.
        return False
    content = [w for w in words if w not in _STOPWORDS]
    if len(content) >= MIN_CACHEABLE_CONTENT_WORDS:
        return True
    # One content word can still be a real question if the question is not a fragment:
    # "who created you" and "what is escrow" are worth keeping; "thanks" is not.
    return len(content) == 1 and len(words) >= 3


def distinctive_word_present(question: str, passage: str, others: Sequence[str]) -> bool:
    """Does the passage contain the most distinctive word of the question?

    The embedding cannot answer this. Measured against a real store: "cum e vremea la
    Cluj?" scores 0.830 against an employment contract, and "What is the annual paid
    leave?" scores 0.831 against the contract that answers it. Separation of one
    thousandth — because both questions name Cluj, and in a compressed multilingual space
    a shared proper noun is most of the signal. The tier answered a weather question with
    a salary.

    So a second, independent test. Of the question's content words, take the one that
    appears in the FEWEST of the candidate passages — the one carrying the most
    information about what was actually asked — and require the chosen passage to contain
    it. "vremea" appears in no contract, so no contract may answer a question about it, at
    any cosine. "salariul" appears in one, and that one may answer.

    Deliberately literal: it costs a paraphrase that shares no vocabulary with its own
    document, which then goes to the model and is answered correctly a few seconds later.
    That is the cheap direction of this error.
    """
    words = _content_words(question)
    if not words:
        return False
    haystacks = [_fold_text(p) for p in others]
    top = _fold_text(passage)
    # Document frequency across the candidates, so "Cluj" in every contract counts for
    # little and "Kaution" in one counts for a lot.
    frequency = {w: sum(w in h for h in haystacks) for w in words}
    rarest = min(words, key=lambda w: (frequency[w], -len(w)))
    return rarest in top


def _fold(word: str) -> str:
    """Strip diacritics for matching. `cât` and `cat`, `Kaution` and `kaution`.

    Without this the stop-word list — written in ASCII — never matched a Romanian or
    German question word, so `cât` survived as a "content" word, appeared in no document,
    and was therefore chosen as the question's most distinctive term. Every Romanian
    question was then rejected for not finding `cât` in its own answer.
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", word.lower())
        if unicodedata.category(c) != "Mn"
    ).replace("ß", "ss")


def _fold_text(text: str) -> str:
    return " ".join(_fold(w) for w in re.findall(r"\w+", text))


def _content_words(text: str) -> list[str]:
    return [f for w in re.findall(r"\w+", text)
            if len(f := _fold(w)) > 2 and f not in _STOPWORDS]


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

        # The runner-up has to be a genuinely different passage. A translated copy is the
        # same passage in another language: it scores almost identically by construction,
        # so counting it as a rival trips the ambiguity rule below and turns an answerable
        # question into a GPU turn. Measured on documents translated at ingest: 2 of 12
        # answerable questions became unanswerable, two of which the untranslated index
        # had answered.
        def same_passage(other: MemoryRecord) -> bool:
            ids = {top.id, top.origin_id} - {None}
            return other.id in ids or (other.origin_id is not None and other.origin_id in ids)

        rivals = [score for score, _cosine, record in scored[1:] if not same_passage(record)]
        runner_up = rivals[0] if rivals else 0.0

        # ---- tier 0: this exact question has been answered before
        runner_up_cosine = max((c for _s, c, _r in scored[1:]), default=0.0)
        if (floor <= Tier.RECALL and top.kind is MemoryKind.FACT
                and top_cosine >= self.config.recall_cosine
                and top_cosine - runner_up_cosine >= self.config.recall_margin
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

        if not distinctive_word_present(
            question, top.text, [record.text for _s, _c, record in scored]
        ):
            # The passage scores well but does not contain the thing that was asked
            # about. A shared place name is not an answer.
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
        if not worth_caching(question):
            # A greeting is not a question, and its answer is not a fact. Storing it puts
            # a short generic string into the part of the embedding space where everything
            # is near everything, and it becomes the nearest neighbour of questions that
            # have nothing to do with it.
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
