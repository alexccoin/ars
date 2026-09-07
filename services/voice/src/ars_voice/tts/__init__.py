"""Speech synthesis. Streaming, cancellable, EN + RO + DE, many voices and characters."""

from .catalogue import (
    CHARACTERS,
    ROMANIAN_LIMITATION,
    VOICES,
    VOICES_BY_NAME,
    CharacterProfile,
    VoiceGender,
    VoiceProfile,
    caveats_for,
    describe,
    describe_character,
    parse_voice_spec,
    voices,
)
from .character import CharacterTtsEngine
from .dsp import CHARACTER_CHAINS, CharacterChain
from .mock import MockTtsEngine, NullTtsEngine
from .piper import PiperTtsEngine, UnknownVoiceError, VoiceSelection
from .segmentation import SentenceStreamer, split_sentences
from .streaming import StreamingSynthesizer

__all__ = [
    "CHARACTERS", "CHARACTER_CHAINS", "ROMANIAN_LIMITATION", "VOICES", "VOICES_BY_NAME",
    "CharacterChain", "CharacterProfile", "CharacterTtsEngine", "MockTtsEngine",
    "NullTtsEngine", "PiperTtsEngine", "SentenceStreamer", "StreamingSynthesizer",
    "UnknownVoiceError", "VoiceGender", "VoiceProfile", "VoiceSelection", "caveats_for",
    "describe", "describe_character", "parse_voice_spec", "split_sentences", "voices",
]
