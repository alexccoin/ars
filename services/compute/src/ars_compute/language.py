"""Which language A.R.S answers in, and whether the Romanian it produces is real Romanian.

Two problems live here, and they are not the same problem.

**1. Choosing the reply language.** The rule is one line: reply in the language of the
user's most recent utterance, per utterance, following `Transcript.language`. The
complication is that `Transcript.language` comes from an ASR language classifier, and
those classifiers are systematically wrong in exactly one direction for this household:
a Romanian sentence carrying English technical nouns ("Poți să faci un rebase pe branch-ul
de staging?") frequently comes back tagged `en` with mediocre confidence. Following the
transcript blindly there produces the single most annoying failure mode a bilingual
assistant has — being answered in English because you said the word "branch".

So the transcript is the primary signal, and a *matrix-language* detector is the check on
it. The detector deliberately ignores borrowed technical vocabulary and weights Romanian
function words, diacritics, and Romanian enclitic morphology on foreign stems
("branch-ul", "commit-uri") — that morphology is unambiguous evidence of a Romanian
sentence, because English does not do that to its nouns.

**2. Diacritics.** Romanian without ă â î ș ț is not a stylistic choice, it is broken
text, and TTS mispronounces it. Local models quantised to 4 bits drop diacritics under
load. The prompt asks for them; this module *verifies* and, on a deliberately small and
unambiguous closed vocabulary, repairs them. It never touches a word where the
undiacriticked form is also a real word ("sa"/"să", "ca"/"că", "tine"/"ține"), never
touches anything that looks like code or a path, and never runs when the reply language
is English.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from ars_protocol import Language, Session, Transcript

# --------------------------------------------------------------------------- vocabularies

RO_DIACRITICS = "ăâîșțĂÂÎȘȚ"
_RO_DIACRITIC_RE = re.compile(f"[{RO_DIACRITICS}]")

RO_FUNCTION_WORDS: frozenset[str] = frozenset("""
si sa la de pe cu un o pentru care este sunt nu mai daca dar din ca ce cum cand unde
imi iti isi mie tie lui ei noi voi eu tu el ea ne va le mi ti ii
am ai are avem aveti au aveam aveau fost fi fie face fac faci facem
vreau vrei vrea vrem vreti vor poti pot poate putem puteti trebuie
asta aceasta acesta astea acestia aia ala acolo aici acum atunci
foarte prea putin mult mai destul cam chiar doar numai tot toate toti
te-am mi-a mi-am ti-am s-a n-am nu-i
adica deci insa totusi asadar oricum
buna salut multumesc merci scuze te rog va rog
""".split())

EN_FUNCTION_WORDS: frozenset[str] = frozenset("""
the a an and or but if then than that this these those there here
is are was were be been being am do does did doing done
i you he she it we they me him her us them my your his its our their
of to in on at for with from by about into over after before
what when where why how which who whom whose
can could will would shall should may might must
not no yes please thanks thank sorry
""".split())

TECHNICAL_BORROWINGS: frozenset[str] = frozenset("""
api backend frontend endpoint endpoints server servers client clients database db
query queries cache caching log logs logging debug bug bugs fix patch hotfix
deploy deployment deployments release releases rollback staging production prod dev
commit commits merge merged rebase branch branches pull push request pr prs repo
repository repositories issue issues ticket tickets sprint standup backlog
build builds pipeline ci cd docker container containers kubernetes k8s cluster
script scripts framework library package packages module dependency dependencies
test tests testing mock mocks fixture fixtures coverage lint linter
laptop desktop phone browser tab tabs screenshot dashboard
email mail inbox spam folder file files link links url
token tokens login logout password auth oauth session cookie
meeting call zoom slack calendar deadline feedback review update upgrade
download upload stream streaming buffer thread threads timeout retry crash restart
model models prompt prompts dataset training inference embedding embeddings llm
wifi bluetooth vpn dns ssh git github gitlab
ok okay online offline default setup config feature features bump
""".split())
"""English words that appear inside ordinary Romanian speech and must NOT count as
evidence of an English sentence. This list is the difference between an assistant that
follows the household's actual speech and one that keeps flipping languages. It is
deliberately generous: a false entry here costs nothing (the word is simply ignored),
a missing entry costs a wrong-language reply."""

_RO_ENCLITIC_RE = re.compile(
    r"\b[a-z][\w']*-(?:ul|ului|urile|urilor|uri|le|lui|lor|a|ei)\b", re.IGNORECASE
)
"""`branch-ul`, `commit-uri`, `link-ului`. Romanian glues its definite article onto
foreign stems with a hyphen. English never does this, so a match is near-proof of a
Romanian matrix sentence even when every content word is English."""

_WORD_RE = re.compile(r"[\w'ăâîșțĂÂÎȘȚ-]+", re.UNICODE)


# --------------------------------------------------------------------------- detection

class LanguageSource(StrEnum):
    SESSION_PIN = "session_pin"
    """The session declared `preferred_language`. Explicit user configuration wins."""
    TRANSCRIPT = "transcript"
    """ASR's own label, confident enough to take at face value."""
    MATRIX_OVERRIDE = "matrix_override"
    """ASR disagreed with the text. Almost always a code-switched Romanian utterance
    that got tagged `en`."""
    LOW_ASR_CONFIDENCE = "low_asr_confidence"
    TEXT_INPUT = "text_input"
    """Typed, no ASR label at all."""
    DEFAULT = "default"


@dataclass(frozen=True)
class MatrixEvidence:
    """What the text itself says about which language it is in."""

    language: Language
    ro_score: float
    en_score: float
    ro_hits: tuple[str, ...]
    en_hits: tuple[str, ...]
    borrowings: tuple[str, ...]
    has_diacritics: bool
    enclitics: tuple[str, ...]

    @property
    def margin(self) -> float:
        return abs(self.ro_score - self.en_score)

    @property
    def decisive(self) -> bool:
        """High enough to overrule an ASR label. Diacritics or Romanian enclitic
        morphology are on their own sufficient; otherwise we want a clear function-word
        margin, not a coin flip."""
        if self.has_diacritics or self.enclitics:
            return True
        return self.margin >= 2.0 and max(self.ro_score, self.en_score) >= 3.0

    @property
    def code_switched(self) -> bool:
        """Romanian matrix carrying English technical vocabulary. Not an error, not a
        language switch, and never to be 'corrected'."""
        return self.language is Language.RO and bool(self.borrowings)


def _fold(word: str) -> str:
    """ASCII-fold for matching: `să` and `sa` are the same lookup key, because ASR and
    keyboards produce both."""
    return "".join(
        c for c in unicodedata.normalize("NFD", word.lower()) if unicodedata.category(c) != "Mn"
    ).replace("ș", "s").replace("ț", "t")


def detect_matrix_language(text: str) -> MatrixEvidence:
    """Decide the *matrix* language of an utterance from the text alone.

    Scoring, in order of weight:
      * a Romanian diacritic anywhere      -> +3.0 Romanian (nothing else produces these)
      * a Romanian enclitic on any stem    -> +2.5 Romanian each (max 3 counted)
      * a Romanian function word           -> +1.0 each
      * an English function word           -> +1.0 each
      * a technical borrowing              -> 0. Ignored entirely, in both directions.
    """
    words = [w for w in _WORD_RE.findall(text)]
    folded = [_fold(w) for w in words]

    borrowings = tuple(w for w, f in zip(words, folded, strict=True) if f in TECHNICAL_BORROWINGS)
    ro_hits = tuple(
        w for w, f in zip(words, folded, strict=True)
        if f in RO_FUNCTION_WORDS and f not in TECHNICAL_BORROWINGS
    )
    en_hits = tuple(
        w for w, f in zip(words, folded, strict=True)
        if f in EN_FUNCTION_WORDS and f not in TECHNICAL_BORROWINGS and f not in RO_FUNCTION_WORDS
    )
    enclitics = tuple(m.group(0) for m in _RO_ENCLITIC_RE.finditer(text))[:3]
    has_diacritics = bool(_RO_DIACRITIC_RE.search(text))

    ro_score = float(len(ro_hits)) + (3.0 if has_diacritics else 0.0) + 2.5 * len(enclitics)
    en_score = float(len(en_hits))

    # Ties go to English only because DEFAULT_LANGUAGE is English; a tie means we have no
    # evidence, and in that state the transcript label is what actually decides.
    language = Language.RO if ro_score > en_score else Language.EN
    return MatrixEvidence(
        language=language, ro_score=ro_score, en_score=en_score, ro_hits=ro_hits,
        en_hits=en_hits, borrowings=borrowings, has_diacritics=has_diacritics,
        enclitics=enclitics,
    )


@dataclass(frozen=True)
class ReplyLanguage:
    language: Language
    source: LanguageSource
    evidence: MatrixEvidence
    asr_language: Language | None
    asr_confidence: float

    @property
    def overrode_asr(self) -> bool:
        return self.asr_language is not None and self.asr_language is not self.language


ASR_TRUST_FLOOR = 0.75
"""Below this, the ASR language label is treated as a hint rather than an answer.
faster-whisper's language probability is well calibrated above ~0.8 and close to useless
below ~0.6 on short bilingual utterances; 0.75 is the conservative midpoint."""


def resolve_reply_language(
    transcript: Transcript,
    *,
    session: Session | None = None,
    typed: bool = False,
) -> ReplyLanguage:
    """The whole per-utterance language rule, in one place, with its reason recorded.

    `session.preferred_language is None` means "follow each utterance", which the protocol
    calls the default and "the behaviour a bilingual household actually wants". A pin is
    honoured because it is the user's explicit configuration, not a guess.
    """
    evidence = detect_matrix_language(transcript.text)
    asr_lang = transcript.language
    conf = transcript.language_confidence

    if session is not None and session.preferred_language is not None:
        return ReplyLanguage(session.preferred_language, LanguageSource.SESSION_PIN,
                             evidence, asr_lang, conf)

    if typed:
        # Typed input has no acoustic language ID worth trusting; the client may have sent
        # a default. The text is the only real evidence.
        source = LanguageSource.TEXT_INPUT
        return ReplyLanguage(evidence.language, source, evidence, asr_lang, conf)

    if conf < ASR_TRUST_FLOOR:
        return ReplyLanguage(evidence.language, LanguageSource.LOW_ASR_CONFIDENCE,
                             evidence, asr_lang, conf)

    if evidence.language is not asr_lang and evidence.decisive:
        # This is the code-switch case: ASR heard English nouns and labelled the whole
        # utterance `en`, but the sentence is grammatically Romanian.
        return ReplyLanguage(evidence.language, LanguageSource.MATRIX_OVERRIDE,
                             evidence, asr_lang, conf)

    return ReplyLanguage(asr_lang, LanguageSource.TRANSCRIPT, evidence, asr_lang, conf)


# --------------------------------------------------------------------------- diacritics

DIACRITIC_REPAIRS: dict[str, str] = {
    # Only entries where the ASCII form is NOT itself a valid Romanian word and NOT an
    # English word. Anything ambiguous is in AMBIGUOUS_ASCII below and is reported, never
    # rewritten. Adding an entry here requires both of those checks.
    "amandoi": "amândoi", "amandoua": "amândouă",
    "asa": "așa", "astazi": "astăzi", "atat": "atât", "atata": "atâta",
    "asteapta": "așteaptă", "astept": "aștept", "asteptam": "așteptăm",
    "cand": "când", "cateva": "câteva", "cativa": "câțiva", "citeste": "citește",
    "discutie": "discuție", "discutii": "discuții", "discutia": "discuția",
    "dimineata": "dimineața", "dupa": "după", "fara": "fără",
    "fisier": "fișier", "fisiere": "fișiere", "fisierul": "fișierul",
    "fisierele": "fișierele", "gaseste": "găsește", "gasit": "găsit",
    "greseala": "greșeală", "greseli": "greșeli",
    "imi": "îmi", "inainte": "înainte", "inca": "încă", "incepe": "începe",
    "inchide": "închide", "incearca": "încearcă", "inseamna": "înseamnă",
    "insa": "însă", "inteleg": "înțeleg", "intelege": "înțelege", "intr": "într",
    "intreb": "întreb", "intreaba": "întreabă", "intrebare": "întrebare",
    "intrebarea": "întrebarea", "intrebari": "întrebări", "intrebarile": "întrebările",
    "isi": "își", "iti": "îți",
    "maine": "mâine", "marti": "marți", "multumesc": "mulțumesc",
    "multumim": "mulțumim", "rand": "rând", "randul": "rândul",
    "actiune": "acțiune", "actiuni": "acțiuni", "atentie": "atenție",
    "informatiile": "informațiile", "situatia": "situația", "solutia": "soluția",
    "conditii": "condiții", "functie": "funcție", "informatii": "informații",
    "optiune": "opțiune", "optiuni": "opțiuni", "sectiune": "secțiune",
    "situatie": "situație", "solutie": "soluție", "solutii": "soluții",
    "poti": "poți", "putin": "puțin", "rau": "rău", "si": "și", "ti": "ți", "putina": "puțină",
    "raspuns": "răspuns",
    "raspunsul": "răspunsul", "raspunde": "răspunde", "raspund": "răspund",
    "romana": "română", "romaneste": "românește", "sapte": "șapte", "sase": "șase",
    "sedinta": "ședință", "sedinte": "ședințe", "sedintei": "ședinței",
    "sters": "șters", "sterge": "șterge", "stiu": "știu", "stii": "știi", "stie": "știe",
    "adauga": "adaugă", "salveaza": "salvează", "pleaca": "pleacă",
    "lucreaza": "lucrează", "soseste": "sosește", "masina": "mașină",
    "usor": "ușor", "vorbeste": "vorbește", "vorbesti": "vorbești",
}

AMBIGUOUS_ASCII: frozenset[str] = frozenset({
    # Real Romanian words whose diacritic form is a *different* real word, or which
    # collide with English. Reported by `diacritics_report` so an eval can see them,
    # never silently rewritten — guessing here changes meaning.
    # Every entry here must be a word that is CORRECT as written *and* correct with a
    # diacritic, meaning something different. A word that is simply correct without
    # diacritics ("sunt", "este", "unde") does not belong here — putting it here makes
    # good output score as broken, which quietly poisons the eval metric.
    "sa", "ca", "cat", "tine", "as", "in", "pana", "fata", "tara", "mana",
    "aceasta", "acesta", "vara", "para", "sar", "tai", "doua", "noua",
    "ora", "vina", "masa", "sta", "tata",
})


def _is_codeish(token: str) -> bool:
    """Never touch anything that might be an identifier, a path, a URL or a command."""
    return bool(re.search(r"[/\\._@:#0-9]", token)) or (token != token.lower() and token.isupper())


def restore_diacritics(text: str, language: Language) -> str:
    """Repair unambiguously ASCII-fied Romanian words. No-op for English.

    Conservative on purpose. This is a safety net for a quantised local model that
    dropped diacritics, not a spell-checker: it fixes what it is certain about and leaves
    everything else exactly as the model wrote it.
    """
    if language is not Language.RO:
        return text

    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        if _is_codeish(token):
            return token
        low = token.lower()
        if low in TECHNICAL_BORROWINGS or low in EN_FUNCTION_WORDS:
            return token
        if low not in DIACRITIC_REPAIRS:
            return token
        fixed = DIACRITIC_REPAIRS[low]
        if token[0].isupper():
            fixed = fixed[0].upper() + fixed[1:]
        return fixed

    return re.sub(r"[A-Za-z][A-Za-z'-]*", repl, text)


@dataclass(frozen=True)
class DiacriticsReport:
    language: Language
    total_words: int
    diacritic_chars: int
    repairable: tuple[str, ...]
    """ASCII-fied words we know how to fix. Non-empty after `restore_diacritics` is a bug."""
    ambiguous: tuple[str, ...]
    """ASCII-fied words we refuse to guess at. Non-empty means the model, not the
    post-processor, needs fixing — surface it in the eval, do not paper over it."""

    @property
    def ok(self) -> bool:
        return not self.repairable and not self.ambiguous

    @property
    def score(self) -> float:
        """1.0 = clean. Used as an eval metric, so it must be continuous, not a boolean."""
        if self.language is not Language.RO or self.total_words == 0:
            return 1.0
        bad = len(self.repairable) + len(self.ambiguous)
        return max(0.0, 1.0 - bad / self.total_words)


def diacritics_report(text: str, language: Language) -> DiacriticsReport:
    words = _WORD_RE.findall(text)
    if language is not Language.RO:
        return DiacriticsReport(language, len(words), 0, (), ())
    repairable: list[str] = []
    ambiguous: list[str] = []
    for w in words:
        if _is_codeish(w):
            continue
        low = w.lower()
        if low in TECHNICAL_BORROWINGS or low in EN_FUNCTION_WORDS:
            continue
        if low in DIACRITIC_REPAIRS:
            repairable.append(w)
        elif low in AMBIGUOUS_ASCII:
            ambiguous.append(w)
    return DiacriticsReport(
        language=language, total_words=len(words),
        diacritic_chars=len(_RO_DIACRITIC_RE.findall(text)),
        repairable=tuple(repairable), ambiguous=tuple(ambiguous),
    )


class DiacriticRepairStream:
    """Streaming-safe wrapper around `restore_diacritics`.

    The repair is word-level, but deltas arrive mid-word ("Ras", "pun", "sul"). Buffering
    the whole reply would destroy the point of streaming, so this holds back only the
    trailing partial word — at most a few characters — and releases everything before it.
    Time-to-first-token is unaffected beyond one word boundary.
    """

    __slots__ = ("_buf", "_language")

    def __init__(self, language: Language) -> None:
        self._language = language
        self._buf = ""

    def push(self, delta: str) -> str:
        if self._language is not Language.RO:
            return delta
        self._buf += delta
        # Everything up to and including the last non-word character is safe to emit.
        cut = max((self._buf.rfind(c) for c in " \n\t.,;:!?()\"'-"), default=-1)
        if cut < 0:
            return ""
        out, self._buf = self._buf[: cut + 1], self._buf[cut + 1 :]
        return restore_diacritics(out, self._language)

    def flush(self) -> str:
        if not self._buf:
            return ""
        out, self._buf = self._buf, ""
        return restore_diacritics(out, self._language)


__all__ = [
    "AMBIGUOUS_ASCII",
    "ASR_TRUST_FLOOR",
    "DIACRITIC_REPAIRS",
    "RO_DIACRITICS",
    "TECHNICAL_BORROWINGS",
    "DiacriticRepairStream",
    "DiacriticsReport",
    "LanguageSource",
    "MatrixEvidence",
    "ReplyLanguage",
    "detect_matrix_language",
    "diacritics_report",
    "resolve_reply_language",
    "restore_diacritics",
]
