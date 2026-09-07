"""The voice catalogue: named voices a caller picks by name, not by speaker id.

`SynthesisRequest.voice` is a string. Before this file it was ignored entirely; the engine
only ever knew one voice per language. This is the list of names that string may take, and
what each one resolves to on disk.

Four of the models below are **multi-speaker**: one `.onnx` containing many voices, selected
with `SynthesisConfig.speaker_id`. That is what makes "many more voices" a 355 MB download
instead of a 1.5 GB one — `en_GB-vctk-medium` alone carries 109 speakers in 77 MB. The
curated names here are a roster, not a limit: `voices_in(model)` enumerates every speaker a
model has, and the `model#speaker` spec syntax reaches any of them.

**Every `median_f0_hz` below was measured**, not read off a model card: one sentence per
voice, autocorrelation pitch track over voiced frames (40 ms window, 10 ms hop, RMS gate
0.02, correlation gate 0.35), median. It is a *proxy* for gender and it is wrong sometimes —
`de_DE-thorsten_emotional` `angry` measures 219 Hz and is the same male speaker as `neutral`
at 126 Hz, and `whisper` has no periodic excitation at all, so its number is noise. Where
the proxy and the truth disagree, `gender` follows the truth and the number stays visible.

Nobody has listened to these yet. `research/voices.md` §8 names that as the largest open
risk in the whole plan: the multi-speaker recommendation rests on perceptual quality that
was never assessed. Renders are in `var/voice-auditions/` (gitignored).

Two hard constraints are encoded here rather than hidden:

* **Romanian has exactly one Piper voice in existence and it is male.** There is no female
  Romanian voice to select, and this catalogue does not invent one by quietly substituting
  English. `ROMANIAN_LIMITATION` says so in all three languages, and the two derived
  Romanian entries are labelled as what they are: `mihai` pitch- and formant-shifted, which
  produces a different-sized speaker, not a woman.
* **No `high`-quality voice appears here.** Measured 395-436 ms to first audio against a
  120 ms budget. `tests/unit/voice/test_voice_catalogue.py` asserts the absence, because the
  tempting thing to do with a catalogue is add the nicest-sounding row to it.

Every user-facing string exists in EN, RO and DE (CLAUDE.md #5). They are assembled from a
closed tag vocabulary rather than written out per voice, so a new voice cannot ship with a
missing translation — the tag either exists in all three languages or `describe()` raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ars_protocol import SUPPORTED_LANGUAGES, Language


class VoiceGender(StrEnum):
    """What the voice sounds like, for a picker to group by.

    `NEUTRAL` is not "unknown": it is used where the speaker is deliberately non-binary
    (`en_US-sam`) or where the voice is derived and lands between registers. A voice whose
    gender we genuinely could not establish is not in the roster.
    """

    FEMALE = "female"
    MALE = "male"
    NEUTRAL = "neutral"


NON_COMMERCIAL_LICENCES = frozenset({"CC BY-NC-SA 4.0", "CC BY-NC 4.0"})
"""Fine on the owner's own machine, a blocker if A.R.S is ever distributed with these
bundled. `semaine` (four purpose-built characters) and `hfc_female` are the casualties of
that day; flagged here so the day does not arrive as a surprise."""


@dataclass(frozen=True)
class VoiceProfile:
    """One selectable voice: a model file, optionally a speaker inside it, optionally a
    pitch/formant shift."""

    name: str
    """The stable id a caller puts in `SynthesisRequest.voice`. Never a speaker number."""

    language: Language
    model: str
    """Piper model stem, i.e. `models/tts/<model>.onnx`."""

    gender: VoiceGender
    median_f0_hz: float | None
    """Measured, on the sentence in `_PRIMING_SENTENCE` for this language. `None` where the
    measurement is meaningless (whispered speech has no F0 to track)."""

    licence: str
    speaker: str | None = None
    """Key into the model's `speaker_id_map`, for multi-speaker models."""

    speaker_id: int | None = None
    """The id that key maps to. Recorded so selection costs no file read, and asserted
    against the model's own sidecar in the tests — a model re-release that renumbers its
    speakers would otherwise silently change who is talking."""

    formant_k: float = 1.0
    """Pitch *and* formant scale, free of charge: the backend already resamples 22.05 kHz down
    to the protocol rate, so claiming the source is `22050·k` shifts pitch and formants and
    costs nothing. Unlike a naive pitch shift this sounds like a physically different-sized
    speaker. Duration is compensated with `length_scale = k`, which is only good to ~10%."""

    accent: str | None = None
    tags: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    """Ids into `CAVEATS`. User-facing, in all three languages: this is where "there is no
    female Romanian voice" gets said out loud instead of being papered over."""

    @property
    def is_derived(self) -> bool:
        return self.formant_k != 1.0


# --------------------------------------------------------------------------- vocabulary

_GENDER_WORDS: dict[VoiceGender, dict[Language, str]] = {
    VoiceGender.FEMALE: {
        Language.EN: "Female voice", Language.RO: "Voce feminină",
        Language.DE: "Weibliche Stimme",
    },
    VoiceGender.MALE: {
        Language.EN: "Male voice", Language.RO: "Voce masculină",
        Language.DE: "Männliche Stimme",
    },
    VoiceGender.NEUTRAL: {
        Language.EN: "Neutral voice", Language.RO: "Voce neutră",
        Language.DE: "Neutrale Stimme",
    },
}

_ACCENTS: dict[str, dict[Language, str]] = {
    "british": {Language.EN: "British", Language.RO: "britanică", Language.DE: "britisch"},
    "scottish": {Language.EN: "Scottish", Language.RO: "scoțiană", Language.DE: "schottisch"},
    "american": {Language.EN: "American", Language.RO: "americană", Language.DE: "amerikanisch"},
    "indian": {
        Language.EN: "Indian English", Language.RO: "cu accent indian",
        Language.DE: "indisches Englisch",
    },
    "german": {Language.EN: "German", Language.RO: "germană", Language.DE: "deutsch"},
    "romanian": {Language.EN: "Romanian", Language.RO: "românească", Language.DE: "rumänisch"},
}

# Adjectives agree with "voce" (RO) and "Stimme" (DE), both feminine, so the speaker's own
# gender never changes the form. That is the whole reason the template reads
# "<gender> voice, <accent> — <tags>" in all three languages instead of three templates.
_TAGS: dict[str, dict[Language, str]] = {
    "everyday": {Language.EN: "everyday", Language.RO: "de zi cu zi", Language.DE: "alltäglich"},
    "deep": {Language.EN: "deep", Language.RO: "gravă", Language.DE: "tief"},
    "bright": {Language.EN: "bright", Language.RO: "luminoasă", Language.DE: "hell"},
    "calm": {Language.EN: "calm", Language.RO: "calmă", Language.DE: "ruhig"},
    "warm": {Language.EN: "warm", Language.RO: "caldă", Language.DE: "warm"},
    "clear": {Language.EN: "clear", Language.RO: "clară", Language.DE: "klar"},
    "gloomy": {Language.EN: "gloomy", Language.RO: "posomorâtă", Language.DE: "düster"},
    "aggressive": {
        Language.EN: "aggressive", Language.RO: "agresivă", Language.DE: "aggressiv",
    },
    "young": {Language.EN: "young", Language.RO: "tinerească", Language.DE: "jugendlich"},
    "whispering": {
        Language.EN: "whispering", Language.RO: "șoptită", Language.DE: "flüsternd",
    },
    "sleepy": {Language.EN: "sleepy", Language.RO: "somnoroasă", Language.DE: "schläfrig"},
    "angry": {Language.EN: "angry", Language.RO: "furioasă", Language.DE: "wütend"},
    "by_design_neutral": {
        Language.EN: "deliberately non-binary",
        Language.RO: "intenționat neutră ca gen",
        Language.DE: "bewusst geschlechtsneutral",
    },
    "pitch_shifted": {
        Language.EN: "pitch- and formant-shifted",
        Language.RO: "cu tonul și formanții modificați",
        Language.DE: "in Tonhöhe und Formanten verschoben",
    },
    "narrator": {Language.EN: "narrator", Language.RO: "de narator", Language.DE: "erzählend"},
    "night_mode": {
        Language.EN: "for night mode", Language.RO: "pentru modul de noapte",
        Language.DE: "für den Nachtmodus",
    },
}

CAVEATS: dict[str, dict[Language, str]] = {
    "ro_only_male": {
        Language.EN: (
            "Piper has exactly one Romanian voice and it is male. There is no female "
            "Romanian voice to offer, and A.R.S will not substitute an English one."
        ),
        Language.RO: (
            "Piper are exact o singură voce românească și este masculină. Nu există o voce "
            "feminină românească de oferit, iar A.R.S nu o înlocuiește pe ascuns cu una engleză."
        ),
        Language.DE: (
            "Piper hat genau eine rumänische Stimme, und sie ist männlich. Eine weibliche "
            "rumänische Stimme gibt es nicht, und A.R.S ersetzt sie nicht heimlich durch eine "
            "englische."
        ),
    },
    "ro_derived_not_female": {
        Language.EN: (
            "Derived from the one Romanian voice by shifting pitch and formants together. "
            "It is a different-sized speaker, not a woman — no amount of shifting makes a "
            "female Romanian voice out of a male one."
        ),
        Language.RO: (
            "Derivată din singura voce românească, prin deplasarea tonului și a formanților. "
            "Este un vorbitor de altă statură, nu o femeie — nicio deplasare nu transformă o "
            "voce masculină într-una feminină."
        ),
        Language.DE: (
            "Aus der einzigen rumänischen Stimme abgeleitet, indem Tonhöhe und Formanten "
            "gemeinsam verschoben wurden. Das ergibt eine anders gebaute Sprecherin oder "
            "Sprecher, keine Frau."
        ),
    },
    "same_speaker_other_emotion": {
        Language.EN: "The same speaker as the German default, recorded in another emotion.",
        Language.RO: "Același vorbitor ca vocea germană implicită, înregistrat în altă emoție.",
        Language.DE: "Derselbe Sprecher wie die deutsche Standardstimme, in einer anderen Emotion.",
    },
    "non_commercial": {
        Language.EN: "Licensed for personal use only; not for a commercial release.",
        Language.RO: "Licențiată doar pentru uz personal; nu pentru o lansare comercială.",
        Language.DE: "Nur für den privaten Gebrauch lizenziert, nicht für eine kommerzielle "
        "Veröffentlichung.",
    },
}

ROMANIAN_LIMITATION: dict[Language, str] = CAVEATS["ro_only_male"]
"""Exported on its own because a voice picker has to show it next to the Romanian section,
not bury it in one voice's tooltip."""


# --------------------------------------------------------------------------- the roster

_EN = Language.EN
_RO = Language.RO
_DE = Language.DE
_F = VoiceGender.FEMALE
_M = VoiceGender.MALE
_N = VoiceGender.NEUTRAL

VOICES: tuple[VoiceProfile, ...] = (
    # ---------------------------------------------------------------- English, female
    VoiceProfile("amy", _EN, "en_US-amy-medium", _F, 195.1, "see MODEL_CARD",
                 accent="american", tags=("everyday", "clear")),
    VoiceProfile("lessac", _EN, "en_US-lessac-medium", _F, 210.5, "see MODEL_CARD",
                 accent="american", tags=("clear", "narrator")),
    VoiceProfile("heather", _EN, "en_US-hfc_female-medium", _F, 254.0, "CC BY-NC-SA 4.0",
                 accent="american", tags=("bright", "young"), caveats=("non_commercial",)),
    VoiceProfile("clara", _EN, "en_US-arctic-medium", _F, 188.2, "see LICENSE",
                 speaker="clb", speaker_id=4, accent="american", tags=("warm", "clear")),
    VoiceProfile("asha", _EN, "en_US-arctic-medium", _F, 228.6, "see LICENSE",
                 speaker="axb", speaker_id=15, accent="indian", tags=("bright", "clear")),
    VoiceProfile("alba", _EN, "en_GB-alba-medium", _F, 213.3, "see MODEL_CARD",
                 accent="scottish", tags=("warm",)),
    VoiceProfile("jenny", _EN, "en_GB-jenny_dioco-medium", _F, 192.8, "see URL",
                 accent="british", tags=("calm", "clear")),
    VoiceProfile("iris", _EN, "en_GB-vctk-medium", _F, 197.5, "CC BY 4.0",
                 speaker="p300", speaker_id=72, accent="british", tags=("calm",)),
    VoiceProfile("esme", _EN, "en_GB-vctk-medium", _F, 210.5, "CC BY 4.0",
                 speaker="p323", speaker_id=31, accent="british", tags=("warm",)),
    VoiceProfile("wren", _EN, "en_GB-vctk-medium", _F, 246.2, "CC BY 4.0",
                 speaker="p317", speaker_id=36, accent="british", tags=("bright", "young")),
    VoiceProfile("prudence", _EN, "en_GB-semaine-medium", _F, 222.2, "CC BY-NC-SA 4.0",
                 speaker="prudence", speaker_id=0, accent="british", tags=("calm", "clear"),
                 caveats=("non_commercial",)),
    VoiceProfile("poppy", _EN, "en_GB-semaine-medium", _F, 262.3, "CC BY-NC-SA 4.0",
                 speaker="poppy", speaker_id=3, accent="british", tags=("bright", "young"),
                 caveats=("non_commercial",)),
    # ---------------------------------------------------------------- English, male
    VoiceProfile("alan", _EN, "en_GB-alan-medium", _M, 90.4, "see URL",
                 accent="british", tags=("deep", "calm")),
    VoiceProfile("joe", _EN, "en_US-joe-medium", _M, 97.0, "CC0",
                 accent="american", tags=("deep", "everyday")),
    VoiceProfile("angus", _EN, "en_US-arctic-medium", _M, 144.1, "see LICENSE",
                 speaker="awb", speaker_id=0, accent="scottish", tags=("clear",)),
    VoiceProfile("ravi", _EN, "en_US-arctic-medium", _M, 135.6, "see LICENSE",
                 speaker="ksp", speaker_id=3, accent="indian", tags=("clear",)),
    VoiceProfile("ivor", _EN, "en_GB-vctk-medium", _M, 79.6, "CC BY 4.0",
                 speaker="p254", speaker_id=76, accent="british", tags=("deep",)),
    VoiceProfile("gareth", _EN, "en_GB-vctk-medium", _M, 101.3, "CC BY 4.0",
                 speaker="p270", speaker_id=12, accent="british", tags=("deep", "calm")),
    VoiceProfile("rufus", _EN, "en_GB-vctk-medium", _M, 115.9, "CC BY 4.0",
                 speaker="p259", speaker_id=4, accent="british", tags=("warm",)),
    VoiceProfile("spike", _EN, "en_GB-semaine-medium", _M, 89.4, "CC BY-NC-SA 4.0",
                 speaker="spike", speaker_id=1, accent="british", tags=("deep", "aggressive"),
                 caveats=("non_commercial",)),
    VoiceProfile("obadiah", _EN, "en_GB-semaine-medium", _M, 102.6, "CC BY-NC-SA 4.0",
                 speaker="obadiah", speaker_id=2, accent="british", tags=("deep", "gloomy"),
                 caveats=("non_commercial",)),
    # ---------------------------------------------------------------- English, neutral
    VoiceProfile("sam", _EN, "en_US-sam-medium", _N, 144.1, "Apache-2.0",
                 accent="american", tags=("by_design_neutral", "calm")),
    # ---------------------------------------------------------------- Romanian
    # One real voice. Everything else in this block is derived from it and says so.
    VoiceProfile("mihai", _RO, "ro_RO-mihai-medium", _M, 122.1, "CC0",
                 accent="romanian", tags=("everyday", "clear"), caveats=("ro_only_male",)),
    VoiceProfile("mihai_deep", _RO, "ro_RO-mihai-medium", _M, 103.9, "CC0",
                 formant_k=0.85, accent="romanian", tags=("deep", "pitch_shifted"),
                 caveats=("ro_only_male",)),
    VoiceProfile("pup_ro", _RO, "ro_RO-mihai-medium", _N, None, "CC0",
                 formant_k=1.38, accent="romanian", tags=("young", "bright", "pitch_shifted"),
                 caveats=("ro_only_male",)),
    VoiceProfile("mihai_light", _RO, "ro_RO-mihai-medium", _N, 156.9, "CC0",
                 formant_k=1.30, accent="romanian", tags=("young", "pitch_shifted"),
                 caveats=("ro_only_male", "ro_derived_not_female")),
    # A small, young-sounding speaker in each language, for a child. Built by shifting a real
    # voice up rather than by imitating anyone: `formant_k` scales pitch AND formants, so
    # the result is a physically smaller speaker rather than a chipmunk. Named `pup_*` and
    # not after any character — this is an original voice, and it is not, and does not
    # claim to be, a performance anyone recorded.
    VoiceProfile("pup_en", _EN, "en_US-joe-medium", _N, None, "CC0",
                 formant_k=1.34, accent="american", tags=("young", "bright", "pitch_shifted")),
    # ---------------------------------------------------------------- German
    VoiceProfile("thorsten", _DE, "de_DE-thorsten-medium", _M, 126.0, "CC0",
                 accent="german", tags=("everyday", "clear")),
    VoiceProfile("kerstin", _DE, "de_DE-kerstin-low", _F, 164.9, "CC0",
                 accent="german", tags=("calm", "warm")),
    VoiceProfile("ramona", _DE, "de_DE-ramona-low", _F, 188.2, "see URL",
                 accent="german", tags=("bright", "clear")),
    VoiceProfile("pup_de", _DE, "de_DE-thorsten-medium", _N, None, "CC0",
                 formant_k=1.34, accent="german", tags=("young", "bright", "pitch_shifted")),
    VoiceProfile("thorsten_whisper", _DE, "de_DE-thorsten_emotional-medium", _M, None, "CC0",
                 speaker="whisper", speaker_id=7, accent="german",
                 tags=("whispering", "night_mode"), caveats=("same_speaker_other_emotion",)),
    VoiceProfile("thorsten_sleepy", _DE, "de_DE-thorsten_emotional-medium", _M, 119.4, "CC0",
                 speaker="sleepy", speaker_id=5, accent="german", tags=("sleepy", "calm"),
                 caveats=("same_speaker_other_emotion",)),
    VoiceProfile("thorsten_angry", _DE, "de_DE-thorsten_emotional-medium", _M, 219.2, "CC0",
                 speaker="angry", speaker_id=1, accent="german", tags=("angry", "aggressive"),
                 caveats=("same_speaker_other_emotion",)),
)
"""The named roster. Curated, not exhaustive: `en_GB-vctk-medium` has 109 speakers and
`en_US-arctic-medium` 18, all reachable as `model#speaker` without appearing here."""

VOICES_BY_NAME: dict[str, VoiceProfile] = {v.name: v for v in VOICES}


# --------------------------------------------------------------------------- characters

@dataclass(frozen=True)
class CharacterProfile:
    """A DSP character. Language-independent by construction — it is applied to whatever
    voice is already speaking, so `robot_dalek` exists in Romanian on the day it exists in
    English. That property is why this is DSP and not another model: nothing trained on
    robot speech exists for Romanian, and nothing will."""

    name: str
    kind: str
    """`robot` or `alien`. What a picker groups by."""

    description: dict[Language, str]
    ms_per_chunk: float
    """Measured median cost per 120 ms chunk on an M5 Max. The whole register costs less
    than 1.1 ms; the budget is 120 ms."""


CHARACTERS: dict[str, CharacterProfile] = {
    "robot_ring": CharacterProfile(
        "robot_ring", "robot",
        {
            _EN: "Robot — clean computer voice, ring-modulated and bit-crushed",
            _RO: "Robot — voce curată de calculator, modulată în inel și cuantizată",
            _DE: "Roboter — saubere Computerstimme, ringmoduliert und bit-reduziert",
        },
        0.122,
    ),
    "robot_dalek": CharacterProfile(
        "robot_dalek", "robot",
        {
            _EN: "Robot — buzzing, metallic and hostile",
            _RO: "Robot — bâzâitoare, metalică și ostilă",
            _DE: "Roboter — brummend, metallisch und feindselig",
        },
        0.083,
    ),
    "robot_vocoder": CharacterProfile(
        "robot_vocoder", "robot",
        {
            _EN: "Robot — monotone vocoder, the classic machine voice",
            _RO: "Robot — vocoder monoton, vocea clasică de mașină",
            _DE: "Roboter — monotoner Vocoder, die klassische Maschinenstimme",
        },
        1.009,
    ),
    "pup": CharacterProfile(
        "pup", "companion",
        {
            _EN: "Pup — a small, eager rescue-dog voice for a child",
            _RO: "Cățel — o voce mică și entuziastă de câine salvator, pentru un copil",
            _DE: "Welpe — eine kleine, eifrige Rettungshund-Stimme für ein Kind",
        },
        0.09,
    ),
    "alien_ring": CharacterProfile(
        "alien_ring", "alien",
        {
            _EN: "Alien — inharmonic and detuned, one strange speaker",
            _RO: "Extraterestru — nearmonică și dezacordată, un singur vorbitor straniu",
            _DE: "Außerirdisch — unharmonisch und verstimmt, eine fremde Stimme",
        },
        0.341,
    ),
    "alien_swarm": CharacterProfile(
        "alien_swarm", "alien",
        {
            _EN: "Alien — a hive, several of it talking at once",
            _RO: "Extraterestru — un roi, mai multe voci deodată",
            _DE: "Außerirdisch — ein Schwarm, mehrere Stimmen zugleich",
        },
        0.159,
    ),
}

ALIASES: dict[str, str] = {
    "robot": "robot_ring",
    "dalek": "robot_dalek",
    "vocoder": "robot_vocoder",
    "alien": "alien_ring",
    "swarm": "alien_swarm",
}
"""What a person actually says. "Speak like a robot" should not require knowing which of
three robots is the default one."""


# --------------------------------------------------------------------------- spec parsing

CHARACTER_SEPARATOR = "/"
SPEAKER_SEPARATOR = "#"


@dataclass(frozen=True)
class VoiceSpec:
    """A parsed `SynthesisRequest.voice`.

    Accepted forms, all of them a single string because that is what the protocol field is:

    * `""` / `None`      — the configured default for the request's language
    * `alan`             — a catalogue name
    * `en_US-amy-medium` — a raw Piper model, for config values and backwards compatibility
    * `en_GB-vctk-medium#p300` — any speaker of any multi-speaker model, catalogued or not
    * `robot_dalek`      — a character over the language's default voice, so it works in
      Romanian on day one
    * `robot_dalek/alan` — a character over a specific voice
    """

    voice: str | None = None
    character: str | None = None

    def __str__(self) -> str:
        if self.character and self.voice:
            return f"{self.character}{CHARACTER_SEPARATOR}{self.voice}"
        return self.character or self.voice or ""


def parse_voice_spec(spec: str | None) -> VoiceSpec:
    """Split a voice string into (voice, character). Never raises on an unknown *voice* —
    resolution is the engine's job and it needs the name to put in the error message."""
    text = (spec or "").strip()
    if not text:
        return VoiceSpec()
    if CHARACTER_SEPARATOR in text:
        head, _, tail = text.partition(CHARACTER_SEPARATOR)
        return VoiceSpec(voice=tail.strip() or None, character=_character_name(head.strip()))
    character = _character_name(text, strict=False)
    if character is not None:
        return VoiceSpec(character=character)
    return VoiceSpec(voice=text)


def _character_name(name: str, *, strict: bool = True) -> str | None:
    resolved = ALIASES.get(name.lower(), name.lower())
    if resolved in CHARACTERS:
        return resolved
    if strict:
        raise ValueError(
            f"unknown character {name!r} (have {sorted(CHARACTERS)} "
            f"or aliases {sorted(ALIASES)})"
        )
    return None


# --------------------------------------------------------------------------- queries

def voices(
    *,
    language: Language | None = None,
    gender: VoiceGender | None = None,
    include_derived: bool = True,
    commercial_only: bool = False,
) -> list[VoiceProfile]:
    """The catalogue, filtered. This is what a picker enumerates."""
    out = list(VOICES)
    if language is not None:
        out = [v for v in out if v.language is language]
    if gender is not None:
        out = [v for v in out if v.gender is gender]
    if not include_derived:
        out = [v for v in out if not v.is_derived]
    if commercial_only:
        out = [v for v in out if v.licence not in NON_COMMERCIAL_LICENCES]
    return out


def describe(profile: VoiceProfile, language: Language) -> str:
    """A one-line label, in the language of whoever is reading it — which is not the
    language the voice speaks. A Romanian speaker browsing the German voices reads Romanian.
    """
    parts = [_GENDER_WORDS[profile.gender][language]]
    if profile.accent:
        parts.append(_ACCENTS[profile.accent][language])
    head = ", ".join(parts)
    if not profile.tags:
        return head
    return f"{head} — {', '.join(_TAGS[tag][language] for tag in profile.tags)}"


def caveats_for(profile: VoiceProfile, language: Language) -> list[str]:
    """The things that must be said out loud about this voice, in `language`. Empty for
    most voices; never empty for Romanian."""
    return [CAVEATS[c][language] for c in profile.caveats]


def describe_character(name: str, language: Language) -> str:
    resolved = ALIASES.get(name.lower(), name.lower())
    return CHARACTERS[resolved].description[language]


def missing_translations() -> list[str]:
    """Every user-facing string in this module, checked against `SUPPORTED_LANGUAGES`.

    A list, not an exception: the test wants to name all of them at once, and CLAUDE.md #5
    is a completeness property, not a per-call one.
    """
    gaps: list[str] = []
    tables: list[tuple[str, dict[str, dict[Language, str]]]] = [
        ("gender", {g.value: table for g, table in _GENDER_WORDS.items()}),
        ("accent", _ACCENTS),
        ("tag", _TAGS),
        ("caveat", CAVEATS),
        ("character", {n: c.description for n, c in CHARACTERS.items()}),
    ]
    for kind, table in tables:
        for key, translations in table.items():
            for language in SUPPORTED_LANGUAGES:
                if not translations.get(language):
                    gaps.append(f"{kind}:{key}:{language.value}")
    return gaps


__all__ = [
    "ALIASES",
    "CAVEATS",
    "CHARACTERS",
    "CHARACTER_SEPARATOR",
    "NON_COMMERCIAL_LICENCES",
    "ROMANIAN_LIMITATION",
    "SPEAKER_SEPARATOR",
    "VOICES",
    "VOICES_BY_NAME",
    "CharacterProfile",
    "VoiceGender",
    "VoiceProfile",
    "VoiceSpec",
    "caveats_for",
    "describe",
    "describe_character",
    "missing_translations",
    "parse_voice_spec",
    "voices",
]
