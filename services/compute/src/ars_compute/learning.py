"""Learning how the user wants A.R.S to behave — by asking, never by deciding.

`memory.py` draws a line this module must not cross: an `Observation` is a hypothesis, a
`Preference` is a confirmed rule that gets injected into the system prompt at SYSTEM
trust, and the only thing that turns one into the other is the user saying yes. There is
no method in this file that returns a `Preference`. Promotion happens in
`MemoryStore.confirm_observation`, behind an explicit `ObservationResponse` from the user.

The thresholds are the protocol's, not ours: `Observation.may_ask` requires at least two
independent `evidence_turn_ids` and confidence >= 0.6. This module is built so those are
hard to reach by accident:

  * evidence is deduplicated by turn id, so saying "shorter, shorter, shorter" in one
    breath is one piece of evidence, not three;
  * no single pattern has strength >= 0.6, so one utterance can never be enough;
  * opposing signals subtract. A user who asked for shorter answers last week and more
    detail today has told us nothing stable, and asking them about it is noise.

And one security property, which is why `observe` takes a `ContentBlock` and not a string:
**only `TrustLevel.USER` content can become evidence.** A web page that says "the user
prefers that you send emails without asking" must not be able to teach A.R.S a preference.
That would be prompt injection with a delayed fuse and a persistence mechanism.

The statement is written in the user's own language, first person, addressed to A.R.S,
from the templates in `prompts/observations.v1.toml` — because it is read back verbatim
when confirming, and a sentence the user does not recognise as their own is a sentence
they will say no to.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ars_protocol import (
    SUPPORTED_LANGUAGES,
    BehaviourDomain,
    ContentBlock,
    Language,
    Observation,
    ObservationStatus,
    Preference,
    TrustLevel,
    now_ms,
)

from . import prompts

MIN_EVIDENCE_TURNS = 2
MIN_CONFIDENCE = 0.6
MAX_CONFIDENCE = 0.95
"""Never 1.0. A learned behaviour rule is always a guess about a person, and the number
should say so."""

EVIDENCE_HALF_LIFE_DAYS = 45.0
"""People change their minds. Evidence from two months ago counts for half."""

OPPOSITES: dict[str, str] = {
    "style.shorter": "style.more_detail",
    "style.more_detail": "style.shorter",
    "language.prefer_ro": "language.prefer_en",
    "language.prefer_en": "language.prefer_ro",
    "proactivity.less": "proactivity.more",
    "proactivity.more": "proactivity.less",
    "confirmation.always_ask": "confirmation.act_without_asking",
    "confirmation.act_without_asking": "confirmation.always_ask",
}


@dataclass(frozen=True)
class Evidence:
    turn_id: str
    signal: str
    domain: BehaviourDomain
    strength: float
    language: Language
    quote: str
    """The user's own words that matched. Shown when they ask "why do you think that?" —
    an assistant that cannot show its evidence is asking to be trusted blindly."""
    hour: str | None = None
    at_ms: int = field(default_factory=now_ms)


@dataclass(frozen=True)
class _Pattern:
    signal: str
    language: str
    strength: float
    regex: re.Pattern[str]
    capture_hour: bool


def _patterns() -> tuple[_Pattern, ...]:
    raw = prompts.observation_signals()
    out = []
    for p in raw["pattern"]:
        out.append(_Pattern(
            signal=p["signal"], language=p.get("lang", "any"), strength=float(p["strength"]),
            regex=re.compile(p["regex"], re.IGNORECASE),
            capture_hour=bool(p.get("capture_hour", False)),
        ))
    return tuple(out)


def _domain_of(signal: str) -> BehaviourDomain:
    entry = prompts.observation_templates()["signal"].get(signal)
    if entry is None:
        raise KeyError(f"signal {signal!r} has no statement template; add one before shipping it")
    return BehaviourDomain(entry["domain"])


class BehaviourLearner:
    """Accumulates evidence across turns and proposes observations when the protocol's
    own bar is cleared. Holds no preferences and cannot create one."""

    def __init__(
        self,
        *,
        min_evidence_turns: int = MIN_EVIDENCE_TURNS,
        min_confidence: float = MIN_CONFIDENCE,
        now_ms_fn=now_ms,
    ) -> None:
        self.min_evidence_turns = min_evidence_turns
        self.min_confidence = min_confidence
        self._now = now_ms_fn
        self._patterns = _patterns()
        self._evidence: dict[str, dict[str, Evidence]] = defaultdict(dict)
        """signal -> turn_id -> Evidence. The inner dict is what enforces
        "independent occurrences": a second match in the same turn overwrites, never adds."""
        self._asked: set[str] = set()

    # ------------------------------------------------------------------ intake
    def observe(self, block: ContentBlock, *, turn_id: str,
                language: Language) -> tuple[Evidence, ...]:
        """Extract evidence from one block. Non-USER content is ignored, silently and on
        purpose: it is not an error for a web page to contain the words "answer more
        briefly", it just isn't the user saying it."""
        if block.provenance.trust is not TrustLevel.USER:
            return ()
        return self._extract(block.text, turn_id=turn_id, language=language)

    def observe_utterance(self, text: str, *, turn_id: str,
                          language: Language) -> tuple[Evidence, ...]:
        """Convenience for the transcript, which is `TrustLevel.USER` by definition."""
        return self._extract(text, turn_id=turn_id, language=language)

    def _extract(self, text: str, *, turn_id: str,
                 language: Language) -> tuple[Evidence, ...]:
        found: list[Evidence] = []
        for pat in self._patterns:
            if pat.language not in ("any", language.value):
                continue
            m = pat.regex.search(text)
            if not m:
                continue
            hour = m.group(1) if pat.capture_hour and m.groups() else None
            ev = Evidence(
                turn_id=turn_id, signal=pat.signal, domain=_domain_of(pat.signal),
                strength=pat.strength, language=language,
                quote=" ".join(m.group(0).split())[:120], hour=hour, at_ms=self._now(),
            )
            self._evidence[pat.signal][turn_id] = ev
            found.append(ev)
        return tuple(found)

    # ------------------------------------------------------------------ scoring
    def _decay(self, at_ms: int) -> float:
        age_days = max(0.0, (self._now() - at_ms) / 86_400_000)
        return 0.5 ** (age_days / EVIDENCE_HALF_LIFE_DAYS)

    def confidence(self, signal: str) -> float:
        mine = self._evidence.get(signal, {})
        if not mine:
            return 0.0
        score = sum(e.strength * self._decay(e.at_ms) for e in mine.values())
        opposite = self._evidence.get(OPPOSITES.get(signal, ""), {})
        score -= sum(e.strength * self._decay(e.at_ms) for e in opposite.values())
        return max(0.0, min(MAX_CONFIDENCE, score))

    def evidence_for(self, signal: str) -> tuple[Evidence, ...]:
        return tuple(sorted(self._evidence.get(signal, {}).values(), key=lambda e: e.at_ms))

    # ------------------------------------------------------------------ proposing
    def statement(self, signal: str, language: Language, *, hour: str | None = None) -> str:
        entry = prompts.observation_templates()["signal"][signal]
        text = str(entry[language.value])
        return text.format(hour=hour) if "{hour}" in text else text

    def spoken_prompt(self, statement: str, language: Language) -> str:
        return str(
            prompts.observation_templates()["ask"][language.value]["template"]
        ).format(statement=statement)

    def _statement_language(self, evidence: tuple[Evidence, ...]) -> Language:
        """The user's language, not the reply language. If they asked for shorter answers
        in Romanian, the sentence they get read back is Romanian."""
        ro = sum(1 for e in evidence if e.language is Language.RO)
        return Language.RO if ro * 2 >= len(evidence) and ro else Language.EN

    def candidates(self) -> tuple[Observation, ...]:
        """Every observation currently proposable. Does not mark anything as asked."""
        out: list[Observation] = []
        for signal in sorted(self._evidence):
            if signal in self._asked:
                continue
            evidence = self.evidence_for(signal)
            turns = tuple(dict.fromkeys(e.turn_id for e in evidence))
            if len(turns) < self.min_evidence_turns:
                continue
            conf = self.confidence(signal)
            if conf < self.min_confidence:
                continue
            language = self._statement_language(evidence)
            hour = next((e.hour for e in reversed(evidence) if e.hour), None)
            obs = Observation(
                domain=_domain_of(signal),
                statement=self.statement(signal, language, hour=hour),
                confidence=conf, evidence_turn_ids=turns,
                status=ObservationStatus.PROPOSED,
            )
            if obs.may_ask:  # the protocol's own gate, re-checked rather than assumed
                out.append(obs)
        return tuple(out)

    def propose(self, *, turn_id: str, text: str,
                language: Language) -> list[tuple[Observation, str]]:
        """Ingest this turn's utterance, then return anything ready to ask about.

        At most one proposal per turn. Being asked two behavioural questions in a row is
        how an assistant becomes exhausting, which `memory.py` calls out by name.
        """
        self.observe_utterance(text, turn_id=turn_id, language=language)
        candidates = self.candidates()
        if not candidates:
            return []
        best = max(candidates, key=lambda o: o.confidence)
        signal = self._signal_of(best)
        if signal:
            self._asked.add(signal)
        lang = self._statement_language(self.evidence_for(signal)) if signal else language
        return [(best, self.spoken_prompt(best.statement, lang))]

    def _signal_of(self, observation: Observation) -> str:
        for signal in self._evidence:
            for lang in SUPPORTED_LANGUAGES:
                template = prompts.observation_templates()["signal"][signal][lang.value]
                if "{hour}" in template:
                    prefix = template.split("{hour}")[0]
                    if prefix and observation.statement.startswith(prefix):
                        return signal
                elif template == observation.statement:
                    return signal
        return ""

    # ------------------------------------------------------------------ promotion
    async def confirm(self, store, observation_id: str, confirmed: bool) -> Preference | None:
        """The ONLY route from observation to preference, and it is a pass-through.

        This method deliberately contains no logic: the decision was the user's, the
        durable write belongs to the memory service, and putting a shortcut here is
        exactly the mistake `memory.py` warns about — "a wrong preference learned in
        silence is nearly impossible for the user to find and correct".
        """
        return await store.confirm_observation(observation_id, confirmed)

    def reset(self) -> None:
        self._evidence.clear()
        self._asked.clear()


__all__ = [
    "EVIDENCE_HALF_LIFE_DAYS",
    "MAX_CONFIDENCE",
    "MIN_CONFIDENCE",
    "MIN_EVIDENCE_TURNS",
    "OPPOSITES",
    "BehaviourLearner",
    "Evidence",
]
