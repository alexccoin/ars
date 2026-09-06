"""Guard-owned user-facing text, in English and Romanian.

Why this file exists at all
---------------------------
When the guard says ASK, the sentence the user hears is the last thing standing between
an attacker and the user's mailbox. If any part of that sentence can be written by the
model, and the model has just read an attacker's email, then the attacker writes the
consent prompt: *"May I archive some old newsletters?"* while the real call is
``email.send``.

So the guard never asks the model for words. Every string the user sees or hears about a
guard decision is assembled here, from templates that shipped in this diff, in both
languages A.R.S speaks. The only variable parts are:

* the capability     - an enum member, rendered from a table below (guard-owned);
* the resource       - the concrete target, sanitised (see :func:`safe_fragment`);
* the taint source   - a ``SourceKind`` enum plus a sanitised origin string;
* the model summary  - **only when the turn is not tainted**, and sanitised even then.

`safe_fragment` is not decoration. A URL, a mailbox query and an email display name are
all attacker-influenced, and they land in a string that gets spoken aloud and rendered in
a UI. Control characters, bidi overrides and zero-width joiners are removed there so a
consent prompt cannot be visually rearranged into a different sentence.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum

from ars_protocol import Capability, Language, SourceKind

MAX_FRAGMENT_CHARS = 96
"""Hard cap on any untrusted fragment spliced into a prompt. Long enough for a URL or a
mailbox query, short enough that nobody can smuggle a paragraph of instructions into a
sentence the user is about to approve."""

_DISALLOWED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
"""Control, format (includes bidi overrides and zero-width joiners), private use,
surrogate, unassigned. None of these have any business in a spoken consent prompt."""


def safe_fragment(text: str | None, *, max_chars: int = MAX_FRAGMENT_CHARS) -> str:
    """Make an untrusted string safe to splice into a guard-composed sentence.

    Normalises to NFKC (so lookalike forms collapse), strips every control and format
    character, collapses whitespace to single spaces, and truncates. Returns ``""`` for
    ``None`` or for input that is entirely stripped away.
    """
    if not text:
        return ""
    normalised = unicodedata.normalize("NFKC", text)
    kept = [ch for ch in normalised if unicodedata.category(ch) not in _DISALLOWED_CATEGORIES]
    collapsed = " ".join("".join(kept).split())
    if len(collapsed) > max_chars:
        collapsed = collapsed[: max_chars - 1].rstrip() + "…"
    return collapsed


class Msg(StrEnum):
    """Template keys. One per distinct thing the guard can tell the user."""

    ALLOW_SILENT = "allow_silent"
    ALLOW_SESSION_CONFIRMED = "allow_session_confirmed"

    ASK_FIRST_USE = "ask_first_use"
    ASK_EVERY_USE = "ask_every_use"
    ASK_ONCE_PER_SESSION = "ask_once_per_session"
    ASK_TAINTED = "ask_tainted"
    ASK_TAINTED_EGRESS = "ask_tainted_egress"

    DENY_GUARD_DISABLED = "deny_guard_disabled"
    DENY_NO_GRANT = "deny_no_grant"
    DENY_GRANT_EXPIRED = "deny_grant_expired"
    DENY_GRANT_REVOKED = "deny_grant_revoked"
    DENY_RESOURCE_OUT_OF_SCOPE = "deny_resource_out_of_scope"
    DENY_TAINTED_CRITICAL = "deny_tainted_critical"
    DENY_RATE_LIMITED_TURN = "deny_rate_limited_turn"
    DENY_RATE_LIMITED_SESSION = "deny_rate_limited_session"
    DENY_POLICY = "deny_policy"


# --------------------------------------------------------------------------- templates
#
# Placeholders, all pre-sanitised by the composer:
#   {capability}  human name of the capability, from CAPABILITY_NAMES
#   {resource}    the concrete target, or the "everything" wording, from _ANY_RESOURCE
#   {scope}       the grant's own resource patterns, comma joined
#   {source}      human name of the taint SourceKind, from SOURCE_NAMES
#   {origin}      sanitised uri/label of the taint source, or the "unknown" wording
#   {summary}     model-written one-liner - NEVER passed when the turn is tainted
#   {limit}       an integer

_EN: dict[Msg, str] = {
    Msg.ALLOW_SILENT:
        "Allowed: {capability}, on {resource}.",
    Msg.ALLOW_SESSION_CONFIRMED:
        "Allowed: {capability}, on {resource} - you approved this earlier in this session.",

    Msg.ASK_FIRST_USE:
        "I need your permission to do this: {capability}, on {resource}. {summary} May I?",
    Msg.ASK_EVERY_USE:
        "You asked me to check with you every time: {capability}, on {resource}. "
        "{summary} May I?",
    Msg.ASK_ONCE_PER_SESSION:
        "First time this session: {capability}, on {resource}. {summary} "
        "May I, for the rest of this conversation?",
    Msg.ASK_TAINTED_EGRESS:
        "Careful - part of this turn came from {source} ({origin}), and this would send "
        "text out of your machine. Read what would be sent: {resource}. Outside text "
        "cannot choose what leaves here. Send it?",

    Msg.ASK_TAINTED:
        "Careful - part of this turn came from {source} ({origin}). I treat outside text "
        "as data, never as instructions, so I will not do this on its say-so. "
        "Do you want me to {capability}, on {resource}?",

    Msg.DENY_GUARD_DISABLED:
        "No. The guard is switched off, so I have no access to anything private and I "
        "cannot change anything in the world - including anything that would "
        "{capability}. Turn the guard back on to use it.",
    Msg.DENY_NO_GRANT:
        "No. You have not given me permission to {capability}, and this is not something "
        "I will ask for in the middle of a task. Grant it deliberately in settings first.",
    Msg.DENY_GRANT_EXPIRED:
        "No. Your permission to {capability} has expired. You can renew it in settings - "
        "I will not extend it myself.",
    Msg.DENY_GRANT_REVOKED:
        "No. You took back my permission to {capability}. I will not ask you to give it "
        "again during a task; grant it in settings if you have changed your mind.",
    Msg.DENY_RESOURCE_OUT_OF_SCOPE:
        "No. You allowed me to {capability}, but only for {scope} - and this is {resource}. "
        "I will not stretch a permission you gave me to cover something you did not.",
    Msg.DENY_TAINTED_CRITICAL:
        "No. This turn used text from {source} ({origin}), and I will never {capability} "
        "because outside text suggested it. If you want this, ask me yourself in a "
        "new turn.",
    Msg.DENY_RATE_LIMITED_TURN:
        "No. That is {limit} attempts to {capability} in a single turn - something is "
        "looping. Stopping here.",
    Msg.DENY_RATE_LIMITED_SESSION:
        "No. I have already tried to {capability} {limit} times in this conversation. "
        "Stopping until you start a new one.",
    Msg.DENY_POLICY:
        "No. Policy does not allow me to {capability}, on {resource}.",
}

_RO: dict[Msg, str] = {
    Msg.ALLOW_SILENT:
        "Permis să {capability}, pentru {resource}.",
    Msg.ALLOW_SESSION_CONFIRMED:
        "Permis să {capability}, pentru {resource} - ai aprobat asta mai devreme în "
        "această sesiune.",

    Msg.ASK_FIRST_USE:
        "Am nevoie de permisiunea ta ca să fac asta: să {capability}, pentru {resource}. "
        "{summary} Îmi dai voie?",
    Msg.ASK_EVERY_USE:
        "Mi-ai cerut să te întreb de fiecare dată: să {capability}, pentru {resource}. "
        "{summary} Îmi dai voie?",
    Msg.ASK_ONCE_PER_SESSION:
        "Prima dată în această sesiune: să {capability}, pentru {resource}. {summary} "
        "Îmi dai voie, pentru tot restul conversației?",
    Msg.ASK_TAINTED_EGRESS:
        "Atenție - o parte din acest schimb are ca sursă {source} ({origin}), iar asta ar "
        "trimite text în afara calculatorului tău. Citește ce s-ar trimite: {resource}. "
        "Un text din exterior nu poate alege ce pleacă de aici. Îl trimit?",

    Msg.ASK_TAINTED:
        "Atenție - o parte din acest schimb are ca sursă {source} ({origin}). Tratez "
        "textul din exterior ca date, niciodată ca instrucțiuni, așa că nu fac asta "
        "pentru că a cerut-o el. Vrei să {capability}, pentru {resource}?",

    Msg.DENY_GUARD_DISABLED:
        "Nu. Garda este oprită, deci nu am acces la nimic privat și nu pot schimba nimic "
        "în lume - deci nici nu pot să {capability}. Pornește garda la loc ca să "
        "folosesc asta.",
    Msg.DENY_NO_GRANT:
        "Nu. Nu mi-ai dat permisiunea să {capability}, iar asta nu este ceva ce cer în "
        "mijlocul unei sarcini. Acord-o intenționat din setări, mai întâi.",
    Msg.DENY_GRANT_EXPIRED:
        "Nu. Nu mai am voie să {capability} - permisiunea a expirat. O poți reînnoi din "
        "setări; eu nu o prelungesc singur.",
    Msg.DENY_GRANT_REVOKED:
        "Nu. Nu mai am voie să {capability} - mi-ai retras permisiunea. Nu îți cer să mi-o "
        "dai din nou în timpul unei sarcini; acord-o din setări dacă te-ai răzgândit.",
    Msg.DENY_RESOURCE_OUT_OF_SCOPE:
        "Nu. Mi-ai permis să {capability}, dar numai pentru {scope} - iar aici este vorba "
        "de {resource}. Nu întind o permisiune pe care mi-ai dat-o ca să acopere ceva "
        "ce nu mi-ai dat.",
    Msg.DENY_TAINTED_CRITICAL:
        "Nu. În acest schimb am folosit text care are ca sursă {source} ({origin}), iar eu "
        "nu {capability} niciodată pentru că a cerut-o un text din exterior. Dacă vrei "
        "asta, cere-mi tu, într-un schimb nou.",
    Msg.DENY_RATE_LIMITED_TURN:
        "Nu. Am încercat deja de {limit} ori să {capability} într-un singur schimb - ceva "
        "se învârte în buclă. Mă opresc aici.",
    Msg.DENY_RATE_LIMITED_SESSION:
        "Nu. Am încercat deja de {limit} ori să {capability} în această conversație. "
        "Mă opresc până începi una nouă.",
    Msg.DENY_POLICY:
        "Nu. Politica nu îmi permite să {capability}, pentru {resource}.",
}

TEMPLATES: dict[Language, dict[Msg, str]] = {Language.EN: _EN, Language.RO: _RO}


# --------------------------------------------------------------------- capability names
#
# Written as verb phrases so they read correctly inside "May I {capability}?" and
# "Vrei sa {capability}?". Deliberately concrete: "send an email as you" is the truth,
# "email access" is the kind of vague phrasing people click through.

CAPABILITY_NAMES: dict[Language, dict[Capability, str]] = {
    Language.EN: {
        Capability.WEB_SEARCH: "search the web",
        Capability.WEB_FETCH: "open a web page",
        Capability.GITHUB_READ_PUBLIC: "read a public GitHub repository",
        Capability.EMAIL_READ: "read your email",
        Capability.EMAIL_SEARCH: "search your email",
        Capability.CALENDAR_READ: "read your calendar",
        Capability.CONTACTS_READ: "read your contacts",
        Capability.GITHUB_READ_PRIVATE: "read your private GitHub repositories",
        Capability.FILES_READ: "read your files",
        Capability.EMAIL_SEND: "send an email as you",
        Capability.CALENDAR_WRITE: "change your calendar",
        Capability.FILES_WRITE: "write to your files",
        Capability.GITHUB_WRITE: "write to GitHub as you",
        Capability.APP_CONTROL: "control apps on this device",
        Capability.SHELL_EXEC: "run commands on this computer",
        Capability.NETWORK_EGRESS: "send data out over the network",
        Capability.MEMORY_READ: "read what I remember about you",
        Capability.MEMORY_WRITE: "save something to what I remember about you",
    },
    Language.RO: {
        Capability.WEB_SEARCH: "caut pe web",
        Capability.WEB_FETCH: "deschid o pagină web",
        Capability.GITHUB_READ_PUBLIC: "citesc un depozit public de pe GitHub",
        Capability.EMAIL_READ: "îți citesc e-mailul",
        Capability.EMAIL_SEARCH: "caut în e-mailul tău",
        Capability.CALENDAR_READ: "îți citesc calendarul",
        Capability.CONTACTS_READ: "îți citesc agenda de contacte",
        Capability.GITHUB_READ_PRIVATE: "îți citesc depozitele private de pe GitHub",
        Capability.FILES_READ: "îți citesc fișierele",
        Capability.EMAIL_SEND: "trimit un e-mail în numele tău",
        Capability.CALENDAR_WRITE: "îți modific calendarul",
        Capability.FILES_WRITE: "îți scriu în fișiere",
        Capability.GITHUB_WRITE: "scriu pe GitHub în numele tău",
        Capability.APP_CONTROL: "controlez aplicații de pe acest dispozitiv",
        Capability.SHELL_EXEC: "rulez comenzi pe acest computer",
        Capability.NETWORK_EGRESS: "trimit date în rețea",
        Capability.MEMORY_READ: "citesc ce îmi amintesc despre tine",
        Capability.MEMORY_WRITE: "salvez ceva în ce îmi amintesc despre tine",
    },
}

SOURCE_NAMES: dict[Language, dict[SourceKind, str]] = {
    Language.EN: {
        SourceKind.MICROPHONE: "the microphone",
        SourceKind.KEYBOARD: "the keyboard",
        SourceKind.SYSTEM_PROMPT: "my own instructions",
        SourceKind.MEMORY: "my memory",
        SourceKind.LOCAL_FILE: "a file on this machine",
        SourceKind.EMAIL: "an email",
        SourceKind.WEB_PAGE: "a web page",
        SourceKind.WEB_SEARCH_RESULT: "a web search result",
        SourceKind.GITHUB: "GitHub",
        SourceKind.SKILL_OUTPUT: "the output of a tool",
    },
    Language.RO: {
        SourceKind.MICROPHONE: "microfonul",
        SourceKind.KEYBOARD: "tastatura acestui dispozitiv",
        SourceKind.SYSTEM_PROMPT: "propriile mele instrucțiuni",
        SourceKind.MEMORY: "memoria mea",
        SourceKind.LOCAL_FILE: "un fișier de pe acest calculator",
        SourceKind.EMAIL: "un e-mail",
        SourceKind.WEB_PAGE: "o pagină web",
        SourceKind.WEB_SEARCH_RESULT: "un rezultat de căutare web",
        SourceKind.GITHUB: "GitHub",
        SourceKind.SKILL_OUTPUT: "rezultatul unei unelte",
    },
}

_ANY_RESOURCE: dict[Language, str] = {
    Language.EN: "everything it covers",
    Language.RO: "tot ce acoperă",
}

_UNKNOWN_ORIGIN: dict[Language, str] = {
    Language.EN: "origin not recorded",
    Language.RO: "origine neînregistrată",
}

_UNKNOWN_SOURCE: dict[Language, str] = {
    Language.EN: "a source outside this conversation",
    Language.RO: "o sursă din afara acestei conversații",
}
"""Used when a turn is flagged tainted but carries no provenance. The guard still names
the taint - "I do not know exactly where this came from" is itself information the user
needs, and is far better than quietly dropping the warning."""


def capability_name(capability: Capability, language: Language) -> str:
    return CAPABILITY_NAMES[language][capability]


def source_name(source: SourceKind, language: Language) -> str:
    return SOURCE_NAMES[language][source]


def any_resource(language: Language) -> str:
    return _ANY_RESOURCE[language]


def unknown_origin(language: Language) -> str:
    return _UNKNOWN_ORIGIN[language]


def unknown_source(language: Language) -> str:
    return _UNKNOWN_SOURCE[language]


class _Blank(dict):  # type: ignore[type-arg]
    """format_map backing store that renders a missing placeholder as empty string."""

    def __missing__(self, key: str) -> str:
        return ""


def render(key: Msg, language: Language, **fields: object) -> str:
    """Render one guard template.

    Every field is substituted by name; a missing field renders as an empty string
    rather than raising, so a template that gains a placeholder can never take the guard
    down mid-decision. Whitespace is collapsed so an omitted `{summary}` does not leave
    a double space in something that gets spoken aloud.
    """
    template = TEMPLATES[language][key]
    return " ".join(template.format_map(_Blank(fields)).split())
