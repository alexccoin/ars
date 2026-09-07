"""A different voice and a different face when the subject is the child.

Alex has a young son, Liam. A seven-year-old and an adult want different things from the
same assistant, and switching both the voice and the face is how a machine says "I am
talking to you now" without anyone having to explain it.

Two properties are load-bearing and both are tested here: the name never appears in the
source, and the persona is chosen by a whole-word match rather than by a model.
"""

from __future__ import annotations

import pathlib

import pytest
from ars_gateway.persona import Persona, PersonaPicker
from ars_protocol import Language


def test_it_is_inert_until_a_name_is_configured() -> None:
    """A child's name hardcoded into a repository is a child's name published. Empty by
    default, and with it empty the whole feature does nothing."""
    picker = PersonaPicker("")

    assert not picker.enabled
    assert picker.choose("what is Liam doing?", Language.EN).persona is Persona.DEFAULT


def test_the_childs_name_is_nowhere_in_the_source() -> None:
    """The safeguard above is only real if nobody quietly wrote the name into a default."""
    root = pathlib.Path(__file__).resolve().parents[3]
    for path in (root / "services" / "gateway" / "src").rglob("*.py"):
        assert "liam" not in path.read_text().lower(), path
    assert "liam" not in (root / "packages" / "core" / "src" / "ars_core" / "config.py"
                          ).read_text().lower()


@pytest.mark.parametrize("text,expected", [
    ("what is Liam doing today?", Persona.COMPANION),
    ("remind liam about football", Persona.COMPANION),
    ("LIAM asked me something", Persona.COMPANION),
    ("what is the rent?", Persona.DEFAULT),
    ("William came over", Persona.DEFAULT),       # whole words only
    ("brilliant idea", Persona.DEFAULT),
    ("", Persona.DEFAULT),
])
def test_who_the_turn_is_about(text: str, expected: Persona) -> None:
    """A whole-word match on one configured name, and nothing cleverer. A classifier would
    be right more often and wrong unpredictably — and its failure mode is firing on a
    stranger's message, which is the opposite of what a persona that signals WHO is being
    spoken to can afford. A regex that does not fire just gives the ordinary voice."""
    assert PersonaPicker("Liam").choose(text, Language.EN).persona is expected


@pytest.mark.parametrize("language,voice", [
    (Language.EN, "pup/pup_en"),
    (Language.RO, "pup/pup_ro"),
    (Language.DE, "pup/pup_de"),
])
def test_the_companion_has_a_voice_in_every_language(language: Language, voice: str) -> None:
    """Rule 5 applies to a child as much as to an adult: the pup speaks Romanian on the
    day it speaks English, because the character is DSP over a real voice rather than a
    model trained on one."""
    assert PersonaPicker("Liam").choose("is Liam home?", language).voice == voice


def test_the_companion_voice_exists_in_the_catalogue() -> None:
    """A persona naming a voice nobody built would fail at synthesis time, in a turn, out
    loud, in front of the child it was for."""
    from ars_voice.tts.catalogue import CHARACTERS, VOICES

    names = {v.name for v in VOICES}
    for language in (Language.EN, Language.RO, Language.DE):
        spec = PersonaPicker("Liam").choose("Liam", language).voice
        character, _, voice = spec.partition("/")
        assert character in CHARACTERS, character
        assert voice in names, voice


def test_the_choice_says_why() -> None:
    """A machine that changes its voice without saying why is unsettling rather than
    friendly."""
    assert "Liam" in PersonaPicker("Liam").choose("where is Liam?", Language.EN).reason
