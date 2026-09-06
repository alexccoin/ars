"""A trilingual parallel fixture set for EN / RO / DE, written for this product.

Three domains, because translation quality is not one number:

  * `COMMAND`     — what Alex says to A.R.S. Short, imperative, often elliptical. This is
                    the hardest length for any translator and the most common input.
  * `CLAUSE`      — sentences out of the documents the document tier answers from.
                    Numbers and deadlines live here, and getting one wrong is the failure
                    that costs money.
  * `DIALOGUE`    — the live-interpreter case: two people, one of whom does not speak the
                    other's language, in a pharmacy / a workshop / an office.
  * `CODESWITCH`  — the household's actual speech: a Romanian or German matrix sentence
                    carrying English technical nouns. `ars_compute.language` exists
                    because of these; a translator that "corrects" `branch-ul` into
                    `ramura` has broken the sentence.

The references are author-written, not crowd-sourced, and there is exactly one per
direction. That means chrF++ against them is a **comparator between systems measured on
this file**, not a score to quote against WMT. A system that beats another by 8 chrF here
is better at this product's sentences; a system that scores 61 is not thereby "as good as
a published 61".

`must_keep` is the second, blunter metric: the substrings that have to survive the trip
regardless of phrasing. Numbers, times, and the borrowed technical nouns. A translation
that renders "4.200 lei" as "4,200 euros" can still score 70 chrF++, and it is still
worthless. This list is what catches that, and it needs no reference at all — it can be
run against live traffic where no reference exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Triple:
    id: str
    domain: str
    en: str
    ro: str
    de: str
    must_keep: tuple[str, ...] = field(default_factory=tuple)
    """Substrings that must appear in the output verbatim in every language. Borrowed
    technical nouns only. Numbers are NOT listed here: they are checked by `numerals.py`
    against the target language's own grouping convention, because EN writes `4,200`
    where RO and DE write `4.200` and a substring check calls the correct conversion a
    failure."""

    def text(self, lang: str) -> str:
        return getattr(self, lang)


TRIPLES: tuple[Triple, ...] = (
    # ------------------------------------------------------------------ commands
    Triple(
        "cmd.email", "COMMAND",
        "Read me the last three emails from the bank.",
        "Citește-mi ultimele trei emailuri de la bancă.",
        "Lies mir die letzten drei E-Mails von der Bank vor.",
    ),
    Triple(
        "cmd.meeting", "COMMAND",
        "What time is my meeting tomorrow morning?",
        "La ce oră am ședința mâine dimineață?",
        "Um wie viel Uhr ist mein Meeting morgen früh?",
    ),
    Triple(
        "cmd.remind", "COMMAND",
        "Remind me to call the landlord on Friday.",
        "Amintește-mi să sun proprietarul vineri.",
        "Erinnere mich daran, den Vermieter am Freitag anzurufen.",
    ),
    Triple(
        "cmd.cancel", "COMMAND",
        "Cancel that, I did not mean to send it.",
        "Anulează, nu am vrut să trimit asta.",
        "Brich das ab, ich wollte das nicht senden.",
    ),
    Triple(
        "cmd.notifications", "COMMAND",
        "Turn off the notifications until six o'clock.",
        "Oprește notificările până la ora șase.",
        "Schalte die Benachrichtigungen bis sechs Uhr aus.",
    ),
    Triple(
        "cmd.search", "COMMAND",
        "Search the web for the train timetable to Vienna.",
        "Caută pe internet orarul trenurilor spre Viena.",
        "Suche im Internet den Fahrplan der Züge nach Wien.",
    ),
    # ------------------------------------------------------------------ clauses
    Triple(
        "cls.rent", "CLAUSE",
        "The monthly rent is 4,200 lei, payable by the fifth day of each month.",
        "Chiria lunară este de 4.200 lei, plătibilă până în data de cinci a fiecărei luni.",
        "Die monatliche Miete beträgt 4.200 Lei, zahlbar bis zum fünften Tag jedes Monats.",
    ),
    Triple(
        "cls.deposit", "CLAUSE",
        "The deposit is two months' rent and is returned when the flat is handed back.",
        "Garanția este de două chirii și se returnează la predarea apartamentului.",
        "Die Kaution beträgt zwei Monatsmieten und wird bei der Rückgabe der Wohnung "
        "zurückerstattet.",
    ),
    Triple(
        "cls.notice", "CLAUSE",
        "Termination requires 60 days' written notice.",
        "Rezilierea se face cu un preaviz scris de 60 de zile.",
        "Die Kündigung erfordert eine schriftliche Frist von 60 Tagen.",
    ),
    Triple(
        "cls.probation", "CLAUSE",
        "The probation period is 90 days.",
        "Perioada de probă este de 90 de zile.",
        "Die Probezeit beträgt 90 Tage.",
    ),
    Triple(
        "cls.remote", "CLAUSE",
        "Remote work is allowed three days per week with the manager's approval.",
        "Munca de acasă este permisă trei zile pe săptămână, cu acordul managerului.",
        "Homeoffice ist drei Tage pro Woche mit Zustimmung des Vorgesetzten erlaubt.",
    ),
    Triple(
        "cls.leave", "CLAUSE",
        "The annual paid leave is 25 working days plus the legal public holidays.",
        "Concediul de odihnă anual este de 25 de zile lucrătoare, plus sărbătorile legale.",
        "Der jährliche bezahlte Urlaub beträgt 25 Arbeitstage zuzüglich der gesetzlichen "
        "Feiertage.",
    ),
    Triple(
        "cls.utilities", "CLAUSE",
        "Utilities are paid separately by the tenant.",
        "Utilitățile sunt plătite separat de chiriaș.",
        "Die Nebenkosten werden vom Mieter separat bezahlt.",
    ),
    Triple(
        "cls.invoice", "CLAUSE",
        "The invoice must be settled within 14 days of receipt.",
        "Factura trebuie achitată în termen de 14 zile de la primire.",
        "Die Rechnung ist innerhalb von 14 Tagen nach Erhalt zu begleichen.",
    ),
    # ------------------------------------------------------------------ dialogue
    Triple(
        "dlg.appointment", "DIALOGUE",
        "Good morning, I have an appointment at half past nine.",
        "Bună dimineața, am o programare la nouă și jumătate.",
        "Guten Morgen, ich habe einen Termin um halb zehn.",
    ),
    Triple(
        "dlg.repeat", "DIALOGUE",
        "Could you say that again more slowly, please?",
        "Poți să repeți mai rar, te rog?",
        "Könnten Sie das bitte noch einmal langsamer sagen?",
    ),
    Triple(
        "dlg.documents", "DIALOGUE",
        "I do not have the documents with me, can I send them by email?",
        "Nu am documentele la mine, pot să le trimit pe email?",
        "Ich habe die Unterlagen nicht dabei, kann ich sie per E-Mail schicken?",
    ),
    Triple(
        "dlg.repair", "DIALOGUE",
        "How much will the repair cost and how long will it take?",
        "Cât va costa reparația și cât va dura?",
        "Wie viel wird die Reparatur kosten und wie lange wird sie dauern?",
    ),
    Triple(
        "dlg.allergy", "DIALOGUE",
        "My daughter is allergic to penicillin.",
        "Fiica mea este alergică la penicilină.",
        "Meine Tochter ist allergisch gegen Penizillin.",
    ),
    Triple(
        "dlg.pharmacy", "DIALOGUE",
        "Where is the nearest pharmacy that is open now?",
        "Unde este cea mai apropiată farmacie deschisă acum?",
        "Wo ist die nächste Apotheke, die jetzt geöffnet ist?",
    ),
    Triple(
        "dlg.card", "DIALOGUE",
        "I would like to pay by card if that is possible.",
        "Aș vrea să plătesc cu cardul, dacă se poate.",
        "Ich würde gerne mit Karte bezahlen, wenn das möglich ist.",
    ),
    Triple(
        "dlg.price", "DIALOGUE",
        "We agreed on a different price last week.",
        "Ne-am înțeles la un alt preț săptămâna trecută.",
        "Wir hatten uns letzte Woche auf einen anderen Preis geeinigt.",
    ),
    # ------------------------------------------------------------------ code-switched
    Triple(
        "csw.rebase", "CODESWITCH",
        "Can you rebase the staging branch and push it again?",
        "Poți să faci un rebase pe branch-ul de staging și să-l dai push din nou?",
        "Kannst du den Staging-Branch rebasen und ihn erneut pushen?",
        must_keep=("staging",),
    ),
    Triple(
        "csw.deploy", "CODESWITCH",
        "The deployment failed because the token expired.",
        "Deployment-ul a eșuat pentru că a expirat token-ul.",
        "Das Deployment ist fehlgeschlagen, weil das Token abgelaufen ist.",
    ),
)

LANGUAGES: tuple[str, ...] = ("en", "ro", "de")
DIRECTIONS: tuple[tuple[str, str], ...] = tuple(
    (a, b) for a in LANGUAGES for b in LANGUAGES if a != b
)

LANGUAGE_NAMES = {"en": "English", "ro": "Romanian", "de": "German"}


# ------------------------------------------------------------- document fixtures
#
# A German document, with EN and RO questions asked of it. This is the fixture that
# decides whether translation-at-ingest closes the document tier's cross-language hole.
# Same shape and register as the fixtures in `retrieval_calibration.py`, so the two
# benchmarks are comparable.

GERMAN_DOC = """MIETVERTRAG — Republicii-Straße 42, Cluj-Napoca

Die monatliche Miete beträgt 4.200 Lei und ist bis zum fünften Tag jedes Monats zu zahlen.
Die Kaution beträgt zwei Monatsmieten, also 8.400 Lei, und wird bei der Rückgabe der
Wohnung zurückerstattet.
Die Laufzeit des Vertrages beträgt 12 Monate, beginnend am 1. Mai 2026.
Ein Stellplatz ist in der Miete nicht enthalten. Er kostet zusätzlich 300 Lei pro Monat.
Die Nebenkosten (Strom, Wasser, Gas, Internet) werden vom Mieter separat bezahlt.
Die Kündigung erfolgt mit einer schriftlichen Frist von 60 Tagen."""

GERMAN_DOC_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Wie hoch ist die monatliche Miete?", "de"),
    ("How much is the monthly rent?", "en"),
    ("Cât este chiria pe lună?", "ro"),
    ("Ist ein Stellplatz in der Miete enthalten?", "de"),
    ("Is parking included in the rent?", "en"),
    ("Locul de parcare este inclus în chirie?", "ro"),
    ("Wie hoch ist die Kaution?", "de"),
    ("How much is the deposit?", "en"),
    ("Cât este garanția?", "ro"),
    ("Mit welcher Frist kann ich kündigen?", "de"),
    ("How much notice do I have to give to end the lease?", "en"),
    ("Cu ce preaviz se reziliază contractul?", "ro"),
)

IRRELEVANT_QUESTIONS: tuple[str, ...] = (
    "What is the capital of Portugal?",
    "Care este capitala Portugaliei?",
    "Wie koche ich Nudeln?",
    "cum se gătește pastele?",
    "When does the next train to Bucharest leave?",
    "Wie wird das Wetter morgen in Cluj?",
)

__all__ = [
    "DIRECTIONS",
    "GERMAN_DOC",
    "GERMAN_DOC_QUESTIONS",
    "IRRELEVANT_QUESTIONS",
    "LANGUAGES",
    "LANGUAGE_NAMES",
    "TRIPLES",
    "Triple",
]
